import unittest
import gc
import weakref
from threading import Event, Thread
from concurrent.futures import Future
from dataclasses import dataclass
from unittest.mock import patch
from types import SimpleNamespace

from impuls_player.controller import Controller
from impuls_player.queue import PlaybackQueue


@dataclass(frozen=True)
class Track:
    id: str
    title: str = 'Track'


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn):
        future = Future()
        self.jobs.append((future, fn))
        return future

    def run(self, index=0):
        future, fn = self.jobs.pop(index)
        try:
            future.set_result(fn())
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, **kwargs):
        pass


class Window:
    def __init__(self):
        self.account = None
        self.tracks = []
        self.errors = []
        self.status = ''
    def set_account(self, name): self.account = name
    def set_tracks(self, tracks, heading): self.tracks = list(tracks)
    def append_tracks(self, tracks): self.tracks.extend(tracks)
    def show_loading(self, heading): self.heading = heading; self.tracks = []
    def show_load_error(self, message='Не удалось загрузить данные. Повторите попытку.'):
        self.load_error = message
        self.status = message
    def set_pagination(self, has_more, loading=False, error=False):
        self.pagination = (has_more, loading)
        self.pagination_error = error
    def set_playlists(self, playlists): pass
    def set_status(self, message): self.status = message
    def set_busy(self, busy): pass
    def set_now_playing(self, track, index=None):
        self.now_playing = track
        self.now_playing_index = index
    def set_playback(self, playing, position_ms=0, duration_ms=0): pass
    def show_error(self, message): self.errors.append(message)
    def show_device_code(self, code, url): pass
    def set_volume(self, value): self.volume = value
    def set_equalizer_available(self, available): self.equalizer_available = available
    def set_equalizer_values(self, values): self.equalizer_values = tuple(values)
    def set_shuffle_state(self, enabled): self.shuffle_state = enabled
    def set_repeat_state(self, enabled): self.repeat_state = enabled
    def set_stream_quality(self, codec, bitrate): self.stream_quality = (codec, bitrate)


class Player:
    def __init__(self):
        self.urls = []; self.buffers = []; self.has_source = False; self.playing = False
        self.stop_count = 0
        self.fail_buffer = False
        self.equalizer_available = True; self.equalizer_changes = []
    def stop(self): self.has_source = False; self.playing = False; self.stop_count += 1
    def play_url(self, url, autoplay=True, start_ms=0):
        self.start_ms = start_ms
        self.urls.append(url); self.has_source = True; self.playing = autoplay
    def play_buffer(self, data, autoplay=True):
        if self.fail_buffer:
            raise RuntimeError('buffer failure')
        self.buffers.append(data); self.has_source = True; self.playing = autoplay
    def toggle(self): self.playing = not self.playing
    def seek(self, fraction): pass
    def set_volume(self, fraction): pass
    def set_equalizer(self, index, gain): self.equalizer_changes.append((index, gain))


class Store:
    def __init__(self): self.token = None
    def save(self, token): self.token = token
    def load(self): return self.token
    def clear(self): self.token = None


class Service:
    def __init__(self, token): self.token = token
    def connect(self): return 'User'
    def search(self, query): return [Track(query)]
    def liked_tracks(self, should_cancel=None): return [Track('liked')]
    def liked_tracks_page(self, offset=0, limit=100, should_cancel=None, snapshot=None): return ([Track('liked')], False)
    def playback_service(self): return self
    def stream_url(self, track): return 'https://music.example/' + track.id
    def stream_info(self, track, quality='hq'):
        self.requested_quality = quality
        return SimpleNamespace(url=self.stream_url(track), codec='mp3', bitrate=192, quality=quality)


