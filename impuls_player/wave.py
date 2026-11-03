"""Personal Wave sessions, using the unofficial Yandex Music session API.

Methods perform synchronous network requests. Call them on a worker thread,
serializing access with the same lock used for the owning MusicService. Playback
must report events only after the audio engine starts, ends or skips a track.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import math
import re

from .service import MusicService, ServiceError, Track, _safely, _track


_SESSION_ID = re.compile(r"^[A-Za-z0-9_~.=-]{1,512}$")
_RECENT_LIMIT = 100
_TRACK_LIMIT = 500
_FEEDBACK_HISTORY_LIMIT = 2048


class WaveSession:
    """One explicitly advanced personal-wave session, without background loops."""

    def __init__(self, service: MusicService):
        self._service = service
        self._session_id: str | None = None
        # The same track can occur in multiple batches. Keep each immutable
        # Track object alive and associate its identity with its own batch.
        self._batch_for_track: OrderedDict[int, tuple[Track, str | None]] = OrderedDict()
        self._recent: deque[str] = deque(maxlen=_RECENT_LIMIT)

    def _session(self) -> str:
        if self._session_id is None:
            raise ServiceError("Сначала запустите Мою волну.")
        return self._session_id

    def _read_batch(self, response: object) -> list[Track]:
        if response is None or getattr(response, "unknown_session", False) is True:
            raise ServiceError("Сессия Моей волны завершилась. Запустите её снова.")
        raw_sequence = getattr(response, "sequence", None) or []
        if len(raw_sequence) > _TRACK_LIMIT:
            raise ServiceError("Моя волна вернула слишком много треков.")
        batch = getattr(response, "batch_id", None)
        if batch is not None and not isinstance(batch, str):
            raise ServiceError("Моя волна вернула некорректный ответ.")
        tracks = []
        for entry in raw_sequence:
            item = _track(getattr(entry, "track", None))
            if item is not None:
                tracks.append(item)
                key = id(item)
                self._batch_for_track[key] = (item, batch)
                self._batch_for_track.move_to_end(key)
        if not tracks:
            raise ServiceError("Моя волна не вернула доступных треков.")
        while len(self._batch_for_track) > _FEEDBACK_HISTORY_LIMIT:
            self._batch_for_track.popitem(last=False)
        return tracks

    def start(self) -> list[Track]:
        """Create the user's wave and return its first batch of tracks."""
        self._session_id = None
        self._batch_for_track.clear()
        self._recent.clear()
        client = self._service._connected()
        def run() -> list[Track]:
            response = client.rotor_session_new(["user:onyourwave"])
            session_id = getattr(response, "radio_session_id", None)
            if (not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
                    or ".." in session_id):
                raise ServiceError("Не удалось создать сессию Моей волны.")
            tracks = self._read_batch(response)
            if not client.rotor_session_feedback_radio_started(session_id, getattr(response, "batch_id", None)):
                raise ServiceError("Не удалось запустить Мою волну.")
            self._session_id = session_id
            return tracks
        return _safely("Не удалось запустить Мою волну.", run)

    def next_batch(self) -> list[Track]:
        """Fetch one additional batch; an empty/expired session is an error."""
        session_id = self._session()
        client = self._service._connected()
        return _safely("Не удалось получить продолжение Моей волны.",
                       lambda: self._read_batch(client.rotor_session_tracks(session_id, queue=list(self._recent))))

    def _batch(self, track: Track) -> str | None:
        self._session()
        entry = self._batch_for_track.get(id(track)) if isinstance(track, Track) else None
        if entry is None or entry[0] is not track:
            raise ServiceError("Этот трек не относится к текущей сессии Моей волны.")
        return entry[1]

    def track_started(self, track: Track) -> bool:
        """Report that audio for the given session track has begun playing."""
        batch = self._batch(track)
        session_id = self._session()
        client = self._service._connected()
        self._recent.append(track.id)
        return _safely("Не удалось сообщить о начале трека Моей волны.",
                       lambda: bool(client.rotor_session_feedback_track_started(session_id, track.id, batch)))

    def _finish(self, track: Track, played_seconds: float, *, skipped: bool) -> bool:
        batch = self._batch(track)
        if (not isinstance(played_seconds, (int, float)) or isinstance(played_seconds, bool)
                or not math.isfinite(played_seconds) or played_seconds < 0):
            raise ValueError("Время прослушивания должно быть неотрицательным числом.")
        session_id = self._session()
        client = self._service._connected()
        method = client.rotor_session_feedback_skip if skipped else client.rotor_session_feedback_track_finished
        return _safely("Не удалось отправить событие Моей волны.",
                       lambda: bool(method(session_id, track.id, float(played_seconds), batch)))

    def track_finished(self, track: Track, played_seconds: float) -> bool:
        """Report a naturally completed track and the time actually heard."""
        return self._finish(track, played_seconds, skipped=False)

    def track_skipped(self, track: Track, played_seconds: float) -> bool:
        """Report a user skip and the time actually heard before the skip."""
        return self._finish(track, played_seconds, skipped=True)
