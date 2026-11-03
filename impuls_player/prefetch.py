"""Bounded, credential-free audio download for a single upcoming track."""

from __future__ import annotations

from io import BytesIO
import logging
import math
from numbers import Real
import re
import time
from typing import Callable

import requests

from .service import _music_url


_MAX_BYTES = 64 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 5.0
_DEFAULT_TIMEOUT = 120.0


class PrefetchError(Exception):
    """Safe, user-facing failure without a signed URL or credentials."""


def download_audio(url: str, should_cancel: Callable[[], bool] | None = None,
                   *, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Read a complete HTTPS audio file into bounded memory without redirects.

    The caller owns concurrency and should run this blocking operation in a
    worker. Cancellation is checked before the request and between reads;
    a pending socket read is additionally limited by the read timeout.
    """
    if not _music_url(url):
        raise PrefetchError("Недопустимый адрес аудиофайла.")
    if (isinstance(timeout, bool) or not isinstance(timeout, Real)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Неверный таймаут загрузки.")
    deadline = time.monotonic() + float(timeout)

    def check() -> float:
        if should_cancel is not None and should_cancel():
            raise PrefetchError("Загрузка аудио отменена.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PrefetchError("Время загрузки аудио истекло.")
        return remaining

    session = None
    response = None
    try:
        remaining = check()
        # urllib3 DEBUG messages include the full request path and query,
        # which can contain signed CDN credentials.
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
        session = requests.Session()
        # A fresh isolated session must not inherit netrc credentials, proxy
        # authentication, application OAuth headers, or cookies from the API.
        session.trust_env = False
        session.auth = None
        session.headers.clear()
        response = session.get(
            url,
            headers={"Accept-Encoding": "identity"},
            stream=True,
            allow_redirects=False,
            verify=True,
            timeout=(min(_CONNECT_TIMEOUT, remaining), min(_READ_TIMEOUT, remaining)),
        )
        check()
        if response.status_code != 200:
            raise PrefetchError("Сервис не вернул аудиофайл.")
        encoding = response.headers.get("Content-Encoding", "identity")
        if not isinstance(encoding, str) or encoding.lower().strip() not in ("", "identity"):
            raise PrefetchError("Получен неподдерживаемый формат передачи аудио.")
        content_length = response.headers.get("Content-Length")
        if not isinstance(content_length, str) or not re.fullmatch(r"[0-9]+", content_length):
            raise PrefetchError("Сервис не сообщил корректный размер аудио.")
        expected = int(content_length)
        if expected < 1 or expected > _MAX_BYTES:
            raise PrefetchError("Размер аудиофайла превышает допустимый предел.")

        with BytesIO() as data:
            count = 0
            while True:
                check()
                # read1 returns after at most one underlying read. This allows
                # the deadline and cancellation to be checked even when a
                # server delivers very small fragments slowly.
                chunk = response.raw.read1(min(_CHUNK_SIZE, expected - count + 1), decode_content=False)
                check()
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise PrefetchError("Получены некорректные данные аудио.")
                count += len(chunk)
                if count > expected or count > _MAX_BYTES:
                    raise PrefetchError("Получен некорректный размер аудио.")
                data.write(chunk)
            if count != expected:
                raise PrefetchError("Аудиофайл получен не полностью.")
            return data.getvalue()
    except PrefetchError:
        raise
    except Exception:
        raise PrefetchError("Не удалось загрузить аудио. Проверьте подключение.") from None
    finally:
        for resource in (response, session):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
