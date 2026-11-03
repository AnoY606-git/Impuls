"""Small, synchronous boundary around the unofficial Yandex Music API.

Call these methods from a worker rather than the GTK main loop. The dependency is
imported lazily so that unit tests and non-network application startup are safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import logging
import math
import time
from numbers import Real
import re
import socket
import threading
from weakref import WeakKeyDictionary
from typing import Callable, TypeVar
from urllib.parse import urlsplit


_TIMEOUT_SECONDS = 15
_DEVICE_TIMEOUT_SECONDS = 300
_BATCH_SIZE = 100
_CACHE_SECONDS = 60
_METADATA_CACHE_LIMIT = 5000
_PLAYLIST_CACHE_LIMIT = 32
_AUDIO_CODECS = frozenset(('flac', 'flac-mp4', 'aac', 'aac-mp4',
                           'he-aac', 'he-aac-mp4', 'mp3'))
_ID_RE = re.compile(r"^[0-9]+(?::[0-9]+)?$")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Track:
    id: str
    title: str
    artists: tuple[str, ...]
    album: str = ""
    duration_ms: int = 0
    cover_url: str | None = None


@dataclass(frozen=True, slots=True)
class Playlist:
    id: str
    title: str
    owner_uid: str
    track_count: int = 0
    cover_url: str | None = None


@dataclass(frozen=True, slots=True)
class AudioStream:
    """Resolved audio; bitrate is in kbit/s, or zero when not reported."""
    url: str = field(repr=False)
    codec: str
    bitrate: int
    quality: str


class ServiceError(Exception):
    """A safe user-facing error that does not contain credentials or URLs."""


class CollectionSnapshot:
    """Identity for one view's stable sequence of paginated source entries."""
    __slots__ = ('__weakref__',)


def _new_client(token: str | None = None):
    # The dependency logs complete API result objects at DEBUG, including OAuth
    # tokens. Never let a global DEBUG setting turn this logging on.
    logging.getLogger("yandex_music").setLevel(logging.WARNING)
    try:
        from yandex_music import Client
        from yandex_music.utils.request import Request
    except ImportError:
        raise ServiceError("Не установлена библиотека yandex-music.") from None
    return Client(token, request=Request(timeout=_TIMEOUT_SECONDS))


def _safely(message: str, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except ServiceError:
        raise
    except Exception:
        # Upstream exceptions can include request URLs and response bodies.
        raise ServiceError(message) from None


def _https_url(value: object) -> str | None:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname
        if (url.scheme != "https" or not host or "\\" in url.netloc or "%" in url.netloc
                or not re.fullmatch(r"[A-Za-z0-9.-]+", host) or url.username is not None
                or url.password is not None or url.port not in (None, 443)):
            return None
        host_lower = host.lower().rstrip('.')
        if (host_lower in ("localhost", "localhost6")
                or host_lower.endswith((".localhost", ".local", ".localdomain", ".localdomain6"))):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        # libc also accepts historical integer, hexadecimal and shortened IPv4
        # spellings such as 2130706433 or 127.1; never send those to the player.
        try:
            socket.inet_aton(host)
        except OSError:
            return value
        return None
    except ValueError:
        return None


def _music_url(value: object) -> str | None:
    """Allow audio and metadata links only on Yandex-controlled domains."""
    url = _https_url(value)
    if not url:
        return None
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    allowed = ('yandex.ru', 'yandex.net', 'yandex.com', 'ya.ru', 'yandexcloud.net', 'yastatic.net')
    return url if any(host == domain or host.endswith('.' + domain) for domain in allowed) else None


def _cover_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("%%", "200x200")
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate
    return _https_url(candidate)


def _numeric_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ServiceError("Неверный идентификатор элемента.")
    return value


def _track(raw: object) -> Track | None:
    if raw is None:
        return None
    # Some partial upstream Album objects have no id, causing track_id to be
    # "123:None". Fall back to the valid bare track id in that case.
    identifiers = (getattr(raw, "track_id", None), getattr(raw, "id", None))
    identifier = next((value for value in identifiers
                       if value is not None and _ID_RE.fullmatch(str(value))), None)
    if identifier is None:
        return None
    title = str(getattr(raw, "title", None) or "Без названия")
    version = getattr(raw, "version", None)
    if version:
        title = f"{title} ({version})"
    artists = tuple(str(a.name) for a in (getattr(raw, "artists", None) or ()) if getattr(a, "name", None))
    albums = getattr(raw, "albums", None) or ()
    album = str(getattr(albums[0], "title", "") or "") if albums else ""
    duration = getattr(raw, "duration_ms", None)
    duration = duration if isinstance(duration, int) and duration > 0 else 0
    return Track(str(identifier), title, artists, album, duration, _cover_url(getattr(raw, "cover_uri", None)))


def _tracks(values: object) -> list[Track]:
    return [item for raw in (values or ()) if (item := _track(raw)) is not None]


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise ServiceError("Запрос отменён.")


def _page_bounds(offset: int, limit: int) -> None:
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _BATCH_SIZE):
        raise ValueError("Неверные параметры страницы.")


