"""Coordinates the UI, player and service without blocking the UI thread."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from time import monotonic

from .queue import PlaybackQueue
from .player import _normalize_equalizer
from .presets import EQUALIZER_PRESETS
from .wave import WaveSession

_CANCELLED = object()


class Controller:
    def __init__(self, window, player, store, service_factory, executor=None, dispatch=None):
        self.window = window
        self.player = player
        self.store = store
        self.service_factory = service_factory
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix='impuls')
        self._api_executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='impuls-api')
        self._storage_executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='impuls-secrets')
        self.wave_factory = WaveSession
        self._wave = None
        self._wave_tracks = []
        self._wave_current = None
        self._wave_started = False
        self._wave_since = None
        self._wave_seconds = 0.0
        self._wave_batch_pending = False
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
            'set_shuffle', 'set_repeat', 'set_liked')}

    def _submit(self, task, success, error_message, valid=lambda: True, *, api=False, storage=False, essential=False):
        if self._closed:
            return
        self._pending += 1
        self.window.set_busy(True)
        def guarded():
            if (self._closed and not essential) or not valid():
                return _CANCELLED
            return task()
        pool = self._storage_executor if storage else self._api_executor if api else self.executor
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
                except Exception:
                    # Service exceptions may contain URLs, tokens or account data.
                    if callable(error_message):
                        error_message()
                    elif error_message:
                        self.window.show_error(error_message)
                return False
            self.dispatch(deliver)
        future.add_done_callback(complete)

    def _new_session(self):
        self._auth_cancel.set()
        self._auth_cancel = Event()
        with self._credential_lock:
            self._session += 1
            current = self._session
        self._view += 1
        self._play += 1
        self._service = None
        self._visible_tracks = []
        self._visible_wave = False
        self._queue.clear()
        self._wave = None
        self._wave_tracks = []
        self._wave_current = None
        self._wave_started = False
        self._wave_since = None
        self._wave_seconds = 0.0
        self._wave_batch_pending = False
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

    def _load(self, operation, heading, playlists=False):
        service = self._service
        if service is None:
            self.window.set_status('Войдите, чтобы открыть коллекцию.')
            return
        self._view += 1
        view, session = self._view, self._session
        self.window.set_status('Загрузка…')
        def work():
            with self._api_lock:
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
        self._submit(work, done, 'Не удалось загрузить данные. Проверьте подключение.',
                     lambda: view == self._view and session == self._session, api=True)

    def search(self, query):
        query = query.strip()
        if query:
            self._load(lambda service, cancel: service.search(query), 'Результаты поиска')

    def wave(self):
        service = self._service
        if service is None:
            self.window.set_status('Войдите, чтобы включить Мою волну.')
            return
        self._view += 1
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

    def liked_tracks(self):
        self._load(lambda service, cancel: service.liked_tracks(should_cancel=cancel), 'Мне нравится')

    def playlists(self):
        self._load(lambda service, cancel: service.playlists(), 'Плейлисты', playlists=True)

    def open_playlist(self, playlist):
        self._load(lambda service, cancel: service.playlist_tracks(playlist, should_cancel=cancel), playlist.title)

    def play_track(self, track, index=None):
        if self._service is None:
            self.window.set_status('Войдите, чтобы слушать музыку.')
            return
        # A user selection supersedes an older pending view or Wave request.
        self._view += 1
        self._finish_wave_track(False)
        if self._wave is not None and not self._visible_wave:
            self._wave = None
            self._wave_tracks = []
        self._configure_queue_modes(is_wave=self._wave is not None)
        self._queue.replace(self._visible_tracks, track, index=index)
        self._start(track)

    def _start(self, track, autoplay=True):
        service = self._service
        if service is None or track is None:
            return
        self._play += 1
        play, session = self._play, self._session
        self._wave_batch_pending = False
        self._loading_track = True
        self._desired_playing = bool(autoplay)
        self.player.stop()
        self._wave_current = track if self._wave is not None else None
        self.window.set_now_playing(track)
        self.window.set_playback(False)
        self.window.set_status('Подготовка трека…')
        def work():
            with self._api_lock:
                return service.stream_url(track)
        def ready(url):
            self._loading_track = False
            self.player.play_url(url, autoplay=self._desired_playing)
            self.window.set_status('Воспроизведение' if self._desired_playing else 'Пауза')
        def failed():
            self._loading_track = False
            self.window.show_error('Трек недоступен: проверьте подписку, регион и подключение.')
        self._submit(work, ready, failed,
                     lambda: play == self._play and session == self._session, api=True)

    @property
    def playback_pending(self):
        return self._loading_track or self._wave_batch_pending

    @property
    def display_playing(self):
        return self._desired_playing if self.playback_pending else self.player.playing

    @property
    def current_track(self):
        return self._queue.current

    def stop_playback(self):
        self._wave_request += 1
        self._finish_wave_track(False)
        self._play += 1
        self._wave_batch_pending = False
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
            if self.player.playing != playing:
                self.player.toggle()
            self._desired_playing = playing
        elif playing and self._queue.current is not None:
            self.restart_current()

    def toggle_playback(self):
        if not self.player.has_source and not self.playback_pending:
            self.set_playing(True)
        else:
            self.set_playing(not self.display_playing)

    def _advance(self, finished, autoplay=True):
        if self._wave_batch_pending:
            return
        self._finish_wave_track(finished)
        track = self._queue.next()
        if track is not None:
            self._start(track, autoplay=autoplay)
            return
        self._play += 1
        self._loading_track = False
        self.player.stop()
        self.window.set_playback(False)
        if self._wave is None:
            self._desired_playing = False
            self.window.set_status('Очередь закончилась')
            return
        wave, session, play, view = self._wave, self._session, self._play, self._view
        self._desired_playing = bool(autoplay)
        self._wave_batch_pending = True
        self.window.set_status('Продолжаем Мою волну…')
        def ready(tracks):
            self._wave_batch_pending = False
            self._start_wave_batch(tracks, autoplay=self._desired_playing,
                                   update_view=self._visible_wave and view == self._view,
                                   continuation=True)
        def failed():
            self._wave_batch_pending = False
            self.window.show_error('Не удалось продолжить Мою волну.')
        self._submit(wave.next_batch, ready, failed,
                     lambda: wave is self._wave and session == self._session and play == self._play,
                     api=True)

    def next_track(self):
        self._wave_request += 1
        self._advance(False, autoplay=self._desired_playing)

    def audio_ended(self):
        self._advance(True)

    def previous_track(self):
        if self._wave_batch_pending:
            return
        self._wave_request += 1
        track = self._queue.previous()
        if track is not None:
            autoplay = self._desired_playing
            self._finish_wave_track(False)
            self._start(track, autoplay=autoplay)

    def seek(self, fraction):
        self.player.seek(fraction)

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
        self._shuffle_enabled = enabled
        if self._wave is None:
            self._queue.set_shuffle(enabled)
        update = getattr(self.window, 'set_shuffle_state', None)
        if update is not None:
            update(enabled)

    def set_repeat(self, enabled):
        if not isinstance(enabled, bool) or enabled == self._repeat_enabled:
            return
        self._repeat_enabled = enabled
        if self._wave is None:
            self._queue.set_repeat(enabled)
        update = getattr(self.window, 'set_repeat_state', None)
        if update is not None:
            update(enabled)

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
        self._closed = True
        self._auth_cancel.set()
        self.player.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self._api_executor is not self.executor:
            self._api_executor.shutdown(wait=False, cancel_futures=True)
        if self._storage_executor is not self.executor:
            # Logout deletion is a user-requested operation; let it finish even
            # if the window closes immediately after the user signs out.
            self._storage_executor.shutdown(wait=False, cancel_futures=False)
