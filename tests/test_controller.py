import unittest
from threading import Event, Thread
from concurrent.futures import Future
from dataclasses import dataclass

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
    def set_playlists(self, playlists): pass
    def set_status(self, message): self.status = message
    def set_busy(self, busy): pass
    def set_now_playing(self, track): pass
    def set_playback(self, playing, position_ms=0, duration_ms=0): pass
    def show_error(self, message): self.errors.append(message)
    def show_device_code(self, code, url): pass
    def set_volume(self, value): self.volume = value
    def set_equalizer_available(self, available): self.equalizer_available = available
    def set_equalizer_values(self, values): self.equalizer_values = tuple(values)
    def set_shuffle_state(self, enabled): self.shuffle_state = enabled
    def set_repeat_state(self, enabled): self.repeat_state = enabled


class Player:
    def __init__(self):
        self.urls = []; self.has_source = False; self.playing = False
        self.stop_count = 0
        self.equalizer_available = True; self.equalizer_changes = []
    def stop(self): self.has_source = False; self.playing = False; self.stop_count += 1
    def play_url(self, url, autoplay=True):
        self.urls.append(url); self.has_source = True; self.playing = autoplay
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
    def stream_url(self, track): return 'https://music.example/' + track.id


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