def _entry_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _ID_RE.fullmatch(str(value)):
        raise ServiceError("Сервис вернул некорректный идентификатор трека.")
    return str(value)


class MusicService:
    """Adapt API objects to small immutable objects suitable for the UI."""

    def __init__(self, token: str):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Токен не должен быть пустым.")
        self._token = token.strip()
        self._client = None
        self._uid = ""
        self._cache_lock = threading.Lock()
        self._likes_generation = 0
        self._likes_cache: tuple[float, tuple[str, ...]] | None = None
        self._likes_snapshot: tuple[str, ...] | None = None
        self._playlists_cache: dict[tuple[str, str], tuple[float, tuple[object, ...]]] = {}
        self._playlist_snapshots: dict[tuple[str, str], tuple[object, ...]] = {}
        self._view_snapshots = WeakKeyDictionary()
        self._metadata_cache: dict[str, tuple[float, Track]] = {}

    @staticmethod
    def device_login(
        on_code: Callable[[str, str], None],
        should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        """Block while the user authorizes in a browser; return the access token.

        ``on_code`` runs on this worker thread, receiving (verification URL,
        user code). Callers must marshal any GUI updates onto the GUI thread.
        """
        def run() -> str:
            client = _new_client()

            def code_callback(code: object) -> None:
                url = _https_url(getattr(code, "verification_url", None))
                host = urlsplit(url).hostname if url else None
                user_code = getattr(code, "user_code", "")
                if (not host or not (host == "yandex.ru" or host.endswith(".yandex.ru")
                                     or host == "ya.ru" or host.endswith(".ya.ru"))
                        or not isinstance(user_code, str) or not user_code.strip()
                        or len(user_code) > 64 or any(ord(ch) < 32 for ch in user_code)):
                    raise ServiceError("Получен недопустимый адрес или код авторизации.")
                on_code(url, user_code)

            code = client.request_device_code(device_name="Impuls")
            code_callback(code)
            device_code = getattr(code, "device_code", None)
            interval = getattr(code, "interval", None)
            expires = getattr(code, "expires_in", None)
            if (not isinstance(device_code, str) or not device_code or len(device_code) > 2048
                    or any(ord(char) < 32 for char in device_code)
                    or isinstance(interval, bool) or not isinstance(interval, Real)
                    or not math.isfinite(interval) or interval <= 0
                    or isinstance(expires, bool) or not isinstance(expires, Real)
                    or not math.isfinite(expires) or expires <= 0):
                raise ServiceError("Получен некорректный ответ для входа.")
            deadline = time.monotonic() + min(float(expires), _DEVICE_TIMEOUT_SECONDS)
            while True:
                if should_cancel is not None and should_cancel():
                    raise ServiceError("Вход отменён.")
                if time.monotonic() >= deadline:
                    raise ServiceError("Время входа истекло.")
                result = client.poll_device_token(device_code)
                if result is not None:
                    token = getattr(result, "access_token", None)
                    if not isinstance(token, str) or not token.strip():
                        raise ServiceError("Не удалось получить токен авторизации.")
                    return token
                next_poll = time.monotonic() + float(interval)
                while True:
                    if should_cancel is not None and should_cancel():
                        raise ServiceError("Вход отменён.")
                    remaining = min(next_poll, deadline) - time.monotonic()
                    if remaining <= 0:
                        break
                    # Keep cancellation responsive without polling the server
                    # more often than its requested interval.
                    time.sleep(min(0.1, remaining))

        return _safely("Не удалось выполнить вход. Проверьте подключение и повторите попытку.", run)

    def _connected(self):
        if self._client is None:
            raise ServiceError("Сначала войдите в аккаунт.")
        return self._client

    def connect(self) -> str:
        def run() -> str:
            client = _new_client(self._token)
            client.init()
            account = getattr(getattr(client, "me", None), "account", None)
            if account is None:
                raise ServiceError("Не удалось получить данные аккаунта.")
            uid = getattr(account, "uid", None)
            if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not re.fullmatch(r"[0-9]+", str(uid)):
                raise ServiceError("Сервис не вернул корректный идентификатор аккаунта.")
            with self._cache_lock:
                self._uid = str(uid)
                self._client = client
                self._likes_generation += 1
                self._likes_cache = None
                self._likes_snapshot = None
                self._playlists_cache.clear()
                self._playlist_snapshots.clear()
                self._view_snapshots.clear()
                self._metadata_cache.clear()
            return str(getattr(account, "login", None) or getattr(account, "display_name", None) or "Аккаунт Яндекса")

        return _safely("Не удалось войти. Проверьте токен и подключение к сети.", run)

    def playback_service(self) -> MusicService:
        """Create an independent authenticated transport for playback requests."""
        self._connected()
        def run() -> MusicService:
            result = MusicService(self._token)
            result._client = _new_client(self._token)
            result._uid = self._uid
            return result
        return _safely("Не удалось подготовить воспроизведение.", run)

    def search(self, query: str) -> list[Track]:
        if not isinstance(query, str) or not query.strip():
            return []
        client = self._connected()
        def run() -> list[Track]:
            result = client.search(query.strip(), type_="track")
            return _tracks(getattr(getattr(result, "tracks", None), "results", None))
        return _safely("Не удалось выполнить поиск.", run)

    def _liked_ids(self, client: object, should_cancel: Callable[[], bool] | None) -> tuple[str, ...]:
        _check_cancel(should_cancel)
        with self._cache_lock:
            cached, generation = self._likes_cache, self._likes_generation
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
        liked = client.users_likes_tracks(user_id=self._uid)
        _check_cancel(should_cancel)
        values = getattr(liked, "tracks_ids", None)
        if not isinstance(values, (list, tuple)):
            raise ServiceError("Сервис вернул некорректный список любимых треков.")
        identifiers = tuple(_entry_id(value) for value in values)
        with self._cache_lock:
            if client is self._client and generation == self._likes_generation:
                self._likes_cache = (time.monotonic() + _CACHE_SECONDS, identifiers)
        return identifiers

    def _playlist_entries(self, client: object, owner: str, kind: str,
                          should_cancel: Callable[[], bool] | None) -> tuple[object, ...]:
        _check_cancel(should_cancel)
        key = (owner, kind)
        with self._cache_lock:
            cached = self._playlists_cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
        data = client.users_playlists(kind, user_id=owner)
        _check_cancel(should_cancel)
        entries = getattr(data, "tracks", None)
        if not isinstance(entries, (list, tuple)):
            raise ServiceError("Сервис вернул некорректный список треков плейлиста.")
        result = tuple(entries)
        with self._cache_lock:
            if client is self._client:
                self._playlists_cache[key] = (time.monotonic() + _CACHE_SECONDS, result)
                while len(self._playlists_cache) > _PLAYLIST_CACHE_LIMIT:
                    self._playlists_cache.pop(next(iter(self._playlists_cache)))
        return result

    def _page_metadata(self, client: object, identifiers: list[str],
                       should_cancel: Callable[[], bool] | None) -> dict[str, Track]:
        """Resolve at most one page of IDs with a single network request."""
        _check_cancel(should_cancel)
        result: dict[str, Track] = {}
        missing: list[str] = []
        now = time.monotonic()
        with self._cache_lock:
            for identifier in dict.fromkeys(identifiers):
                cached = self._metadata_cache.get(identifier)
                if cached is not None and cached[0] > now:
                    result[identifier] = cached[1]
                else:
                    self._metadata_cache.pop(identifier, None)
                    missing.append(identifier)
        if not missing:
            return result
        _check_cancel(should_cancel)
        raw_tracks = client.tracks(missing)
        _check_cancel(should_cancel)
        if not isinstance(raw_tracks, (list, tuple)) or not raw_tracks:
            raise ServiceError("Сервис не вернул данные запрошенных треков.")
        returned: dict[str, Track] = {}
        for raw in raw_tracks:
            item = _track(raw)
            if item is None:
                raise ServiceError("Сервис вернул некорректные данные трека.")
            returned[item.id] = item
            returned.setdefault(item.id.split(":", 1)[0], item)
        expires = time.monotonic() + _CACHE_SECONDS
        for identifier in missing:
            item = returned.get(identifier) or returned.get(identifier.split(":", 1)[0])
            if item is not None:
                result[identifier] = item
        if not result:
            raise ServiceError("Сервис не вернул данные запрошенных треков.")
        with self._cache_lock:
            if client is self._client:
                for identifier in missing:
                    if identifier in result:
                        self._metadata_cache[identifier] = (expires, result[identifier])
                while len(self._metadata_cache) > _METADATA_CACHE_LIMIT:
                    self._metadata_cache.pop(next(iter(self._metadata_cache)))
        return result

    def liked_tracks_page(self, offset: int = 0, limit: int = 100,
                          should_cancel: Callable[[], bool] | None = None, *,
                          snapshot: CollectionSnapshot | None = None) -> tuple[list[Track], bool]:
        """Load one page from the current view's stable source-entry snapshot."""
        _page_bounds(offset, limit)
        if snapshot is not None and not isinstance(snapshot, CollectionSnapshot):
            raise ValueError("Неверный снимок коллекции.")
        client = self._connected()
        def run() -> tuple[list[Track], bool]:
            _check_cancel(should_cancel)
            key = ('likes',)
            with self._cache_lock:
                captured = self._view_snapshots.get(snapshot) if snapshot is not None else None
                identifiers = ((captured[1] if captured and captured[0] == key else None)
                               if snapshot is not None else self._likes_snapshot) if offset else None
            if identifiers is None:
                identifiers = self._liked_ids(client, should_cancel)
                with self._cache_lock:
                    if client is self._client:
                        if snapshot is None:
                            self._likes_snapshot = identifiers
                        else:
                            self._view_snapshots[snapshot] = (key, identifiers)
            page = identifiers[offset:offset + limit]
            by_id = self._page_metadata(client, list(page), should_cancel)
            return [by_id[identifier] for identifier in page if identifier in by_id], offset + limit < len(identifiers)
        return _safely("Не удалось загрузить любимые треки. Проверьте подключение.", run)

    def liked_tracks(self, should_cancel: Callable[[], bool] | None = None) -> list[Track]:
        result: list[Track] = []
        offset = 0
        while True:
            page, more = self.liked_tracks_page(offset, _BATCH_SIZE, should_cancel)
            result.extend(page)
            if not more:
                return result
            offset += _BATCH_SIZE

    def playlists(self) -> list[Playlist]:
        client = self._connected()
        def run() -> list[Playlist]:
            result = []
            values = client.users_playlists_list(user_id=self._uid)
            if not isinstance(values, (list, tuple)):
                raise ServiceError("Сервис вернул некорректный список плейлистов.")
            for value in values:
                kind = getattr(value, "kind", None)
                owner = getattr(value, "owner", None)
                owner_uid = getattr(owner, "uid", None) or getattr(owner, "id", None) or self._uid
                if kind is None or not owner_uid:
                    raise ServiceError("Сервис вернул некорректный список плейлистов.")
                if not _ID_RE.fullmatch(str(kind)) or not _ID_RE.fullmatch(str(owner_uid)):
                    raise ServiceError("Сервис вернул некорректный список плейлистов.")
                cover = getattr(value, "cover", None)
                uri = getattr(cover, "uri", None) or getattr(value, "og_image", None)
                count = getattr(value, "track_count", None)
                count = count if isinstance(count, int) and count > 0 else 0
                result.append(Playlist(str(kind), str(getattr(value, "title", None) or "Плейлист"),
                                       str(owner_uid), count, _cover_url(uri)))
            return result
        return _safely("Не удалось загрузить плейлисты.", run)

    def playlist_tracks(self, playlist: Playlist,
                        should_cancel: Callable[[], bool] | None = None) -> list[Track]:
        result: list[Track] = []
        offset = 0
        while True:
            page, more = self.playlist_tracks_page(playlist, offset, _BATCH_SIZE, should_cancel)
            result.extend(page)
            if not more:
                return result
            offset += _BATCH_SIZE

    def playlist_tracks_page(self, playlist: Playlist, offset: int = 0, limit: int = 100,
                             should_cancel: Callable[[], bool] | None = None, *,
                             snapshot: CollectionSnapshot | None = None) -> tuple[list[Track], bool]:
        """Load a playlist page using embedded data or a single metadata request."""
        _page_bounds(offset, limit)
        if snapshot is not None and not isinstance(snapshot, CollectionSnapshot):
            raise ValueError("Неверный снимок коллекции.")
        client = self._connected()
        kind = _numeric_id(playlist.id)
        owner = _numeric_id(playlist.owner_uid)
        def run() -> tuple[list[Track], bool]:
            _check_cancel(should_cancel)
            key = (owner, kind)
            snapshot_key = ('playlist', owner, kind)
            with self._cache_lock:
                captured = self._view_snapshots.get(snapshot) if snapshot is not None else None
                entries = ((captured[1] if captured and captured[0] == snapshot_key else None)
                           if snapshot is not None else self._playlist_snapshots.get(key)) if offset else None
            if entries is None:
                entries = self._playlist_entries(client, owner, kind, should_cancel)
                with self._cache_lock:
                    if client is self._client:
                        if snapshot is not None:
                            self._view_snapshots[snapshot] = (snapshot_key, entries)
                        else:
                            self._playlist_snapshots[key] = entries
                            while len(self._playlist_snapshots) > _PLAYLIST_CACHE_LIMIT:
                                self._playlist_snapshots.pop(next(iter(self._playlist_snapshots)))
            page = entries[offset:offset + limit]
            selected: list[tuple[Track | None, str | None]] = []
            missing: list[str] = []
            for entry in page:
                item = _track(getattr(entry, "track", None))
                identifier = None if item is not None else _entry_id(getattr(entry, "track_id", None))
                if identifier is not None:
                    missing.append(identifier)
                selected.append((item, identifier))
            by_id = self._page_metadata(client, missing, should_cancel)
            result = []
            for item, identifier in selected:
                track = item if item is not None else by_id.get(identifier)
                if track is not None:
                    result.append(track)
            return result, offset + limit < len(entries)
        return _safely("Не удалось загрузить треки плейлиста. Проверьте подключение.", run)

    def stream_info(self, track: Track, quality: str = 'hq') -> AudioStream:
        """Resolve a raw stream and report the actual delivered format."""
        if not isinstance(quality, str) or quality not in ('nq', 'hq', 'lossless'):
            raise ValueError("Неверное качество аудио.")
        client = self._connected()
        identifier = _numeric_id(track.id)
        def bitrate(value: object) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100000 else 0
        def actual_quality(codec: str, rate: int, declared: object) -> str:
            # Some accounts return quality=lossless for an AAC fallback. Only
            # FLAC can be reported as lossless; an unknown rate stays unknown.
            if codec in ('flac', 'flac-mp4'):
                return 'lossless'
            if declared in ('nq', 'hq'):
                return declared
            if rate:
                return 'hq' if rate >= 256 else 'nq'
            return 'unknown'
        def run() -> AudioStream:
            # Upstream's default codec list covers the supported raw formats.
            try:
                file_info = client.tracks_file_info(identifier, quality=quality, transport="raw")
                info = getattr(file_info, "download_info", None)
                codec = getattr(info, 'codec', None)
                if info is not None and getattr(info, 'transport', None) == 'raw' and codec in _AUDIO_CODECS:
                    alternatives = getattr(info, 'urls', None)
                    urls = [getattr(info, 'url', None)]
                    if isinstance(alternatives, (list, tuple)):
                        urls.extend(alternatives)
                    url = next((safe for item in urls if (safe := _music_url(item))), None)
                    if url:
                        rate = bitrate(getattr(info, 'bitrate', None))
                        return AudioStream(url, codec, rate, actual_quality(codec, rate, getattr(info, 'quality', None)))
            except Exception:
                # Older clients/accounts may still expose the legacy endpoint.
                pass
            candidates = client.tracks_download_info(identifier) or []
            candidates = [c for c in candidates if getattr(c, "codec", "") == "mp3"
                          and not getattr(c, "preview", False)
                          and _music_url(getattr(c, "download_info_url", None))]
            if quality == 'nq':
                def normal_order(item):
                    rate = bitrate(getattr(item, 'bitrate_in_kbps', None))
                    return (0, -rate) if 0 < rate <= 192 else (1, rate or 100001)
                candidates.sort(key=normal_order)
            else:
                candidates.sort(key=lambda item: bitrate(getattr(item, 'bitrate_in_kbps', None)), reverse=True)
            for candidate in candidates:
                try:
                    url = _music_url(candidate.get_direct_link(allow_redirects=False))
                except Exception:
                    continue
                if url:
                    rate = bitrate(getattr(candidate, 'bitrate_in_kbps', None))
                    return AudioStream(url, 'mp3', rate, actual_quality('mp3', rate, None))
            raise ServiceError("Трек недоступен для воспроизведения в этом аккаунте.")
        return _safely("Не удалось получить аудиопоток. Проверьте доступность трека.", run)

    def stream_url(self, track: Track) -> str:
        return self.stream_info(track).url

    def set_liked(self, track: Track, liked: bool) -> None:
        client = self._connected()
        identifier = _numeric_id(track.id).split(":", 1)[0]
        def run() -> None:
            method = client.users_likes_tracks_add if liked else client.users_likes_tracks_remove
            if not method(identifier):
                raise ServiceError("Не удалось изменить отметку «Мне нравится».")
        try:
            _safely("Не удалось изменить отметку «Мне нравится».", run)
        finally:
            # The server may have applied a change even when its response failed.
            with self._cache_lock:
                self._likes_generation += 1
                self._likes_cache = None
