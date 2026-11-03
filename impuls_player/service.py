"""Small, synchronous boundary around the unofficial Yandex Music API.

Call these methods from a worker rather than the GTK main loop. The dependency is
imported lazily so that unit tests and non-network application startup are safe.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
import math
import time
from numbers import Real
import re
import socket
from typing import Callable, TypeVar
from urllib.parse import urlsplit


_TIMEOUT_SECONDS = 15
_DEVICE_TIMEOUT_SECONDS = 300
_BATCH_SIZE = 100
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


class ServiceError(Exception):
    """A safe user-facing error that does not contain credentials or URLs."""


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


def _fetch_by_id(client: object, identifiers: list[str],
                 should_cancel: Callable[[], bool] | None = None) -> dict[str, Track]:
    """Resolve metadata in bounded batches, indexed by complete and bare ID."""
    by_id: dict[str, Track] = {}
    for start in range(0, len(identifiers), _BATCH_SIZE):
        if should_cancel is not None and should_cancel():
            raise ServiceError("Запрос отменён.")
        for item in _tracks(client.tracks(identifiers[start:start + _BATCH_SIZE])):
            by_id[item.id] = item
            by_id.setdefault(item.id.split(":", 1)[0], item)
    return by_id


class MusicService:
    """Adapt API objects to small immutable objects suitable for the UI."""

    def __init__(self, token: str):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Токен не должен быть пустым.")
        self._token = token.strip()
        self._client = None
        self._uid = ""

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
            self._uid = str(getattr(account, "uid", "") or "")
            self._client = client
            return str(getattr(account, "login", None) or getattr(account, "display_name", None) or "Аккаунт Яндекса")

        return _safely("Не удалось войти. Проверьте токен и подключение к сети.", run)

    def search(self, query: str) -> list[Track]:
        if not isinstance(query, str) or not query.strip():
            return []
        client = self._connected()
        def run() -> list[Track]:
            result = client.search(query.strip(), type_="track")
            return _tracks(getattr(getattr(result, "tracks", None), "results", None))
        return _safely("Не удалось выполнить поиск.", run)

    def liked_tracks(self, should_cancel: Callable[[], bool] | None = None) -> list[Track]:
        client = self._connected()
        def run() -> list[Track]:
            liked = client.users_likes_tracks()
            identifiers = [str(value) for value in (getattr(liked, "tracks_ids", None) or ())
                           if _ID_RE.fullmatch(str(value))]
            by_id = _fetch_by_id(client, identifiers, should_cancel)
            return [item for value in identifiers
                    if (item := by_id.get(value) or by_id.get(value.split(":", 1)[0])) is not None]
        return _safely("Не удалось загрузить любимые треки.", run)

    def playlists(self) -> list[Playlist]:
        client = self._connected()
        def run() -> list[Playlist]:
            result = []
            for value in client.users_playlists_list() or []:
                kind = getattr(value, "kind", None)
                owner = getattr(value, "owner", None)
                owner_uid = getattr(owner, "uid", None) or getattr(owner, "id", None) or self._uid
                if kind is None or not owner_uid:
                    continue
                if not _ID_RE.fullmatch(str(kind)) or not _ID_RE.fullmatch(str(owner_uid)):
                    continue
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
        client = self._connected()
        kind = _numeric_id(playlist.id)
        owner = _numeric_id(playlist.owner_uid)
        def run() -> list[Track]:
            data = client.users_playlists(kind, user_id=owner)
            entries = getattr(data, "tracks", None) or []
            missing = []
            for entry in entries:
                if getattr(entry, "track", None) is None:
                    identifier = getattr(entry, "track_id", None)
                    if identifier and _ID_RE.fullmatch(str(identifier)):
                        missing.append(str(identifier))
            by_id = _fetch_by_id(client, missing, should_cancel)
            result = []
            for entry in entries:
                item = _track(getattr(entry, "track", None))
                if item is None:
                    identifier = str(getattr(entry, "track_id", ""))
                    item = by_id.get(identifier) or by_id.get(identifier.split(":", 1)[0])
                if item:
                    result.append(item)
            return result
        return _safely("Не удалось загрузить треки плейлиста.", run)

    def stream_url(self, track: Track) -> str:
        client = self._connected()
        identifier = _numeric_id(track.id)
        def run() -> str:
            # The current API returns a direct link for unencrypted `raw` files.
            try:
                file_info = client.tracks_file_info(identifier, quality="hq", codecs=["mp3", "aac"], transport="raw")
                info = getattr(file_info, "download_info", None)
                url = _music_url(getattr(info, "url", None))
                if (info is not None and getattr(info, "transport", None) == "raw"
                        and getattr(info, "codec", None) in ("mp3", "aac") and url):
                    return url
            except Exception:
                # Older clients/accounts may still expose the legacy endpoint.
                pass
            candidates = client.tracks_download_info(identifier) or []
            candidates = [c for c in candidates if getattr(c, "codec", "") == "mp3"
                          and not getattr(c, "preview", False)
                          and _music_url(getattr(c, "download_info_url", None))]
            candidates.sort(key=lambda item: getattr(item, "bitrate_in_kbps", 0) or 0, reverse=True)
            for candidate in candidates:
                url = _music_url(candidate.get_direct_link(allow_redirects=False))
                if url:
                    return url
            raise ServiceError("Трек недоступен для воспроизведения в этом аккаунте.")
        return _safely("Не удалось получить аудиопоток. Проверьте доступность трека.", run)

    def set_liked(self, track: Track, liked: bool) -> None:
        client = self._connected()
        identifier = _numeric_id(track.id).split(":", 1)[0]
        def run() -> None:
            method = client.users_likes_tracks_add if liked else client.users_likes_tracks_remove
            if not method(identifier):
                raise ServiceError("Не удалось изменить отметку «Мне нравится».")
        _safely("Не удалось изменить отметку «Мне нравится».", run)
