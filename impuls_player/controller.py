"""Coordinates the UI, player and service without blocking the UI thread."""

from concurrent.futures import ThreadPoolExecutor
import random
from threading import Event, RLock
from time import monotonic

from .queue import PlaybackQueue
from .player import _normalize_equalizer
from .prefetch import download_audio
from .presets import EQUALIZER_PRESETS
from .wave import WaveSession
from .service import CollectionSnapshot, ServiceError

_CANCELLED = object()
_MAX_PREFETCH_BYTES = 64 * 1024 * 1024


class Controller:
    def __init__(self, window, player, store, service_factory, executor=None, dispatch=None):
        self.window = window
        self.player = player
        self.store = store
        self.service_factory = service_factory
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix='impuls')
        self._api_executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='impuls-api')
        self._stream_executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix='impuls-stream')
        self._prefetch_executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='impuls-prefetch')
        self._prefetch_cancel = Event()
        self._prefetch_generation = 0
        self._prefetch_inflight = False
        self._prefetch_attempt = None
        self._prefetched_audio = None
        self._download_complete_play = None
        self._using_buffer = False
        self._quality = 'hq'
        self._progress_play = None
        self._progress_since = None
        self._progress_position = 0
        self._last_position_ms = 0
        self._recovery_attempts = 0
        self._page_loader = None
        self._queue_source = None
        self._queue_page_offset = 0
        self._queue_page_more = False
        self._queue_page_loading = False
        self._queue_page_generation = 0
        self._page_offset = 0
        self._page_loading = False
        self._page_more = False
        self._page_advance_pending = False
        self._page_prefetch_failed_play = None
        self._storage_executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='impuls-secrets')
        self.wave_factory = WaveSession
        self._wave = None
        self._wave_tracks = []
        self._wave_current = None
        self._wave_started = False
        self._wave_since = None
        self._wave_seconds = 0.0
        self._wave_batch_pending = False
        self._wave_fetching = False
        self._wave_fetch_generation = 0
        self._wave_fetch_failed_play = None
        self._wave_fetch_view = None
        self._wave_queued_batch = None
        self._wave_queued_view = None
        self._loading_track = False
        self._desired_playing = False
        self.dispatch = dispatch or (lambda callback: callback())
        self._service = None
        self._queue = PlaybackQueue()
        self._shuffle_enabled = False
        self._repeat_enabled = False
        self._visible_tracks = []
        self._visible_wave = False
        self._session = 0
        self._view = 0
        self._view_request = None
        self._wave_request = 0
        self._play = 0
        self._pending = 0
        self._closed = False
        self._auth_cancel = Event()
        self._api_lock = RLock()
        self._credential_lock = RLock()
        self._storage_lock = RLock()
        available = bool(getattr(self.player, 'equalizer_available', False))
        set_available = getattr(self.window, 'set_equalizer_available', None)
        if set_available is not None:
            set_available(available)
        elif not available:
            self.window.set_status('Эквалайзер недоступен: установите gstreamer1-plugins-good.')

    def actions(self):
        return {name: getattr(self, name) for name in (
            'login', 'device_login', 'logout', 'search', 'wave', 'liked_tracks', 'playlists',
            'open_playlist', 'play_track', 'toggle_playback', 'next_track',
            'previous_track', 'seek', 'set_volume', 'set_equalizer', 'equalizer_preset',
            'set_shuffle', 'set_repeat', 'set_liked', 'load_more', 'set_quality',
            'refresh_view', 'play_all')}

    def _submit(self, task, success, error_message, valid=lambda: True, *, api=False, storage=False, essential=False, stream=False, failure=None):
        if self._closed:
            return
        self._pending += 1
        self.window.set_busy(True)
        def guarded():
            if (self._closed and not essential) or not valid():
                return _CANCELLED
            return task()
        pool = self._stream_executor if stream else self._storage_executor if storage else self._api_executor if api else self.executor
        future = pool.submit(guarded)

        def complete(finished):
            def deliver():
                if self._closed:
                    return False
                self._pending = max(0, self._pending - 1)
                self.window.set_busy(self._pending > 0)
                if not valid():
                    return False
                try:
                    result = finished.result()
                    if result is not _CANCELLED:
                        success(result)
                except Exception as exc:
                    if failure is not None:
                        failure()
                    # Service exceptions may contain URLs, tokens or account data.
                    if callable(error_message):
                        error_message()
                    elif error_message:
                        self.window.show_error(f"{error_message} {exc}" if isinstance(exc, ServiceError) else error_message)
                return False
            self.dispatch(deliver)
        future.add_done_callback(complete)

    def _new_session(self):
        self._cancel_prefetch()
        self._download_complete_play = None
        self._using_buffer = False
        self._auth_cancel.set()
        self._auth_cancel = Event()
        with self._credential_lock:
            self._session += 1
            current = self._session
        self._view += 1
        self._view_request = None
        self._play += 1
        self._service = None
        self._visible_tracks = []
        self._visible_wave = False
        self._queue.clear()
        self._page_loader = None
        self._queue_source = None
        self._queue_page_more = False
        self._queue_page_loading = False
        self._queue_page_generation += 1
        self._page_loading = False
        self._page_more = False
        self._page_advance_pending = False
        self._page_prefetch_failed_play = None
        self.window.set_pagination(False)
        self._wave = None
        self._wave_tracks = []
        self._wave_current = None
        self._wave_started = False
        self._wave_since = None
        self._wave_seconds = 0.0
        self._wave_batch_pending = False
        self._wave_fetch_generation += 1
        self._wave_fetching = False
        self._wave_fetch_failed_play = None
        self._wave_fetch_view = None
        self._wave_queued_batch = None
        self._wave_queued_view = None
        self._loading_track = False
        self._desired_playing = False
        self.player.stop()
        self.window.set_account(None)
        self.window.set_now_playing(None)
        self.window.set_playback(False)
        self.window.set_tracks([], 'Коллекция')
        return current

    def _persist(self, session, token, remember):
        # Serialise keyring operations without making the GTK thread wait for
        # a locked keyring or a desktop authentication prompt.
        with self._storage_lock:
            with self._credential_lock:
                if session != self._session:
                    return
            if remember:
                self.store.save(token)
            else:
                self.store.clear()

    def _connected(self, result, session, remember):
        service, name, token = result
        self._service = service
        self.window.set_account(name)
        self.window.set_status('Вы вошли в аккаунт')
        self._submit(lambda: self._persist(session, token, remember), lambda _: None,
                     'Не удалось изменить данные в системном хранилище секретов.',
                     lambda: session == self._session, storage=True)

    def login(self, token, remember):
        token = token.strip()
        if not token:
            self.window.show_error('Введите токен или воспользуйтесь входом по коду.')
            return
        session = self._new_session()
        self.window.set_status('Подключение к Яндекс Музыке…')
        def work():
            service = self.service_factory(token)
            return service, service.connect(), token
        self._submit(work, lambda result: self._connected(result, session, remember),
                     'Не удалось войти. Проверьте подключение и данные для входа.',
                     lambda: session == self._session)

    def device_login(self, remember):
        session = self._new_session()
        cancel = self._auth_cancel
        self.window.set_status('Получаем код для входа…')
        def code_received(url, code):
            def show():
                if not self._closed and session == self._session:
                    self.window.show_device_code(code, url)
                return False
            self.dispatch(show)
        def work():
            token = self.service_factory.device_login(code_received, should_cancel=cancel.is_set)
            if cancel.is_set():
                raise RuntimeError('cancelled')
            service = self.service_factory(token)
            return service, service.connect(), token
        self._submit(work, lambda result: self._connected(result, session, remember),
                     'Не удалось войти по коду. Повторите попытку или используйте токен.',
                     lambda: session == self._session)

    def restore(self):
        session = self._session
        def loaded(token):
            if token:
                self.login(token, True)
        self._submit(self.store.load, loaded, None,
                     lambda: session == self._session)

    def logout(self):
        self._new_session()
        self.window.set_status('Вы вышли из аккаунта')
        def clear_saved():
            # This deletion must not be cancelled by a subsequent login. The
            # serial storage executor places any later save after the deletion.
            with self._storage_lock:
                self.store.clear()
        self._submit(clear_saved, lambda _: None,
                     'Не удалось удалить токен из системного хранилища секретов.',
                     storage=True, essential=True)

    def _load(self, operation, heading, playlists=False, refresh=False):
        service = self._service
        if service is None:
            self.window.set_status('Войдите, чтобы открыть коллекцию.')
            return
        self._view += 1
        self._page_loader = None
        self._page_loading = False
        self._visible_tracks = []
        self._visible_wave = False
        self.window.set_pagination(False)
        view, session = self._view, self._session
        self.window.show_loading(heading)
        self.window.set_status('Загрузка…')
        def work():
            with self._api_lock:
                if refresh:
                    service.invalidate_cache()
                return operation(service, lambda: view != self._view
                                 or session != self._session or self._closed)
        def done(result):
            self._visible_wave = False
            if playlists:
                self._visible_tracks = []
                self.window.set_playlists(result)
            else:
                self._visible_tracks = list(result)
                self.window.set_tracks(self._visible_tracks, heading)
            self.window.set_status(f'{heading}: {len(result)}')
        def failed():
            self.window.show_load_error()
        self._submit(work, done, 'Не удалось загрузить данные. Проверьте подключение.',
                     lambda: view == self._view and session == self._session, api=True, failure=failed)
        self._resume_queue_after_navigation()

    def _load_page(self, loader, heading, refresh=False):
        if self._service is None:
            self.window.set_status('Войдите, чтобы открыть коллекцию.')
            return
        self._view += 1
        if refresh:
            original_loader = loader
            def loader(service, offset, cancel):
                if offset == 0:
                    service.invalidate_cache()
                return original_loader(service, offset, cancel)
        self._page_loader = loader
        self._page_heading = heading
        self._page_offset = 0
        self._page_loading = False
        self._page_more = True
        self._visible_tracks = []
        self._visible_wave = False
        self.window.show_loading(heading)
        self.load_more()
        self._resume_queue_after_navigation()

    def load_more(self, background=False):
        if self._page_loader is None or self._page_loading or not self._page_more:
            return
        loader, service = self._page_loader, self._service
        view, session, offset = self._view, self._session, self._page_offset
        self._page_loading = True
        self.window.set_pagination(True, loading=True)
        if not background:
            self.window.set_status('Загрузка…')
        valid = lambda: view == self._view and session == self._session and loader is self._page_loader
        was_waiting = False
        def work():
            with self._api_lock:
                return loader(service, offset, lambda: not valid() or self._closed)
        def done(result):
            tracks, has_more = result
            self._page_loading = False
            self._page_offset = offset + 100
            self._page_more = has_more
            self._visible_tracks.extend(tracks)
            if offset and self._queue_source is loader and self._queue.current is not None:
                previous_next = self._queue.peek_next()
                self._queue.append_tracks(tracks)
                self._queue_page_offset = offset + 100
                self._queue_page_more = has_more
                if self._queue.peek_next() != previous_next:
                    self._cancel_prefetch()
            if offset == 0:
                self.window.set_tracks(tracks, self._page_heading)
            else:
                self.window.append_tracks(tracks)
            self.window.set_pagination(has_more)
            self.window.set_status(f'{self._page_heading}: загружено {len(self._visible_tracks)}' + (' · есть ещё' if has_more else ''))
            if self._page_advance_pending and self._queue_source is loader:
                if tracks or not has_more:
                    self._page_advance_pending = False
                    self._advance(False, autoplay=self._desired_playing)
                else:
                    # A page can contain only unavailable tracks; continue
                    # until the next playable entry or the actual list end.
                    self.load_more()
            elif self._download_complete_play == self._play:
                self._prepare_prefetch()
        def failed():
            nonlocal was_waiting
            was_waiting = self._page_advance_pending
            self._page_loading = False
            self._cancel_page_advance()
            self.window.set_pagination(True, error=True)
            if background and not was_waiting:
                self._page_prefetch_failed_play = self._play
                return
            message = 'Не удалось загрузить коллекцию. Можно повторить загрузку.'
            if offset == 0:
                self.window.show_load_error(message)
            else:
                self.window.set_status(message)
        def background_error():
            if was_waiting:
                self.window.show_error('Не удалось загрузить коллекцию.')
        self._submit(work, done, background_error if background else 'Не удалось загрузить коллекцию.',
                     valid, api=True, failure=failed)

    def _resume_queue_after_navigation(self):
        if self._queue_source is None or self._queue_source is self._page_loader:
            return
        if self._page_advance_pending:
            self._load_queue_page()
        elif self._download_complete_play == self._play:
            self._prepare_prefetch()

    def _load_queue_page(self, background=False):
        """Extend the playing queue without replacing the currently viewed list."""
        if (self._queue_source is None or not self._queue_page_more
                or self._queue_page_loading or self._service is None):
            return
        loader, service = self._queue_source, self._service
        offset, session, generation = self._queue_page_offset, self._session, self._queue_page_generation
        self._queue_page_loading = True
        valid = lambda: (not self._closed and self._session == session
                         and self._queue_source is loader and self._queue_page_generation == generation)
        def work():
            with self._api_lock:
                return loader(service, offset, lambda: not valid())
        def done(result):
            tracks, has_more = result
            self._queue_page_loading = False
            self._queue_page_offset = offset + 100
            self._queue_page_more = has_more
            previous_next = self._queue.peek_next()
            self._queue.append_tracks(tracks)
            if self._queue.peek_next() != previous_next:
                self._cancel_prefetch()
            if self._page_advance_pending:
                if tracks or not has_more:
                    self._page_advance_pending = False
                    self._advance(False, autoplay=self._desired_playing)
                else:
                    self._load_queue_page()
            elif self._download_complete_play == self._play:
                self._prepare_prefetch()
        def failed():
            self._queue_page_loading = False
            was_waiting = self._page_advance_pending
            self._cancel_page_advance()
            self._page_prefetch_failed_play = self._play
            if was_waiting or not background:
                self.window.show_error('Не удалось продолжить очередь. Попробуйте ещё раз.')
        self._submit(work, done, None, valid, api=True, failure=failed)

    def search(self, query, *, refresh=False):
        query = query.strip()
        if query:
            self._view_request = ('search', query)
            self._load(lambda service, cancel: service.search(query), 'Результаты поиска', refresh=refresh)

    def refresh_view(self):
        """Reload the viewed collection while retaining the sounding queue."""
        if self._service is None:
            self.window.set_status('Войдите, чтобы обновить коллекцию.')
            return
        request = self._view_request
        if request is None:
            self.window.set_status('Откройте поиск, любимые треки или плейлист для обновления.')
            return
        kind, *args = request
        operation = {'search': self.search, 'liked_tracks': self.liked_tracks,
                     'playlists': self.playlists, 'open_playlist': self.open_playlist}[kind]
        operation(*args, refresh=True)

    def play_all(self, shuffle=False):
        """Start the visible list, keeping duplicate occurrences distinct."""
        if self._service is None:
            self.window.set_status('Войдите, чтобы слушать музыку.')
            return
        if not isinstance(shuffle, bool):
            return
        if not self._visible_tracks:
            self.window.set_status('В текущем списке нет треков для воспроизведения.')
            return
        is_wave = self._wave is not None and self._visible_wave
        if not is_wave:
            self.set_shuffle(shuffle)
        index = random.randrange(len(self._visible_tracks)) if shuffle and not is_wave else 0
        self.play_track(self._visible_tracks[index], index=index)

    def wave(self):
        service = self._service
        if service is None:
            self.window.set_status('Войдите, чтобы включить Мою волну.')
            return
        self._view += 1
        self._view_request = None
        self._page_loader = None
        self._page_loading = False
        self.window.set_pagination(False)
        self._wave_request += 1
        view, session, request = self._view, self._session, self._wave_request
        wave = self.wave_factory(service)
        self.window.set_status('Запускаем Мою волну…')
        def ready(tracks):
            self._finish_wave_track(False)
            self._wave = wave
            self._start_wave_batch(tracks)
        self._submit(wave.start, ready, 'Не удалось запустить Мою волну.',
                     lambda: view == self._view and session == self._session
                     and request == self._wave_request, api=True)
        self._resume_queue_after_navigation()

    def _start_wave_batch(self, tracks, autoplay=True, update_view=True, continuation=False):
        if not tracks:
            raise ValueError('Empty wave batch')
        self._wave_tracks = list(tracks)
        if update_view:
            self._visible_tracks = list(tracks)
            self._visible_wave = True
            self.window.set_tracks(self._visible_tracks, 'Моя волна')
        self._configure_queue_modes(is_wave=True)
        if continuation:
            self._queue.append_wave(self._wave_tracks)
            selected = self._queue.next()
        else:
            self._wave_fetch_generation += 1
            self._wave_fetching = False
            self._wave_fetch_failed_play = None
            self._wave_queued_batch = None
            self._wave_queued_view = None
            self._queue_source = None
            self._queue_page_loading = False
            self._queue_page_generation += 1
            selected = self._wave_tracks[0]
            self._queue.replace(self._wave_tracks, selected)
        self._start(selected, autoplay=autoplay)

    def _wave_feedback(self, method, *args):
        wave, session = self._wave, self._session
        if wave is not None:
            def warn():
                if wave is self._wave:
                    self.window.set_status('Не удалось отправить обратную связь Моей волне.')
            def done(result):
                if result is False:
                    warn()
            self._submit(lambda: getattr(wave, method)(*args), done, warn,
                         lambda: session == self._session, api=True)

    def playback_state(self, playing):
        if self._wave is None or self._wave_current is None:
            return
        if playing:
            if not self._wave_started:
                self._wave_started = True
                self._wave_feedback('track_started', self._wave_current)
            if self._wave_since is None:
                self._wave_since = monotonic()
        elif self._wave_since is not None:
            self._wave_seconds += max(0.0, monotonic() - self._wave_since)
            self._wave_since = None

    def _finish_wave_track(self, finished):
        if self._wave is None or self._wave_current is None:
            return
        self.playback_state(False)
        if self._wave_started:
            self._wave_feedback('track_finished' if finished else 'track_skipped',
                                self._wave_current, self._wave_seconds)
        self._wave_current = None
        self._wave_started = False
        self._wave_seconds = 0.0
        self._wave_since = None

    def liked_tracks(self, *, refresh=False):
        self._view_request = ('liked_tracks',)
        snapshot = CollectionSnapshot()
        self._load_page(lambda service, offset, cancel: service.liked_tracks_page(offset=offset, limit=100, should_cancel=cancel, snapshot=snapshot), 'Мне нравится', refresh=refresh)

    def playlists(self, *, refresh=False):
        self._view_request = ('playlists',)
        self._load(lambda service, cancel: service.playlists(), 'Плейлисты', playlists=True, refresh=refresh)

    def open_playlist(self, playlist, *, refresh=False):
        self._view_request = ('open_playlist', playlist)
        snapshot = CollectionSnapshot()
        self._load_page(lambda service, offset, cancel: service.playlist_tracks_page(playlist, offset=offset, limit=100, should_cancel=cancel, snapshot=snapshot), playlist.title, refresh=refresh)

    def play_track(self, track, index=None):
        if self._service is None:
            self.window.set_status('Войдите, чтобы слушать музыку.')
            return
        queued_wave = self._wave_queued_batch if self._wave is not None and self._visible_wave else None
        self._cancel_prefetch()
        # Selecting a loaded row preserves an in-flight page of this collection.
        if self._page_loader is None:
            self._view += 1
        if self._wave_fetching and self._wave is not None and self._visible_wave:
            # Selecting within the visible Wave supersedes view requests but
            # does not make its already-requested next batch a different view.
            self._wave_fetch_view = self._view
        self._wave_request += 1
        self._queue_source = self._page_loader
        self._queue_page_offset = self._page_offset
        self._queue_page_more = bool(self._page_loader is not None and self._page_more)
        self._queue_page_loading = False
        self._queue_page_generation += 1
        if queued_wave is None:
            self._wave_queued_batch = None
            self._wave_queued_view = None
        else:
            self._wave_queued_view = self._view
        self._finish_wave_track(False)
        if self._wave is not None and not self._visible_wave:
            self._wave = None
            self._wave_tracks = []
        self._configure_queue_modes(is_wave=self._wave is not None)
        self._queue.replace(self._visible_tracks, track, index=index)
        if queued_wave is not None:
            self._queue.append_wave(queued_wave)
        self._start(track)

    def _cancel_prefetch(self):
        self._prefetch_cancel.set()
        self._prefetch_cancel = Event()
        self._prefetch_generation += 1
        self._prefetch_inflight = False
        self._prefetch_attempt = None
        self._prefetched_audio = None

    def set_quality(self, quality):
        if not isinstance(quality, str) or quality not in ('nq', 'hq', 'lossless') or quality == self._quality:
            return
        self._quality = quality
        self._cancel_prefetch()
        self.window.set_status('Качество изменится со следующего трека.')
        self._prepare_prefetch()

    def _show_stream_quality(self, stream):
        self.window.set_stream_quality(stream.codec, stream.bitrate)

    def current_download_complete(self):
        """Request the next audio only after the current source is complete."""
        if (self._closed or self._service is None or not self.player.has_source
                or self._queue.current is None):
            return
        self._download_complete_play = self._play
        self._prepare_prefetch()

    def _prepare_prefetch(self):
        if (self._closed or self._service is None or not self.player.has_source
                or self._download_complete_play != self._play
                or self._prefetch_inflight or self._prefetched_audio is not None):
            return
        if self._wave is not None and not self._queue.has_next:
            if not self._wave_fetching and self._wave_fetch_failed_play != self._play:
                self._request_wave_batch(background=True)
            return
        if self._queue_source is not None and self._queue_page_more and not self._queue.has_next:
            if self._page_prefetch_failed_play != self._play:
                if self._queue_source is self._page_loader:
                    self.load_more(background=True)
                else:
                    self._load_queue_page(background=True)
            return
        track = self._queue.peek_next()
        if track is None or self._prefetch_attempt == (self._play, track):
            return
        service, session, play, quality = self._service, self._session, self._play, self._quality
        generation, cancel = self._prefetch_generation, self._prefetch_cancel
        self._prefetch_attempt = (play, track)
        self._prefetch_inflight = True

        def valid():
            return (not self._closed and not cancel.is_set() and generation == self._prefetch_generation
                    and session == self._session and play == self._play)

        def work():
            if not valid():
                return None
            # Playback gets its own transport. Neither client nor signed URL is
            # retained after this worker completes.
            client = service.playback_service()
            stream = client.stream_info(track, quality)
            if not valid():
                return None
            return download_audio(stream.url, should_cancel=cancel.is_set), (stream.codec, stream.bitrate)

        def completed(future):
            def deliver():
                if generation != self._prefetch_generation:
                    return False
                self._prefetch_inflight = False
                if not valid() or self._queue.peek_next() != track:
                    return False
                try:
                    result = future.result()
                except Exception:
                    # A background optimization must not interrupt playback or
                    # expose signed URLs from an exception.
                    return False
                if result is not None:
                    audio, format_info = result
                    if isinstance(audio, bytes) and 0 < len(audio) <= _MAX_PREFETCH_BYTES:
                        self._prefetched_audio = (session, play, track, audio, format_info)
                return False
            self.dispatch(deliver)
        self._prefetch_executor.submit(work).add_done_callback(completed)

    def audio_error(self):
        """Recover a failed source through a fresh URL, with a bounded retry count."""
        if (self._service is None or self._queue.current is None
                or not (self._desired_playing or self._using_buffer)):
            return False
        return self._recover_audio(self._last_position_ms, autoplay=self._desired_playing)

    def _recover_audio(self, position, *, autoplay=True):
        if self._recovery_attempts >= 2:
            self._desired_playing = False
            self.player.stop()
            return False
        self._recovery_attempts += 1
        self._start(self._queue.current, autoplay=autoplay, start_ms=max(0, position), recovering=True)
        self.window.set_status(f'Восстановление потока… {self._recovery_attempts}/2')
        return True

    def monitor_playback(self, position, duration):
        """Detect a stalled PLAYING pipeline; silent audio still advances time."""
        if (self._closed or self.playback_pending or not self._desired_playing
                or not self.player.has_source or self._queue.current is None):
            self._progress_since = None
            return
        now = monotonic()
        self._last_position_ms = position
        if (self._progress_play != self._play or self._progress_since is None
                or abs(position - self._progress_position) >= 200):
            self._progress_play, self._progress_position, self._progress_since = self._play, position, now
        elif now - self._progress_since >= 12:
            if not self._recover_audio(position):
                self.window.show_error('Поток прерван. Проверьте подключение и запустите трек снова.')

    def _start(self, track, autoplay=True, *, start_ms=0, recovering=False):
        service = self._service
        if service is None or track is None:
            return
        cached = self._prefetched_audio
        audio = (cached[3] if not recovering and not start_ms and cached is not None
                 and cached[:3] == (self._session, self._play, track)
                 else None)
        self._cancel_prefetch()
        self._download_complete_play = None
        self._page_prefetch_failed_play = None
        self._using_buffer = False
        self._progress_since = None
        self._last_position_ms = start_ms
        if not recovering:
            self._recovery_attempts = 0
        self._play += 1
        play, session = self._play, self._session
        quality = self._quality
        self._wave_batch_pending = False
        self._page_advance_pending = False
        self._loading_track = True
        self._desired_playing = bool(autoplay)
        self.player.stop()
        self._wave_current = track if self._wave is not None else None
        if self._wave_queued_batch and track is self._wave_queued_batch[0]:
            if self._visible_wave and self._view == self._wave_queued_view:
                self._visible_tracks = list(self._wave_queued_batch)
                self.window.set_tracks(self._visible_tracks, 'Моя волна')
            self._wave_queued_batch = None
            self._wave_queued_view = None
        self.window.set_now_playing(track)
        self.window.set_playback(False)
        self.window.set_status('Подготовка трека…')
        if audio is not None:
            try:
                self.player.play_buffer(audio, autoplay=self._desired_playing)
            except Exception:
                # A cached file is an optimization; retry through normal HTTPS.
                pass
            else:
                self.window.set_stream_quality(*cached[4])
                self._using_buffer = True
                self._loading_track = False
                self.window.set_status('Воспроизведение' if self._desired_playing else 'Пауза')
                return
        def work():
            return service.playback_service().stream_info(track, quality)
        def ready(stream):
            self._loading_track = False
            if start_ms:
                self.player.play_url(stream.url, autoplay=self._desired_playing, start_ms=start_ms)
            else:
                self.player.play_url(stream.url, autoplay=self._desired_playing)
            self._show_stream_quality(stream)
            self.window.set_status('Воспроизведение' if self._desired_playing else 'Пауза')
        def failed():
            self._loading_track = False
            if recovering and self._recover_audio(self._last_position_ms, autoplay=self._desired_playing):
                return
            self._desired_playing = False
            self.window.show_error('Трек недоступен: проверьте подписку, регион и подключение.')
        self._submit(work, ready, failed,
                     lambda: play == self._play and session == self._session, stream=True)

    @property
    def playback_pending(self):
        return self._loading_track or self._wave_batch_pending or self._page_advance_pending

    @property
    def display_playing(self):
        return self._desired_playing if self.playback_pending else self.player.playing

    @property
    def current_track(self):
        return self._queue.current

    def stop_playback(self):
        self._cancel_prefetch()
        self._download_complete_play = None
        self._using_buffer = False
        self._queue_page_generation += 1
        self._queue_page_loading = False
        self._wave_fetch_generation += 1
        self._wave_fetching = False
        self._wave_request += 1
        self._finish_wave_track(False)
        self._play += 1
        self._wave_batch_pending = False
        self._page_advance_pending = False
        self._loading_track = False
        self._desired_playing = False
        self.player.stop()
        self.window.set_playback(False)

    def restart_current(self):
        if self._queue.current is not None:
            self._finish_wave_track(False)
            self._start(self._queue.current)

    def set_playing(self, playing):
        """Set playback intent idempotently, including while a URL is loading."""
        if not isinstance(playing, bool):
            return
        if self.playback_pending:
            self._desired_playing = playing
        elif self.player.has_source:
            # GStreamer can synchronously report a state-change failure from
            # toggle(); recovery must observe the user's latest intent.
            self._desired_playing = playing
            if self.player.playing != playing:
                self.player.toggle()
        elif playing and self._queue.current is not None:
            self.restart_current()

    def toggle_playback(self):
        if not self.player.has_source and not self.playback_pending:
            self.set_playing(True)
        else:
            self.set_playing(not self.display_playing)

    def _advance(self, finished, autoplay=True):
        if self._wave_batch_pending or self._page_advance_pending:
            return
        self._finish_wave_track(finished)
        if (self._wave is None and self._queue_source is not None
                and self._queue_page_more and not self._queue.has_next):
            self._play += 1
            self._loading_track = False
            self._desired_playing = bool(autoplay)
            self._page_advance_pending = True
            self.player.stop()
            self.window.set_playback(False)
            if self._queue_source is self._page_loader:
                self.load_more()
            else:
                self._load_queue_page()
            return
        track = self._queue.next()
        if track is not None:
            self._start(track, autoplay=autoplay)
            return
        self._play += 1
        self._cancel_prefetch()
        self._download_complete_play = None
        self._using_buffer = False
        self._loading_track = False
        self.player.stop()
        self.window.set_playback(False)
        if self._wave is None:
            self._desired_playing = False
            self.window.set_status('Очередь закончилась')
            return
        self._desired_playing = bool(autoplay)
        self._wave_batch_pending = True
        self.window.set_status('Продолжаем Мою волну…')
        self._request_wave_batch()

    def _request_wave_batch(self, background=False):
        if self._wave is None or self._wave_fetching:
            return
        wave, session, generation = self._wave, self._session, self._wave_fetch_generation
        self._wave_fetching = True
        self._wave_fetch_view = self._view
        valid = lambda: (wave is self._wave and session == self._session
                         and generation == self._wave_fetch_generation)
        def ready(tracks):
            if not tracks:
                raise ValueError('Empty wave batch')
            self._wave_fetching = False
            self._wave_tracks = list(tracks)
            self._queue.append_wave(tracks)
            self._wave_queued_batch = tuple(tracks)
            self._wave_queued_view = self._wave_fetch_view
            if self._wave_batch_pending:
                self._wave_batch_pending = False
                self._start(self._queue.next(), autoplay=self._desired_playing)
            else:
                self._prepare_prefetch()
        def failed():
            self._wave_fetching = False
            waiting = self._wave_batch_pending
            self._wave_batch_pending = False
            self._wave_fetch_failed_play = self._play
            if waiting or not background:
                self.window.show_error('Не удалось продолжить Мою волну.')
        self._submit(wave.next_batch, ready, None, valid, api=True, failure=failed)

    def next_track(self):
        self._wave_request += 1
        self._advance(False, autoplay=self._desired_playing)

    def audio_ended(self):
        self._advance(True)

    def previous_track(self):
        if self._wave_batch_pending:
            return
        self._page_advance_pending = False
        self._wave_request += 1
        track = self._queue.previous()
        if track is not None:
            autoplay = self._desired_playing
            self._finish_wave_track(False)
            self._start(track, autoplay=autoplay)

    def seek(self, fraction):
        self._progress_since = None
        position = self.player.seek(fraction)
        if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
            self._last_position_ms = position

    def _cancel_page_advance(self):
        if self._page_advance_pending:
            self._page_advance_pending = False
            self._desired_playing = False

    def set_volume(self, fraction):
        self.player.set_volume(fraction)
        self.window.set_volume(fraction)

    def set_equalizer(self, index, gain):
        try:
            index, gain = _normalize_equalizer(index, gain)
        except ValueError:
            return
        if not self.player.equalizer_available:
            self.window.show_error('Эквалайзер недоступен. Установите пакет gstreamer1-plugins-good.')
            return
        self.player.set_equalizer(index, gain)

    def equalizer_preset(self, name):
        values = EQUALIZER_PRESETS.get(name) if isinstance(name, str) else None
        if values is None:
            return
        if not self.player.equalizer_available:
            self.window.show_error('Эквалайзер недоступен. Установите пакет gstreamer1-plugins-good.')
            return
        try:
            for index, gain in enumerate(values):
                self.player.set_equalizer(index, gain)
        except Exception:
            self.window.show_error('Не удалось применить настройки эквалайзера.')
            return
        self.window.set_equalizer_values(values)

    @property
    def shuffle_enabled(self):
        return self._shuffle_enabled

    @property
    def repeat_enabled(self):
        return self._repeat_enabled

    def _configure_queue_modes(self, is_wave):
        self._queue.set_shuffle(False if is_wave else self._shuffle_enabled)
        self._queue.set_repeat(False if is_wave else self._repeat_enabled)

    def set_shuffle(self, enabled):
        if not isinstance(enabled, bool) or enabled == self._shuffle_enabled:
            return
        self._cancel_prefetch()
        self._shuffle_enabled = enabled
        if self._wave is None:
            self._queue.set_shuffle(enabled)
        update = getattr(self.window, 'set_shuffle_state', None)
        if update is not None:
            update(enabled)
        self._prepare_prefetch()

    def set_repeat(self, enabled):
        if not isinstance(enabled, bool) or enabled == self._repeat_enabled:
            return
        self._cancel_prefetch()
        self._repeat_enabled = enabled
        if self._wave is None:
            self._queue.set_repeat(enabled)
        update = getattr(self.window, 'set_repeat_state', None)
        if update is not None:
            update(enabled)
        self._prepare_prefetch()

    def set_liked(self, track, liked):
        service, session = self._service, self._session
        if service is None or track is None:
            return
        def work():
            with self._api_lock:
                service.set_liked(track, liked)
        self._submit(work, lambda _: self.window.set_status('Оценка сохранена'),
                     'Не удалось изменить отметку «Мне нравится».',
                     lambda: session == self._session, api=True)

    def close(self):
        self._cancel_prefetch()
        self._download_complete_play = None
        self._using_buffer = False
        self._wave_fetch_generation += 1
        self._wave_fetching = False
        self._closed = True
        self._auth_cancel.set()
        self.player.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self._stream_executor is not self.executor:
            self._stream_executor.shutdown(wait=False, cancel_futures=True)
        if self._prefetch_executor is not self.executor:
            self._prefetch_executor.shutdown(wait=False, cancel_futures=True)
        if self._api_executor is not self.executor:
            self._api_executor.shutdown(wait=False, cancel_futures=True)
        if self._storage_executor is not self.executor:
            # Logout deletion is a user-requested operation; let it finish even
            # if the window closes immediately after the user signs out.
            self._storage_executor.shutdown(wait=False, cancel_futures=False)