class ReverseRandom:
    def shuffle(self, values): values.reverse()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.executor, self.window, self.player, self.store = ManualExecutor(), Window(), Player(), Store()
        self.controller = Controller(self.window, self.player, self.store, Service, self.executor, lambda fn: fn())

    def login(self):
        self.controller.login('secret', False)
        self.executor.run()
        self.executor.run()

    def test_sleep_timer_expires_once_and_stops_pending_playback(self):
        self.open_two_track_queue()
        updates = []
        self.window.set_sleep_timer_remaining = updates.append
        with patch('impuls_player.controller.monotonic', return_value=100):
            self.controller.set_sleep_timer(15)
        with patch('impuls_player.controller.monotonic', return_value=1000):
            self.controller.monitor_playback(0, 10000)
        self.assertFalse(self.controller.display_playing)
        self.assertFalse(self.player.has_source)
        self.assertIsNone(updates[-1])
        self.assertIn('Таймер сна', self.window.status)
        before = self.window.status
        self.controller.monitor_playback(0, 10000)
        self.assertEqual(self.window.status, before)

    def test_sleep_timer_cancellation_and_invalid_values(self):
        with patch('impuls_player.controller.monotonic', return_value=100):
            self.controller.set_sleep_timer(30)
            deadline = self.controller._sleep_deadline
            for value in (-1, True, 1.5, '15', 9999):
                self.controller.set_sleep_timer(value)
                self.assertEqual(self.controller._sleep_deadline, deadline)
            self.controller.set_sleep_timer(0)
            self.assertIsNone(self.controller._sleep_deadline)

    def test_sleep_timer_discards_a_stream_resolved_after_expiry(self):
        self.login()
        self.controller.play_track(Track('late'))
        future, resolve = self.executor.jobs.pop()
        stream = resolve()
        self.assertTrue(self.controller.playback_pending)
        with patch('impuls_player.controller.monotonic', return_value=100):
            self.controller.set_sleep_timer(15)
        with patch('impuls_player.controller.monotonic', return_value=1000):
            self.controller.monitor_playback(0, 10000)
        # The network operation began before expiration and completes later.
        future.set_result(stream)
        self.assertFalse(self.controller.playback_pending)
        self.assertFalse(self.controller.display_playing)
        self.assertFalse(self.player.has_source)
        self.assertEqual(self.player.urls, [])

    def test_refresh_search_reloads_results_without_replacing_playing_queue(self):
        first, second = self.open_two_track_queue()
        service = self.controller._service
        generation = [0]
        service.invalidate_cache = lambda: generation.__setitem__(0, generation[0] + 1)
        service.search = lambda query: [Track(f'{query}-{generation[0]}')]
        self.controller.search('new')
        self.executor.run()
        urls = list(self.player.urls)
        self.controller.refresh_view()
        self.executor.run()
        self.assertEqual([track.id for track in self.window.tracks], ['new-1'])
        self.assertEqual(self.player.urls, urls)
        self.assertEqual(self.controller.current_track, first)
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track, second)

    def test_refresh_likes_keeps_the_playing_snapshot_for_later_pages(self):
        self.login()
        service = self.controller._service
        snapshots, version = {}, [0]
        def load(offset=0, limit=100, should_cancel=None, snapshot=None):
            snapshots.setdefault(snapshot, version[0])
            return [Track(f'{snapshots[snapshot]}-{offset}')], offset == 0
        service.liked_tracks_page = load
        service.invalidate_cache = lambda: version.__setitem__(0, version[0] + 1)
        self.controller.liked_tracks()
        self.executor.run()
        self.controller.play_track(self.window.tracks[0])
        self.executor.run()
        self.controller.refresh_view()
        self.executor.run()
        self.assertEqual([track.id for track in self.window.tracks], ['1-0'])
        self.assertEqual(self.controller.current_track.id, '0-0')
        self.controller.next_track()
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.controller.current_track.id, '0-100')
        self.assertEqual([track.id for track in self.window.tracks], ['1-0'])

    def test_refresh_repeats_playlist_and_playlist_listing_requests(self):
        self.login()
        service, version = self.controller._service, [0]
        service.invalidate_cache = lambda: version.__setitem__(0, version[0] + 1)
        self.window.set_playlists = lambda items: setattr(self.window, 'playlists', list(items))
        service.playlists = lambda: [f'list-{version[0]}']
        self.controller.playlists()
        self.executor.run()
        self.controller.refresh_view()
        self.executor.run()
        self.assertEqual(self.window.playlists, ['list-1'])
        playlist = SimpleNamespace(title='Playlist')
        service.playlist_tracks_page = lambda selected, **kwargs: ([Track(f'playlist-{version[0]}')], False)
        self.controller.open_playlist(playlist)
        self.executor.run()
        self.controller.refresh_view()
        self.executor.run()
        self.assertEqual([track.id for track in self.window.tracks], ['playlist-2'])

    def test_refresh_can_retry_failed_search_and_ignores_stale_navigation(self):
        self.login()
        self.controller._service.invalidate_cache = lambda: None
        self.controller._service.search = lambda query: (_ for _ in ()).throw(RuntimeError('offline'))
        self.controller.search('retry')
        self.executor.run()
        self.controller._service.search = lambda query: [Track(query)]
        self.controller.refresh_view()
        self.executor.run()
        self.assertEqual([track.id for track in self.window.tracks], ['retry'])
        self.controller.refresh_view()
        self.controller.search('latest')
        self.executor.run(1)
        self.executor.run()
        self.assertEqual([track.id for track in self.window.tracks], ['latest'])
        self.controller.refresh_view()
        self.controller.logout()
        self.executor.run()
        self.assertEqual(self.window.tracks, [])

    def test_play_all_starts_first_occurrence_and_preserves_duplicate_rows(self):
        self.login()
        tracks = [Track('same'), Track('same'), Track('last')]
        self.controller._visible_tracks = tracks
        self.controller.set_shuffle(True)
        self.controller.play_all()
        self.executor.run()
        self.assertFalse(self.controller.shuffle_enabled)
        self.assertEqual(self.controller.current_track, tracks[0])
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track.id, 'same')
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track.id, 'last')

    def test_play_all_shuffle_starts_random_occurrence_and_plays_entire_list(self):
        self.login()
        self.controller._visible_tracks = [Track('first'), Track('middle'), Track('last')]
        with patch('impuls_player.controller.random.randrange', return_value=1):
            self.controller.play_all(True)
        self.executor.run()
        self.assertTrue(self.controller.shuffle_enabled)
        self.assertEqual(self.controller.current_track.id, 'middle')
        heard = [self.controller.current_track.id]
        for _ in range(2):
            self.controller.next_track()
            self.executor.run()
            heard.append(self.controller.current_track.id)
        self.assertEqual(set(heard), {'first', 'middle', 'last'})

    def test_play_all_wave_keeps_service_order_and_saved_shuffle_setting(self):
        self.login()
        self.controller.set_shuffle(True)
        self.controller._wave = SimpleNamespace(track_skipped=lambda *_: None)
        self.controller._visible_wave = True
        self.controller._visible_tracks = [Track('first'), Track('second')]
        self.controller.play_all(True)
        self.executor.run()
        self.assertEqual(self.controller.current_track.id, 'first')
        self.assertTrue(self.controller.shuffle_enabled)
        self.assertFalse(self.controller._queue.shuffle_enabled)
        self.assertEqual(self.controller._queue.peek_next().id, 'second')

    def test_new_actions_handle_empty_or_signed_out_views(self):
        actions = self.controller.actions()
        actions['refresh_view']()
        actions['play_all']()
        self.assertEqual(self.executor.jobs, [])
        self.assertTrue(self.window.status)
        self.login()
        self.controller.refresh_view()
        self.controller.play_all()
        self.assertEqual(self.executor.jobs, [])
        self.assertIsNone(self.controller.current_track)

    def test_play_all_during_search_load_does_not_start_previous_visible_list(self):
        self.open_two_track_queue()
        urls = list(self.player.urls)
        self.controller.search('pending')
        self.controller.play_all()
        self.executor.run()
        self.assertEqual(self.player.urls, urls)
        self.assertEqual(len(self.executor.jobs), 0)

    def test_failed_resume_can_recover_using_the_new_playback_intent(self):
        self.open_two_track_queue()
        self.controller.set_playing(False)
        def failed_toggle():
            self.player.stop()
            self.controller.audio_error()
        self.player.toggle = failed_toggle
        self.controller.set_playing(True)
        self.assertTrue(self.controller.playback_pending)
        self.executor.run()
        self.assertTrue(self.player.playing)

    def test_failed_pause_does_not_restart_audio_against_user_intent(self):
        self.open_two_track_queue()
        def failed_toggle():
            self.player.stop()
            self.controller.audio_error()
        self.player.toggle = failed_toggle
        self.controller.set_playing(False)
        self.assertFalse(self.controller.playback_pending)
        self.assertFalse(self.player.playing)

    def test_quality_change_keeps_current_audio_and_rebuilds_next_cache(self):
        self.open_two_track_queue()
        original_urls = list(self.player.urls)
        with patch('impuls_player.controller.download_audio', return_value=b'audio'):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.set_quality('lossless')
            self.assertEqual(self.player.urls, original_urls)
            self.assertIsNone(self.controller._prefetched_audio)
            self.executor.run()
            self.assertEqual(self.controller._service.requested_quality, 'lossless')
            self.controller.next_track()
        self.assertEqual(self.player.buffers, [b'audio'])
        self.assertEqual(self.window.stream_quality, ('mp3', 192))

    def test_quality_invalid_selection_is_ignored(self):
        for value in (None, [], 'invalid'):
            self.controller.set_quality(value)
        self.assertEqual(self.controller._quality, 'hq')

    def test_stalled_playing_recovers_same_track_at_last_position(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.monotonic', return_value=0):
            self.controller.monitor_playback(1500, 8000)
        with patch('impuls_player.controller.monotonic', return_value=13):
            self.controller.monitor_playback(1500, 8000)
        self.assertTrue(self.controller.playback_pending)
        self.executor.run()
        self.assertEqual(self.player.start_ms, 1500)
        self.assertEqual(self.controller.current_track.id, 'first')

    def test_pause_and_seek_reset_stall_timer(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.monotonic', return_value=0):
            self.controller.monitor_playback(1500, 8000)
        self.controller.set_playing(False)
        with patch('impuls_player.controller.monotonic', return_value=99):
            self.controller.monitor_playback(1500, 8000)
        self.assertFalse(self.controller.playback_pending)
        self.controller.set_playing(True)
        self.controller.seek(0.5)
        with patch('impuls_player.controller.monotonic', return_value=100):
            self.controller.monitor_playback(4000, 8000)
        self.assertFalse(self.controller.playback_pending)

    def test_repeated_stalls_stop_after_two_recoveries(self):
        self.open_two_track_queue()
        for attempt in range(3):
            with patch('impuls_player.controller.monotonic', return_value=attempt * 20):
                self.controller.monitor_playback(1500, 8000)
            with patch('impuls_player.controller.monotonic', return_value=attempt * 20 + 13):
                self.controller.monitor_playback(1500, 8000)
            if attempt < 2:
                self.executor.run()
        self.assertFalse(self.player.playing)
        self.assertFalse(self.controller.display_playing)
        self.assertEqual(self.controller._recovery_attempts, 2)
        self.assertTrue(self.window.errors)

    def test_user_switch_cancels_pending_recovery(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.monotonic', return_value=0):
            self.controller.monitor_playback(1500, 8000)
        with patch('impuls_player.controller.monotonic', return_value=13):
            self.controller.monitor_playback(1500, 8000)
        self.controller.next_track()
        self.executor.run()  # stale recovery is discarded
        self.executor.run()
        self.assertEqual(self.controller.current_track.id, 'second')
        self.assertEqual(self.player.start_ms, 0)

    def test_equalizer_action_clamps_and_controls_the_requested_band(self):
        action = self.controller.actions()['set_equalizer']
        action(4, 1.25)
        action(9, 99)
        action(0, -99)
        self.assertEqual(self.player.equalizer_changes, [(4, 1.25), (9, 12.0), (0, -12.0)])

    def test_equalizer_action_rejects_invalid_inputs(self):
        for index, gain in ((-1, 0), (10, 0), (1.5, 0), (True, 0), (0, float('nan')),
                            (0, float('inf')), (0, True), (0, '1')):
            with self.subTest(index=index, gain=gain):
                self.controller.set_equalizer(index, gain)
        self.assertEqual(self.player.equalizer_changes, [])

    def test_unavailable_equalizer_is_announced_and_does_not_change_audio(self):
        self.player.equalizer_available = False
        other = Controller(self.window, self.player, self.store, Service, self.executor, lambda fn: fn())
        self.assertFalse(self.window.equalizer_available)
        other.set_equalizer(0, 3)
        self.assertEqual(self.player.equalizer_changes, [])
        self.assertTrue(self.window.errors)

    def test_equalizer_preset_changes_all_bands_and_updates_controls(self):
        from impuls_player.presets import EQUALIZER_PRESETS
        self.controller.actions()['equalizer_preset']('Рок')
        self.assertEqual(self.player.equalizer_changes, list(enumerate(EQUALIZER_PRESETS['Рок'])))
        self.assertEqual(self.window.equalizer_values, tuple(EQUALIZER_PRESETS['Рок']))

    def test_invalid_or_unavailable_preset_does_not_change_bands(self):
        self.controller.equalizer_preset('missing')
        self.controller.equalizer_preset([])
        self.assertEqual(self.player.equalizer_changes, [])
        self.player.equalizer_available = False
        self.controller.equalizer_preset('Рок')
        self.assertEqual(self.player.equalizer_changes, [])
        self.assertTrue(self.window.errors)

    def test_mode_changes_do_not_stop_or_cancel_a_pending_track(self):
        self.login()
        track = Track('pending')
        self.controller.play_track(track)
        stops = self.player.stop_count
        self.controller.actions()['set_shuffle'](True)
        self.controller.actions()['set_repeat'](True)
        self.assertEqual(self.player.stop_count, stops)
        self.assertTrue(self.controller.shuffle_enabled)
        self.assertTrue(self.controller.repeat_enabled)
        self.assertTrue(self.window.shuffle_state)
        self.assertTrue(self.window.repeat_state)
        self.executor.run()
        self.assertEqual(self.player.urls, ['https://music.example/pending'])

    def test_mode_changes_do_not_interrupt_playing_audio(self):
        self.login()
        track = Track('playing')
        self.controller.play_track(track)
        self.executor.run()
        stops = self.player.stop_count
        self.controller.set_repeat(True)
        self.controller.set_shuffle(True)
        self.controller.set_repeat(False)
        self.assertEqual(self.player.stop_count, stops)
        self.assertTrue(self.player.playing)
        self.assertTrue(self.player.has_source)
        self.assertFalse(self.window.repeat_state)

    def test_repeat_wraps_list_for_natural_end_and_next(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller._visible_tracks = [first, second]
        self.controller.set_repeat(True)
        self.controller.play_track(second)
        self.executor.run()
        self.controller.audio_ended()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls, [
            'https://music.example/second', 'https://music.example/first', 'https://music.example/second',
        ])

    def test_shuffle_repeat_apply_to_new_normal_queue(self):
        self.login()
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        tracks = [Track('one'), Track('two'), Track('three')]
        self.controller._visible_tracks = tracks
        self.controller.set_shuffle(True)
        self.controller.set_repeat(True)
        self.controller.play_track(tracks[1])
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.controller.audio_ended()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls[:3], [
            'https://music.example/two', 'https://music.example/three', 'https://music.example/one',
        ])
        self.assertNotEqual(self.player.urls[-1], self.player.urls[-2])

    def test_wave_keeps_service_order_and_fetches_next_batch_despite_modes(self):
        self.login()
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1'), Track('wave2')]
            def next_batch(self): return [Track('wave3')]
        self.controller.wave_factory = Wave
        self.controller.set_shuffle(True)
        self.controller.set_repeat(True)
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.player.urls, [
            'https://music.example/wave1', 'https://music.example/wave2', 'https://music.example/wave3',
        ])
        self.assertTrue(self.controller.shuffle_enabled)
        self.assertTrue(self.controller.repeat_enabled)

    def test_mode_settings_return_for_normal_list_after_wave(self):
        self.login()
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave')]
        self.controller.wave_factory = Wave
        self.controller.set_shuffle(True)
        self.controller.set_repeat(True)
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        ordinary = [Track('one'), Track('two'), Track('three')]
        self.controller._service.search = lambda _query: ordinary
        self.controller.search('some query')
        self.executor.run()
        self.controller.play_track(ordinary[1])
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls[-2:], ['https://music.example/two', 'https://music.example/three'])

    def test_switching_modes_while_next_wave_batch_is_pending_keeps_request(self):
        self.login()
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): return [Track('wave2'), Track('wave3')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        stops = self.player.stop_count
        self.controller.set_repeat(True)
        self.controller.set_shuffle(True)
        self.assertEqual(self.player.stop_count, stops)
        self.executor.run()
        self.executor.run()
        self.controller.audio_ended()
        self.executor.run()
        self.assertEqual(self.player.urls, [
            'https://music.example/wave1', 'https://music.example/wave2', 'https://music.example/wave3',
        ])

    def test_late_wave_start_does_not_override_new_track_selection(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave')]
        self.controller.wave_factory = Wave
        ordinary = Track('ordinary')
        self.controller._visible_tracks = [ordinary]
        self.controller.wave()
        self.controller.play_track(ordinary)
        self.executor.run()  # obsolete wave start
        self.executor.run()  # selected track
        self.assertEqual(self.controller.current_track, ordinary)
        self.assertEqual(self.player.urls, ['https://music.example/ordinary'])

    def test_mode_setters_ignore_non_boolean_values(self):
        self.controller.set_shuffle('yes')
        self.controller.set_repeat(1)
        self.assertFalse(self.controller.shuffle_enabled)
        self.assertFalse(self.controller.repeat_enabled)

    def test_queued_like_is_not_sent_after_logout(self):
        self.login()
        calls = []
        self.controller._service.set_liked = lambda track, liked: calls.append(track.id)
        self.controller.set_liked(Track('one'), True)
        self.controller.logout()
        self.executor.run()
        self.executor.run()
        self.assertEqual(calls, [])

    def test_logout_invalidates_a_pending_login(self):
        self.controller.login('secret', True)
        self.controller.logout()
        self.executor.run()
        self.executor.run()
        self.assertIsNone(self.window.account)
        self.assertIsNone(self.store.token)

    def test_library_opens_before_network_and_loads_only_requested_page(self):
        self.login()
        calls = []
        def page(offset=0, limit=100, should_cancel=None, snapshot=None):
            calls.append(offset)
            return ([Track(str(offset))], offset == 0)
        self.controller._service.liked_tracks_page = page
        self.controller.liked_tracks()
        self.assertEqual(self.window.heading, 'Мне нравится')
        self.assertEqual(calls, [])
        self.executor.run()
        self.assertEqual(self.window.tracks, [Track('0')])
        self.assertEqual(calls, [0])
        self.controller.load_more()
        self.controller.load_more()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        self.assertEqual(calls, [0, 100])
        self.assertEqual(self.window.tracks, [Track('0'), Track('100')])

    def test_playing_during_page_load_preserves_append_and_queue(self):
        self.login()
        self.controller._service.liked_tracks_page = lambda offset=0, **kw: ([Track(str(offset))], offset == 0)
        self.controller.liked_tracks()
        self.executor.run()
        self.controller.load_more()
        self.controller.play_track(Track('0'))
        self.executor.run()  # second page stays relevant
        self.executor.run()  # first track URL
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track, Track('100'))
        self.assertFalse(self.controller._page_loading)

    def test_navigation_cancels_pending_page_append(self):
        self.login()
        self.controller._service.liked_tracks_page = lambda **kw: ([Track('liked')], True)
        self.controller.liked_tracks()
        self.executor.run()
        self.controller.load_more()
        self.controller.search('new')
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.window.tracks, [Track('new')])

    def open_paged_queue(self, pages):
        self.login()
        requests = []
        def page(offset=0, limit=100, should_cancel=None, snapshot=None):
            requests.append(offset)
            result = pages[offset]
            if isinstance(result, Exception):
                raise result
            return result
        self.controller._service.liked_tracks_page = page
        self.controller.liked_tracks()
        self.executor.run()
        self.controller.play_track(pages[0][0][0], 0)
        self.executor.run()
        return requests

    def test_queue_end_loads_next_page_before_repeat_wraps(self):
        requests = self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.set_repeat(True)
        self.controller.audio_ended()
        self.executor.run()  # next page
        self.assertEqual(requests, [0, 100])
        self.assertEqual(self.controller.current_track, Track('second'))
        self.executor.run()  # second track
        self.assertEqual(self.player.urls, ['https://music.example/first', 'https://music.example/second'])
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls[-1], 'https://music.example/first')

    def test_pausing_during_automatic_page_load_keeps_next_track_paused(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.next_track()
        self.assertTrue(self.controller.playback_pending)
        self.controller.set_playing(False)
        self.executor.run()  # next page
        self.executor.run()  # second track
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertFalse(self.player.playing)

    def test_queue_end_joins_page_already_being_loaded_without_skipping(self):
        requests = self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.load_more()
        self.controller.next_track()
        self.controller.next_track()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        self.executor.run()
        self.assertEqual(requests, [0, 100])
        self.assertEqual(self.player.urls, ['https://music.example/first', 'https://music.example/second'])

    def test_empty_intermediate_page_is_skipped_before_repeat(self):
        requests = self.open_paged_queue({0: ([Track('first')], True),
                                          100: ([], True), 200: ([Track('second')], False)})
        self.controller.set_repeat(True)
        self.controller.next_track()
        self.executor.run()
        self.executor.run()
        self.executor.run()
        self.assertEqual(requests, [0, 100, 200])
        self.assertEqual(self.player.urls, ['https://music.example/first', 'https://music.example/second'])

    def test_stop_during_page_load_does_not_start_a_new_track(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.next_track()
        self.controller.stop_playback()
        self.executor.run()
        self.assertFalse(self.controller.playback_pending)
        self.assertFalse(self.player.has_source)
        self.assertEqual(self.player.urls, ['https://music.example/first'])

    def test_stop_during_detached_queue_page_does_not_start_old_track(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.search('new')
        self.executor.run()  # new view
        self.controller.next_track()
        self.controller.stop_playback()
        self.executor.run()  # cancelled queue page
        self.assertEqual(self.window.tracks, [Track('new')])
        self.assertFalse(self.controller.playback_pending)
        self.assertEqual(self.player.urls, ['https://music.example/first'])

    def test_logout_during_queue_page_load_does_not_start_old_track(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.next_track()
        self.controller.logout()
        self.executor.run()  # stale page
        self.executor.run()  # secret deletion
        self.assertIsNone(self.window.account)
        self.assertFalse(self.controller.playback_pending)
        self.assertEqual(self.player.urls, ['https://music.example/first'])

    def test_previous_during_page_load_preserves_selected_history(self):
        first, second, third = Track('first'), Track('second'), Track('third')
        self.open_paged_queue({0: ([first, second], True), 100: ([third], False)})
        self.controller.next_track()
        self.executor.run()
        self.controller.next_track()  # waiting for next page
        self.controller.previous_track()
        self.executor.run()  # page
        self.executor.run()  # previous track
        self.assertEqual(self.controller.current_track, first)
        self.assertEqual(self.player.urls, ['https://music.example/first',
                                             'https://music.example/second',
                                             'https://music.example/first'])

    def test_selecting_a_track_during_page_load_does_not_auto_advance(self):
        first, other, third = Track('first'), Track('other'), Track('third')
        self.open_paged_queue({0: ([first, other], True), 100: ([third], False)})
        self.controller.next_track()
        self.executor.run()  # other track
        self.controller.next_track()  # waiting for next page
        self.controller.play_track(first, 0)
        self.executor.run()  # page; must not advance selected track
        self.executor.run()  # selected track
        self.assertEqual(self.player.urls, ['https://music.example/first',
                                             'https://music.example/other',
                                             'https://music.example/first'])
        self.assertEqual(self.controller.current_track, first)

    def test_failed_automatic_page_load_is_retryable_and_not_stuck_playing(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: RuntimeError('offline')})
        self.controller.next_track()
        self.executor.run()
        self.assertFalse(self.controller.playback_pending)
        self.assertFalse(self.player.has_source)
        self.assertTrue(self.window.errors)
        self.assertEqual(self.window.pagination, (True, False))
        self.assertTrue(self.window.pagination_error)

    def test_first_page_error_replaces_loading_state_and_allows_retry(self):
        self.login()
        def fail(**kwargs): raise RuntimeError('offline')
        self.controller._service.liked_tracks_page = fail
        self.controller.liked_tracks()
        self.executor.run()
        self.assertTrue(getattr(self.window, 'load_error', None))
        self.assertEqual(self.window.pagination, (True, False))
        self.assertTrue(self.window.pagination_error)

    def test_search_error_replaces_loading_state(self):
        self.login()
        def fail(query): raise RuntimeError('offline')
        self.controller._service.search = fail
        self.controller.search('query')
        self.executor.run()
        self.assertTrue(getattr(self.window, 'load_error', None))

    def test_playback_transport_is_not_retained_after_request(self):
        self.login()
        references = []
        class Playback(Service):
            def __init__(self): pass
            def stream_url(self, track): return 'https://music.example/' + track.id
        def playback_service():
            transport = Playback()
            references.append(weakref.ref(transport))
            return transport
        self.controller._service.playback_service = playback_service
        self.controller.play_track(Track('first'))
        self.executor.run()
        gc.collect()
        self.assertEqual(self.player.urls, ['https://music.example/first'])
        self.assertIsNone(references[0]())

    def open_two_track_queue(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller._visible_tracks = [first, second]
        self.controller.play_track(first, 0)
        self.executor.run()
        return first, second

    def test_prefetch_waits_for_complete_and_plays_the_cached_next_track(self):
        first, second = self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True) as fetch:
            self.assertEqual(len(self.executor.jobs), 0)
            self.assertEqual(self.controller.current_track, first)
            self.controller.current_download_complete()
            self.controller.current_download_complete()  # duplicate callback is coalesced
            self.assertEqual(len(self.executor.jobs), 1)
            self.executor.run()
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn('timeout', fetch.call_args.kwargs)
            self.assertEqual(self.controller.current_track, first)
            self.controller.next_track()
            self.assertEqual(self.controller.current_track, second)
            self.assertEqual(self.player.buffers, [b'audio'])
            self.assertEqual(self.player.urls, ['https://music.example/first'])

    def test_prefetched_track_respects_paused_playback(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.set_playing(False)
            self.controller.next_track()
        self.assertEqual(self.player.buffers, [b'audio'])
        self.assertFalse(self.player.playing)

    def test_shuffle_repeat_prefetches_the_reserved_next_cycle_track(self):
        self.login()
        tracks = [Track('first'), Track('second')]
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        self.controller.set_shuffle(True)
        self.controller.set_repeat(True)
        self.controller._visible_tracks = tracks
        self.controller.play_track(tracks[0], 0)
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        with patch('impuls_player.controller.download_audio', return_value=b'audio') as fetch:
            self.controller.current_download_complete()
            self.assertEqual(len(self.executor.jobs), 1)
            self.executor.run()
            self.controller.audio_ended()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.controller.current_track, tracks[0])
        self.assertEqual(self.player.buffers, [b'audio'])

    def test_buffered_current_track_can_trigger_prefetch_of_the_following_track(self):
        self.login()
        tracks = [Track('first'), Track('second'), Track('third')]
        self.controller._visible_tracks = tracks
        self.controller.play_track(tracks[0], 0)
        self.executor.run()
        with patch('impuls_player.controller.download_audio', side_effect=lambda url, **kw: url.encode(), create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.next_track()
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.next_track()
        self.assertEqual(self.player.buffers, [b'https://music.example/second',
                                               b'https://music.example/third'])

    def test_prefetch_failure_is_silent_and_next_uses_network(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', side_effect=RuntimeError('offline'), create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.assertEqual(self.window.errors, [])
            self.controller.next_track()
            self.executor.run()
        self.assertEqual(self.player.buffers, [])
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')

    def test_prefetch_rejects_oversized_or_empty_audio_and_falls_back(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller._MAX_PREFETCH_BYTES', 3, create=True), \
             patch('impuls_player.controller.download_audio', return_value=b'abcd', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.next_track()
            self.executor.run()
        self.assertEqual(self.player.buffers, [])
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')

    def test_buffer_playback_failure_falls_back_to_network(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.player.fail_buffer = True
            self.controller.next_track()
            self.executor.run()
        self.assertEqual(self.player.buffers, [])
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertEqual(self.window.errors, [])

    def test_asynchronous_buffer_error_retries_through_https_once(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.next_track()
            self.assertEqual(self.player.buffers, [b'audio'])
            self.assertTrue(self.controller.audio_error())
            self.executor.run()
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertTrue(self.controller.audio_error())
        self.executor.run()
        self.assertFalse(self.controller.audio_error())

    def test_paused_buffer_error_retries_without_autoplay(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio'):
            self.controller.current_download_complete()
            self.executor.run()
        self.controller.set_playing(False)
        self.controller.next_track()
        self.assertFalse(self.player.playing)
        self.assertTrue(self.controller.audio_error())
        self.executor.run()
        self.assertFalse(self.player.playing)
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')

    def test_recovery_does_not_consume_repeat_cache_from_the_beginning(self):
        self.open_two_track_queue()
        self.controller._queue.replace([Track('first')], Track('first'))
        self.controller.set_repeat(True)
        with patch('impuls_player.controller.download_audio', return_value=b'audio'):
            self.controller.current_download_complete()
            self.executor.run()
        self.controller.monitor_playback(1500, 8000)
        self.assertTrue(self.controller.audio_error())
        self.executor.run()
        self.assertEqual(self.player.buffers, [])
        self.assertEqual(self.player.start_ms, 1500)

    def test_recovery_url_failures_exhaust_the_limit_and_stop(self):
        self.open_two_track_queue()
        def offline(*args, **kwargs): raise RuntimeError('offline')
        self.controller._service.stream_info = offline
        self.assertTrue(self.controller.audio_error())
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.controller._recovery_attempts, 2)
        self.assertFalse(self.controller._desired_playing)
        self.assertFalse(self.controller.playback_pending)
        self.assertTrue(self.window.errors)

    def test_error_immediately_after_seek_preserves_requested_position(self):
        self.open_two_track_queue()
        self.controller.monitor_playback(10000, 100000)
        self.player.seek = lambda fraction: 90000
        self.controller.seek(0.9)
        self.controller.audio_error()
        self.executor.run()
        self.assertEqual(self.player.start_ms, 90000)

    def test_stop_cancels_inflight_prefetch_without_showing_error(self):
        self.open_two_track_queue()
        started, release, observed = Event(), Event(), []
        def download(url, should_cancel=None, **kwargs):
            started.set()
            release.wait(2)
            observed.append(should_cancel())
            return b'audio'
        with patch('impuls_player.controller.download_audio', side_effect=download, create=True):
            self.controller.current_download_complete()
            worker = Thread(target=self.executor.run)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.controller.stop_playback()
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(observed, [True])
        self.assertEqual(self.window.errors, [])
        self.assertEqual(self.player.buffers, [])

    def test_new_selection_cancels_queued_prefetch(self):
        first, _ = self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True) as fetch:
            self.controller.current_download_complete()
            self.controller.play_track(first, 0)
            self.executor.run()  # cancelled prefetch
            self.executor.run()  # newly selected stream
            fetch.assert_not_called()
        self.assertEqual(self.player.buffers, [])
        self.assertEqual(self.controller.current_track, first)

    def test_shuffle_discards_old_prefetch_and_loads_the_new_next_track(self):
        self.login()
        tracks = [Track('first'), Track('second'), Track('third')]
        self.controller._queue = PlaybackQueue(randomizer=ReverseRandom())
        self.controller._visible_tracks = tracks
        self.controller.play_track(tracks[0], 0)
        self.executor.run()
        with patch('impuls_player.controller.download_audio', side_effect=lambda url, **kw: url.encode(), create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.set_shuffle(True)
            self.executor.run()
            self.controller.next_track()
        self.assertEqual(self.controller.current_track, tracks[2])
        self.assertEqual(self.player.buffers, [b'https://music.example/third'])

    def test_view_change_keeps_prefetch_for_the_playing_queue(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.search('new')
            self.controller.next_track()
            self.executor.run()  # new view
        self.assertEqual(self.player.buffers, [b'audio'])
        self.assertEqual(self.controller.current_track, Track('second'))
        self.assertEqual(self.window.tracks, [Track('new')])

    def test_paged_queue_continues_after_search_without_changing_search_results(self):
        requests = self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.search('search result')
        self.executor.run()
        self.controller.audio_ended()
        self.executor.run()  # detached queue page
        self.executor.run()  # second track
        self.assertEqual(requests, [0, 100])
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertEqual(self.window.tracks, [Track('search result')])

    def test_navigation_during_page_advance_keeps_queue_and_view_independent(self):
        requests = self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.audio_ended()  # queue page request
        self.controller.search('search result')
        for _ in range(8):
            if not self.executor.jobs:
                break
            self.executor.run()
        self.assertFalse(self.executor.jobs)
        self.assertEqual(requests[-1], 100)
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertEqual(self.window.tracks, [Track('search result')])

    def test_user_search_runs_before_resumed_background_queue_page(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        started, release, events = Event(), Event(), []
        def page(offset=0, **kwargs):
            events.append('page')
            if events.count('page') == 1:
                started.set()
                release.wait(2)
            return [Track('second')], False
        def search(query):
            events.append('search')
            return [Track(query)]
        self.controller._service.liked_tracks_page = page
        self.controller._service.search = search
        self.controller.current_download_complete()
        worker = Thread(target=self.executor.run)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.controller.search('new')
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.executor.run()
        self.assertEqual(events[:2], ['page', 'search'])
        self.assertEqual(self.window.tracks, [Track('new')])

    def test_reopening_collection_keeps_the_playing_queue_snapshot(self):
        self.login()
        calls, snapshots = [], []
        def page(offset=0, limit=100, should_cancel=None, snapshot=None):
            calls.append((offset, snapshot))
            if offset == 0:
                snapshots.append(snapshot)
                return ([Track('first' if len(snapshots) == 1 else 'new-view')], True)
            self.assertIs(snapshot, snapshots[0])
            return ([Track('second')], False)
        self.controller._service.liked_tracks_page = page
        self.controller.liked_tracks()
        self.executor.run()
        self.controller.play_track(Track('first'), 0)
        self.executor.run()
        self.controller.liked_tracks()
        self.executor.run()
        self.assertIsNot(snapshots[0], snapshots[1])
        self.controller.audio_ended()
        self.executor.run()
        self.executor.run()
        self.assertEqual([offset for offset, _ in calls], [0, 0, 100])
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertEqual(self.window.tracks, [Track('new-view')])

    def test_deferred_prefetch_delivery_after_stop_is_discarded(self):
        self.open_two_track_queue()
        deliveries = []
        self.controller.dispatch = deliveries.append
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.assertEqual(len(deliveries), 1)
            self.controller.stop_playback()
            deliveries.pop()()
        self.assertFalse(getattr(self.controller, '_prefetched_audio', None))
        self.assertFalse(self.player.has_source)

    def test_background_prefetch_cannot_block_audio_or_metadata_workers(self):
        first, second = Track('first'), Track('second')
        audio_ready, fetch_started, fetch_release = Event(), Event(), Event()
        metadata_started, metadata_release = Event(), Event()
        class SignallingPlayer(Player):
            def play_url(self, url, autoplay=True):
                super().play_url(url, autoplay=autoplay)
                audio_ready.set()
        controller = Controller(Window(), SignallingPlayer(), Store(), Service, dispatch=lambda fn: fn())
        controller._service = Service('synthetic')
        controller._visible_tracks = [first, second]
        def download(url, should_cancel=None, **kwargs):
            fetch_started.set()
            fetch_release.wait(2)
            return b'audio'
        def metadata():
            metadata_started.set()
            metadata_release.wait(2)
        try:
            controller._api_executor.submit(metadata)
            self.assertTrue(metadata_started.wait(1))
            controller.play_track(first, 0)
            self.assertTrue(audio_ready.wait(1))
            audio_ready.clear()
            with patch('impuls_player.controller.download_audio', side_effect=download, create=True):
                controller.current_download_complete()
                self.assertTrue(fetch_started.wait(1))
                controller.play_track(second, 1)
                self.assertTrue(audio_ready.wait(1), 'prefetch blocked the next audio request')
                fetch_release.set()
        finally:
            metadata_release.set()
            fetch_release.set()
            controller.close()

    def test_logout_cancels_completed_prefetch(self):
        self.open_two_track_queue()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.executor.run()
            self.controller.logout()
            self.executor.run()  # secret deletion
        self.assertFalse(self.player.has_source)
        self.assertIsNone(self.controller.current_track)
        self.assertFalse(getattr(self.controller, '_prefetched_audio', None))

    def test_prefetch_loads_next_page_before_ordered_repeat_without_interrupting_playback(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: ([Track('second')], False)})
        self.controller.set_repeat(True)
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True) as fetch:
            self.controller.current_download_complete()
            self.assertEqual(len(self.executor.jobs), 1)  # next metadata page
            fetch.assert_not_called()
            self.executor.run()
            self.assertEqual(self.controller.current_track, Track('first'))
            self.assertTrue(self.player.playing)
            self.executor.run()  # next audio
            self.controller.next_track()
        self.assertEqual(self.player.buffers, [b'audio'])
        self.assertEqual(self.controller.current_track, Track('second'))

    def test_background_page_failure_keeps_playing_without_error_dialog(self):
        self.open_paged_queue({0: ([Track('first')], True), 100: RuntimeError('offline')})
        self.controller.current_download_complete()
        self.executor.run()
        self.assertEqual(self.window.errors, [])
        self.assertEqual(self.controller.current_track, Track('first'))
        self.assertTrue(self.player.playing)
        self.assertTrue(self.window.pagination_error)

    def test_selecting_earlier_wave_track_preserves_completed_future_batch(self):
        self.login()
        first, last, prepared, later = (Track(name) for name in ('first', 'last', 'prepared', 'later'))
        calls = []
        class Wave:
            def __init__(self, service): pass
            def start(self): return [first, last]
            def next_batch(self):
                calls.append(True)
                return [prepared] if len(calls) == 1 else [later]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        with patch('impuls_player.controller.download_audio', return_value=b'audio'):
            self.controller.current_download_complete()
            self.executor.run()  # New Wave batch is already appended to the queue.
            self.executor.run()  # Its first track is prefetched.
            self.controller.play_track(first, 0)
            self.executor.run()
            self.controller.next_track()
            self.executor.run()
            self.controller.next_track()
            self.executor.run()
        self.assertEqual(self.controller.current_track, prepared)
        self.assertEqual(self.window.tracks, [prepared])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.player.urls[-1], 'https://music.example/prepared')

    def test_wave_prefetches_new_batch_after_current_download(self):
        self.login()
        calls = []
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): calls.append('batch'); return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        with patch('impuls_player.controller.download_audio', return_value=b'wave audio', create=True):
            self.controller.current_download_complete()
            self.assertEqual(len(self.executor.jobs), 1)
            self.executor.run()  # next wave batch, no autoplay
            self.assertEqual(self.controller.current_track, Track('wave1'))
            self.assertEqual(calls, ['batch'])
            self.executor.run()  # next wave audio
            self.controller.next_track()
        self.assertEqual(self.controller.current_track, Track('wave2'))
        self.assertEqual(self.player.buffers, [b'wave audio'])

    def test_wave_next_waits_for_existing_prefetch_batch_only_once(self):
        self.login()
        calls = []
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): calls.append('batch'); return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.current_download_complete()
        self.assertEqual(len(self.executor.jobs), 1)
        self.controller.next_track()
        self.controller.next_track()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()  # existing next batch
        self.executor.run()  # wave2 stream
        self.assertEqual(calls, ['batch'])
        self.assertEqual(self.player.urls[-1], 'https://music.example/wave2')

    def test_wave_batch_prefetch_failure_is_silent_until_transition(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): raise RuntimeError('offline')
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.current_download_complete()
        self.executor.run()
        self.assertTrue(self.player.playing)
        self.assertEqual(self.window.errors, [])
        self.controller.next_track()
        self.executor.run()
        self.assertTrue(self.window.errors)

    def test_wave_background_batch_does_not_replace_a_new_view(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        with patch('impuls_player.controller.download_audio', return_value=b'audio', create=True):
            self.controller.current_download_complete()
            self.controller.search('new')
            self.executor.run()  # background wave batch
            self.executor.run()  # search
            self.executor.run()  # wave audio
            self.controller.next_track()
        self.assertEqual(self.window.tracks, [Track('new')])
        self.assertEqual(self.player.buffers, [b'audio'])

    def test_selecting_an_earlier_wave_track_keeps_the_next_batch_visible(self):
        self.login()
        first, last, following = Track('wave1'), Track('wave2'), Track('wave3')
        class Wave:
            def __init__(self, service): pass
            def start(self): return [first, last]
            def next_batch(self): return [following]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.controller.current_download_complete()  # next batch is pending
        self.controller.play_track(first, 0)
        self.executor.run()  # next batch
        self.executor.run()  # first track selected again
        self.controller.next_track()
        self.executor.run()  # last track
        self.controller.next_track()
        self.executor.run()  # following track
        self.assertEqual(self.controller.current_track, following)
        self.assertEqual(self.window.tracks, [following])

    def test_stop_cancels_pending_wave_batch(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.current_download_complete()
        self.controller.stop_playback()
        self.executor.run()
        self.assertFalse(self.player.has_source)
        self.assertEqual(self.player.urls, ['https://music.example/wave1'])

    def test_new_stream_starts_while_old_stream_resolution_is_blocked(self):
        old_started, release, new_started, new_playing = Event(), Event(), Event(), Event()
        class BlockingService(Service):
            def stream_url(self, track):
                if track.id == 'old':
                    old_started.set()
                    release.wait(2)
                else:
                    new_started.set()
                return 'https://music.example/' + track.id
        class SignallingPlayer(Player):
            def play_url(self, url, autoplay=True):
                super().play_url(url, autoplay=autoplay)
                if url.endswith('/new'):
                    new_playing.set()
        controller = Controller(Window(), SignallingPlayer(), Store(), BlockingService, dispatch=lambda fn: fn())
        controller._service = BlockingService('synthetic')
        try:
            controller.play_track(Track('old'))
            self.assertTrue(old_started.wait(1))
            controller.play_track(Track('new'))
            self.assertTrue(new_started.wait(1), 'new stream waited for the obsolete request')
            self.assertTrue(new_playing.wait(1))
        finally:
            release.set()
            controller.close()

    def test_most_recent_navigation_wins_when_responses_are_out_of_order(self):
        self.login()
        self.controller.search('old')
        self.controller.liked_tracks()
        self.executor.run(1)
        self.executor.run(0)
        self.assertEqual(self.window.tracks, [Track('liked')])

    def test_clicking_second_duplicate_advances_to_the_next_row(self):
        self.login()
        duplicate, following = Track('duplicate'), Track('following')
        self.controller._visible_tracks = [duplicate, duplicate, following]
        self.controller.play_track(duplicate, 1)
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls[-1], 'https://music.example/following')

    def test_duplicate_highlight_follows_next_previous_and_repeat(self):
        self.login()
        duplicate = Track('duplicate')
        self.controller._visible_tracks = [duplicate, duplicate]
        self.controller.play_track(duplicate, 0)
        self.executor.run()
        self.assertEqual(self.window.now_playing_index, 0)
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.window.now_playing_index, 1)
        self.controller.previous_track()
        self.executor.run()
        self.assertEqual(self.window.now_playing_index, 0)
        self.controller.set_repeat(True)
        for expected in (1, 0):
            self.controller.audio_ended()
            self.executor.run()
            self.assertEqual(self.window.now_playing_index, expected)

    def test_playing_queue_does_not_send_an_index_from_another_view(self):
        self.open_two_track_queue()
        self.controller.search('new-view')
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertIsNone(self.window.now_playing_index)

    def test_wave_highlight_distinguishes_equal_track_occurrences(self):
        self.login()
        first, duplicate = Track('duplicate'), Track('duplicate')
        class Wave:
            def __init__(self, service): pass
            def start(self): return [first, duplicate]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.window.now_playing_index, 1)

    def test_only_latest_track_resolves_to_audio(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller.play_track(first)
        self.controller.play_track(second)
        self.executor.run(1)
        self.executor.run(0)
        self.assertEqual(self.player.urls, ['https://music.example/second'])

    def test_multiple_skips_while_paused_keep_the_final_track_paused(self):
        self.login()
        first, second, third = Track('first'), Track('second'), Track('third')
        self.controller._visible_tracks = [first, second, third]
        self.controller.play_track(first)
        self.executor.run()
        self.controller.toggle_playback()
        self.controller.next_track()
        self.controller.next_track()
        self.executor.run()  # superseded second track
        self.executor.run()  # third track
        self.assertEqual(self.player.urls[-1], 'https://music.example/third')
        self.assertFalse(self.player.playing)

    def test_repeated_mpris_pause_and_play_are_idempotent(self):
        from gi.repository import GLib
        from impuls_player.mpris import Mpris
        from impuls_player.service import Track as DomainTrack
        class Invocation:
            error = None
            def return_value(self, value): pass
            def return_dbus_error(self, name, message): self.error = name
        self.login()
        track = DomainTrack('1', 'Song', ('Artist',), '', 60000)
        self.controller.play_track(track)
        self.executor.run()
        mpris = Mpris(self.controller)
        try:
            mpris.set_track(track)
            mpris.set_playback(True, 0, 60000)
            for command in ('Pause', 'Pause'):
                call = Invocation()
                mpris._on_method_call(None, None, None, 'org.mpris.MediaPlayer2.Player',
                                      command, GLib.Variant('()', ()), call)
                self.assertIsNone(call.error)
            self.assertFalse(self.player.playing)
            for command in ('Play', 'Play'):
                call = Invocation()
                mpris._on_method_call(None, None, None, 'org.mpris.MediaPlayer2.Player',
                                      command, GLib.Variant('()', ()), call)
                self.assertIsNone(call.error)
            self.assertTrue(self.player.playing)
        finally:
            mpris.close()

    def test_mpris_pause_keeps_a_stopped_player_stopped(self):
        from gi.repository import GLib
        from impuls_player.mpris import Mpris
        from impuls_player.service import Track as DomainTrack
        class Invocation:
            def return_value(self, value): pass
            def return_dbus_error(self, name, message): raise AssertionError(name)
        self.login()
        track = DomainTrack('1', 'Song', ('Artist',), '', 60000)
        self.controller.play_track(track)
        self.executor.run()
        self.controller.stop_playback()
        mpris = Mpris(self.controller)
        try:
            mpris.set_track(track)
            mpris.set_playback(False, 0, 60000, stopped=True)
            mpris._on_method_call(None, None, None, 'org.mpris.MediaPlayer2.Player',
                                  'Pause', GLib.Variant('()', ()), Invocation())
            value = mpris._on_get_property(None, None, None, 'org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
            self.assertEqual(value.unpack(), 'Stopped')
        finally:
            mpris.close()

    def test_mpris_pause_during_loading_keeps_one_request_and_pauses(self):
        from gi.repository import GLib
        from impuls_player.mpris import Mpris
        from impuls_player.service import Track as DomainTrack
        class Invocation:
            error = None
            def return_value(self, value): pass
            def return_dbus_error(self, name, message): self.error = name
        self.login()
        track = DomainTrack('1', 'Song', ('Artist',), '', 60000)
        self.controller.play_track(track)
        self.assertEqual(len(self.executor.jobs), 1)
        mpris = Mpris(self.controller)
        try:
            mpris.set_track(track)
            mpris.set_playback(self.controller.display_playing, 0, 60000,
                               stopped=not self.player.has_source and not self.controller.playback_pending)
            call = Invocation()
            mpris._on_method_call(None, None, None, 'org.mpris.MediaPlayer2.Player',
                                  'Pause', GLib.Variant('()', ()), call)
            self.assertIsNone(call.error)
            self.assertEqual(len(self.executor.jobs), 1)
            self.executor.run()
            self.assertFalse(self.player.playing)
        finally:
            mpris.close()

    def test_toggling_during_loading_changes_intent_without_restarting_request(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller._visible_tracks = [first, second]
        self.controller.play_track(first)
        self.executor.run()
        self.controller.toggle_playback()  # pause
        self.controller.next_track()      # load paused
        self.controller.toggle_playback() # resume while still loading
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertTrue(self.player.playing)

    def test_skipping_while_paused_keeps_the_next_track_paused(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller._visible_tracks = [first, second]
        self.controller.play_track(first)
        self.executor.run()
        self.controller.toggle_playback()
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls[-1], 'https://music.example/second')
        self.assertFalse(self.player.playing)

    def test_play_after_queue_ends_restarts_current_track(self):
        self.login()
        self.controller.play_track(Track('one'))
        self.executor.run()
        self.controller.audio_ended()
        self.controller.toggle_playback()
        self.executor.run()
        self.assertEqual(self.player.urls, ['https://music.example/one', 'https://music.example/one'])

    def test_logout_keyring_deletion_survives_closing_the_app(self):
        started, release, cleared = Event(), Event(), Event()
        class BlockingStore(Store):
            def save(self, token):
                started.set()
                release.wait(2)
                super().save(token)
            def clear(self):
                super().clear()
                cleared.set()
        store = BlockingStore()
        controller = Controller(Window(), Player(), store, Service, dispatch=lambda fn: fn())
        try:
            controller.login('secret', True)
            self.assertTrue(started.wait(2))
            controller.logout()
            controller.close()
            release.set()
            self.assertTrue(cleared.wait(2))
            self.assertIsNone(store.token)
        finally:
            release.set()

    def test_stopped_track_can_be_restarted_with_a_fresh_url(self):
        self.login()
        self.controller.play_track(Track('one'))
        self.executor.run()
        self.controller.stop_playback()
        self.controller.restart_current()
        self.executor.run()
        self.assertEqual(self.player.urls, ['https://music.example/one', 'https://music.example/one'])

    def test_end_of_queue_cancels_an_inflight_audio_request(self):
        self.login()
        self.controller.play_track(Track('only'))
        self.controller.next_track()
        self.executor.run()
        self.assertEqual(self.player.urls, [])

    def test_logout_does_not_wait_for_a_blocked_secret_service(self):
        started, release, returned = Event(), Event(), Event()
        class BlockingStore(Store):
            def save(self, token):
                started.set()
                release.wait(2)
                super().save(token)
        self.controller.store = BlockingStore()
        self.controller.login('secret', True)
        self.executor.run()
        worker = Thread(target=self.executor.run)
        worker.start()
        self.assertTrue(started.wait(1))
        logout = Thread(target=lambda: (self.controller.logout(), returned.set()))
        logout.start()
        try:
            self.assertTrue(returned.wait(0.3), 'logout waited for the keyring')
        finally:
            release.set()
            worker.join(2)
            logout.join(2)

    def test_wave_feedback_failure_is_visible_without_stopping_playback(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def track_started(self, track): raise RuntimeError('secret-url')
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.playback_state(True)
        self.executor.run()
        self.assertIn('обратн', self.window.status.lower())
        self.assertNotIn('secret-url', self.window.status)
        self.assertTrue(self.player.has_source)

    def test_wave_feedback_rejection_is_visible(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def track_started(self, track): return False
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.playback_state(True)
        self.executor.run()
        self.assertIn('обратн', self.window.status.lower())

    def test_wave_reports_started_and_skip_then_plays_next_track(self):
        self.login()
        events = []
        tracks = [Track('wave1'), Track('wave2')]
        class Wave:
            def __init__(self, service): pass
            def start(self): return tracks
            def track_started(self, track): events.append(('started', track.id))
            def track_skipped(self, track, seconds): events.append(('skipped', track.id))
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()  # session
        self.executor.run()  # stream URL
        self.controller.playback_state(True)
        self.executor.run()  # started feedback
        self.controller.next_track()
        self.executor.run()  # skipped feedback
        self.executor.run()  # next stream URL
        self.assertEqual(events, [('started', 'wave1'), ('skipped', 'wave1')])
        self.assertEqual(self.player.urls[-1], 'https://music.example/wave2')

    def test_search_selection_exits_wave_even_if_the_same_track_is_listed(self):
        self.login()
        events = []
        shared, ordinary = Track('shared'), Track('ordinary')
        class Wave:
            def __init__(self, service): pass
            def start(self): return [shared]
            def track_started(self, track): events.append(track.id)
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller._service.search = lambda query: [shared, ordinary]
        self.controller.search('query')
        self.executor.run()
        self.controller.play_track(shared)
        self.executor.run()
        self.controller.playback_state(True)
        self.assertEqual(events, [])
        self.controller.next_track()
        self.executor.run()
        self.controller.playback_state(True)
        self.assertEqual(events, [])

    def test_manual_skip_invalidates_a_pending_wave_start(self):
        self.login()
        first, second = Track('first'), Track('second')
        self.controller._visible_tracks = [first, second]
        self.controller.play_track(first)
        self.executor.run()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.controller.next_track()
        self.executor.run()  # stale wave response
        self.executor.run()  # second track URL
        self.assertEqual(self.player.urls, ['https://music.example/first', 'https://music.example/second'])
        self.assertEqual(self.controller.current_track, second)

    def test_wave_continuation_does_not_replace_open_search_results(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.search('found')
        self.executor.run()
        self.assertEqual(self.window.tracks, [Track('found')])
        self.controller.audio_ended()
        self.executor.run()  # continuation
        self.executor.run()  # stream URL
        self.assertEqual(self.window.tracks, [Track('found')])
        self.assertEqual(self.controller.current_track, Track('wave2'))

    def test_previous_track_crosses_wave_batch_boundary(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1'), Track('wave2')]
            def next_batch(self): return [Track('wave3')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.controller.audio_ended()
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.controller.current_track, Track('wave3'))
        self.controller.previous_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track, Track('wave2'))
        self.assertEqual(self.player.urls[-1], 'https://music.example/wave2')

    def test_previous_during_wave_fetch_does_not_discard_the_batch(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1'), Track('wave2')]
            def next_batch(self): return [Track('wave3')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.executor.run()
        self.controller.next_track()  # fetch another batch
        self.controller.previous_track()  # ignored until fetch completes
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        self.executor.run()
        self.assertEqual(self.controller.current_track, Track('wave3'))
        self.controller.previous_track()
        self.executor.run()
        self.assertEqual(self.controller.current_track, Track('wave2'))

    def test_repeated_next_during_wave_fetch_does_not_skip_a_batch(self):
        self.login()
        calls = []
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self):
                calls.append('fetch')
                return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()
        self.controller.next_track()
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        self.executor.run()
        self.assertEqual(calls, ['fetch'])
        self.assertEqual(self.controller.current_track, Track('wave2'))

    def test_wave_fetches_another_batch_at_the_end(self):
        self.login()
        class Wave:
            def __init__(self, service): pass
            def start(self): return [Track('wave1')]
            def next_batch(self): return [Track('wave2')]
        self.controller.wave_factory = Wave
        self.controller.wave()
        self.executor.run()
        self.executor.run()
        self.controller.next_track()  # no started event: nothing to report
        self.executor.run()  # next batch
        self.executor.run()  # next URL
        self.assertEqual(self.player.urls[-1], 'https://music.example/wave2')

    def test_volume_changes_update_the_visible_control(self):
        self.controller.set_volume(0.35)
        self.assertAlmostEqual(self.window.volume, 0.35)

    def test_failed_new_login_cannot_cancel_pending_logout_deletion(self):
        self.store.token = 'old-token'
        self.controller.logout()
        class FailingService(Service):
            def connect(self): raise ValueError('invalid')
        self.controller.service_factory = FailingService
        self.controller.login('wrong', False)
        self.executor.run()  # pending deletion
        self.executor.run()  # failed sign-in
        self.assertIsNone(self.store.token)

    def test_login_without_remembering_clears_an_older_token(self):
        self.store.token = 'old-token'
        self.login()
        self.assertIsNone(self.store.token)

    def test_network_exception_never_displays_exception_or_token(self):
        class FailingService(Service):
            def connect(self): raise ValueError('secret https://private.invalid')
        self.controller.service_factory = FailingService
        self.controller.login('secret', True)
        self.executor.run()
        self.assertTrue(self.window.errors)
        self.assertNotIn('secret', self.window.errors[0])
        self.assertNotIn('private.invalid', self.window.errors[0])


if __name__ == '__main__': unittest.main()
