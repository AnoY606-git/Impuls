import os
import subprocess
import time
import unittest

from gi.repository import GLib

from impuls_player.controller import Controller
from impuls_player.mpris import Mpris, metadata_for_track, seek_fraction, track_path
from impuls_player.service import Track


class MetadataTests(unittest.TestCase):
    def test_metadata_contains_typed_values_without_media_url(self):
        track = Track("123:45", "Song", ("Artist", "Guest"), "Album", 154321,
                      "https://example.invalid/private-cover?token=secret")
        metadata = metadata_for_track(track)

        self.assertEqual(metadata["mpris:trackid"].get_type_string(), "o")
        self.assertEqual(metadata["mpris:length"].get_type_string(), "x")
        self.assertEqual(metadata["mpris:length"].unpack(), 154321000)
        self.assertEqual(metadata["xesam:artist"].get_type_string(), "as")
        self.assertEqual(metadata["xesam:artist"].unpack(), ["Artist", "Guest"])
        self.assertEqual(metadata["xesam:title"].unpack(), "Song")
        self.assertEqual(metadata["xesam:album"].unpack(), "Album")
        self.assertNotIn("xesam:url", metadata)
        self.assertNotIn("mpris:artUrl", metadata)
        self.assertNotIn("secret", str(metadata))
        self.assertTrue(GLib.Variant.is_object_path(metadata["mpris:trackid"].unpack()))

    def test_no_track_uses_the_standard_no_track_object_path(self):
        metadata = metadata_for_track(None)
        self.assertEqual(metadata["mpris:trackid"].unpack(),
                         "/org/mpris/MediaPlayer2/TrackList/NoTrack")
        self.assertNotIn("mpris:length", metadata)

    def test_distinct_track_ids_have_distinct_stable_dbus_paths(self):
        first = Track("1:23", "a", (), "", 0)
        second = Track("12:3", "b", (), "", 0)
        self.assertNotEqual(track_path(first), track_path(second))
        self.assertEqual(track_path(first), track_path(first))
        self.assertTrue(GLib.Variant.is_object_path(track_path(first)))

    def test_metadata_normalizes_invalid_duration(self):
        metadata = metadata_for_track(Track("99", "title", (), "", -1))
        self.assertNotIn("mpris:length", metadata)

    def test_metadata_sanitizes_nuls_and_invalid_unicode(self):
        metadata = metadata_for_track(Track("1", "bad\x00\ud800title", ("a\x00b",), "", 1000))
        self.assertEqual(metadata["xesam:title"].unpack(), "bad?title")
        self.assertEqual(metadata["xesam:artist"].unpack(), ["ab"])


class SeekTests(unittest.TestCase):
    def test_seek_clamps_positions_to_track_duration(self):
        self.assertEqual(seek_fraction(-100, 1000), 0.0)
        self.assertEqual(seek_fraction(500, 1000), 0.5)
        self.assertEqual(seek_fraction(1200, 1000), 1.0)

    def test_seek_requires_known_positive_duration(self):
        self.assertIsNone(seek_fraction(1, 0))
        self.assertIsNone(seek_fraction(1, -100))


class _FakePlayer:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class _FakeController:
    def __init__(self):
        self.player = _FakePlayer()
        self.calls = []
        self.shuffle_enabled = False
        self.repeat_enabled = False

    def set_shuffle(self, value):
        self.shuffle_enabled = value
        self.calls.append(('shuffle', value))

    def set_repeat(self, value):
        self.repeat_enabled = value
        self.calls.append(('repeat', value))

    def toggle_playback(self):
        self.calls.append(("toggle",))

    def next_track(self):
        self.calls.append(("next",))

    def previous_track(self):
        self.calls.append(("previous",))

    def seek(self, fraction):
        self.calls.append(("seek", fraction))

    def set_volume(self, fraction):
        self.calls.append(("volume", fraction))

    def restart_current(self):
        self.calls.append(("restart",))

    def stop_playback(self):
        self.calls.append(("stop",))
        self.player.stop()


class _Invocation:
    def __init__(self):
        self.value = None
        self.error = None

    def return_value(self, value):
        self.value = value

    def return_dbus_error(self, name, message):
        self.error = name, message


class MprisDispatchTests(unittest.TestCase):
    def setUp(self):
        self.controller = _FakeController()
        self.mpris = Mpris(self.controller)
        self.addCleanup(self.mpris.close)
        self.track = Track("8:19", "Song", ("Artist",), "Album", 60000)
        self.mpris.set_track(self.track)
        self.mpris.set_playback(True, 20000, 60000)

    def call_player(self, name, parameters=GLib.Variant("()", ())):
        reply = _Invocation()
        self.mpris._on_method_call(None, None, None, "org.mpris.MediaPlayer2.Player",
                                   name, parameters, reply)
        self.assertIsNone(reply.error)
        return reply

    def test_set_position_rejects_stale_track_and_out_of_bounds(self):
        stale = track_path(Track("90", "Old", (), "", 1))
        self.call_player("SetPosition", GLib.Variant("(ox)", (stale, 15000000)))
        self.call_player("SetPosition", GLib.Variant("(ox)", (track_path(self.track), -1)))
        self.call_player("SetPosition", GLib.Variant("(ox)", (track_path(self.track), 60000001)))
        self.assertEqual(self.controller.calls, [])
        self.call_player("SetPosition", GLib.Variant("(ox)", (track_path(self.track), 15000000)))
        self.assertEqual(self.controller.calls, [("seek", 0.25)])

    def test_relative_seek_before_start_clamps_and_beyond_end_skips(self):
        self.call_player("Seek", GLib.Variant("(x)", (-30000000,)))
        self.assertEqual(self.controller.calls, [("seek", 0.0)])
        self.mpris.set_playback(True, 20000, 60000)
        self.call_player("Seek", GLib.Variant("(x)", (50000000,)))
        self.assertEqual(self.controller.calls[-1], ("next",))

    def test_rejected_engine_seek_does_not_change_position_or_emit_seeked(self):
        controller = Controller.__new__(Controller)
        controller.player = _FakePlayer()
        controller.player.seek = lambda _fraction: None
        self.mpris._controller = controller
        signals = []
        class Connection:
            def emit_signal(self, *args): signals.append(args)
        connection = self.mpris._connection
        try:
            self.mpris._connection = Connection()
            self.call_player('SetPosition', GLib.Variant('(ox)', (track_path(self.track), 15000000)))
            self.assertEqual(self.mpris._position_ms, 20000)
            self.assertEqual(signals, [])
        finally:
            self.mpris._connection = connection

    def test_seek_publishes_the_position_accepted_by_the_engine(self):
        controller = Controller.__new__(Controller)
        controller.player = _FakePlayer()
        controller.player.seek = lambda _fraction: 14321
        self.mpris._controller = controller
        signals = []
        class Connection:
            def emit_signal(self, *args): signals.append(args)
        connection = self.mpris._connection
        try:
            self.mpris._connection = Connection()
            self.call_player('SetPosition', GLib.Variant('(ox)', (track_path(self.track), 15000000)))
            self.assertEqual(self.mpris._position_ms, 14321)
            self.assertEqual(controller._last_position_ms, 14321)
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0][3], 'Seeked')
            self.assertEqual(signals[0][4].unpack(), (14321000,))
        finally:
            self.mpris._connection = connection

    def test_pause_and_play_do_not_toggle_if_already_in_that_state(self):
        self.call_player("Play")
        self.call_player("Pause")
        self.assertEqual(self.controller.calls, [("toggle",)])
        self.mpris.set_playback(False, 20000, 60000)
        self.call_player("Pause")
        self.call_player("Play")
        self.assertEqual(self.controller.calls, [("toggle",), ("toggle",)])

    def test_get_property_uses_mpris_dbus_types_and_updated_state(self):
        get = lambda name: self.mpris._on_get_property(None, None, None,
                                                       "org.mpris.MediaPlayer2.Player", name)
        self.assertEqual((get("PlaybackStatus").get_type_string(), get("PlaybackStatus").unpack()),
                         ("s", "Playing"))
        self.assertEqual((get("Position").get_type_string(), get("Position").unpack()),
                         ("x", 20000000))
        self.assertEqual(get("Metadata").get_type_string(), "a{sv}")
        self.assertEqual(get("CanSeek").unpack(), True)

    def test_external_volume_is_clamped_and_ui_updates_do_not_call_controller(self):
        setter = self.mpris._on_set_property
        self.assertTrue(setter(None, None, None, "org.mpris.MediaPlayer2.Player",
                               "Volume", GLib.Variant("d", 1.5)))
        self.assertEqual(self.controller.calls, [("volume", 1.0)])
        self.mpris.set_volume(0.2)
        self.assertEqual(self.controller.calls, [("volume", 1.0)])
        value = self.mpris._on_get_property(None, None, None, "org.mpris.MediaPlayer2.Player", "Volume")
        self.assertAlmostEqual(value.unpack(), 0.2)

    def test_shuffle_and_repeat_are_writable_from_desktop_controls(self):
        setter = self.mpris._on_set_property
        get = lambda name: self.mpris._on_get_property(None, None, None,
                                                       "org.mpris.MediaPlayer2.Player", name).unpack()
        self.assertTrue(setter(None, None, None, "org.mpris.MediaPlayer2.Player", "Shuffle", GLib.Variant("b", True)))
        self.assertTrue(setter(None, None, None, "org.mpris.MediaPlayer2.Player", "LoopStatus", GLib.Variant("s", "Playlist")))
        self.assertEqual(self.controller.calls, [('shuffle', True), ('repeat', True)])
        self.assertTrue(get('Shuffle'))
        self.assertEqual(get('LoopStatus'), 'Playlist')
        self.assertFalse(setter(None, None, None, "org.mpris.MediaPlayer2.Player", "LoopStatus", GLib.Variant("s", "Track")))

    def test_modes_changed_in_app_are_published_without_calling_controller(self):
        self.mpris.set_modes(True, True)
        get = lambda name: self.mpris._on_get_property(None, None, None,
                                                       "org.mpris.MediaPlayer2.Player", name).unpack()
        self.assertTrue(get('Shuffle'))
        self.assertEqual(get('LoopStatus'), 'Playlist')
        self.assertEqual(self.controller.calls, [])

    def test_stop_then_play_restarts_current_track_without_losing_queue(self):
        self.call_player("Stop")
        self.assertEqual(self.controller.player.stops, 1)
        self.mpris.set_playback(False, 0, 60000, stopped=True)
        self.call_player("Play")
        self.assertEqual(self.controller.calls, [("stop",), ("restart",)])

    def test_stopped_state_is_distinct_from_pause(self):
        self.mpris.set_playback(False, 0, 60000, stopped=True)
        get = lambda: self.mpris._on_get_property(None, None, None,
                                                   "org.mpris.MediaPlayer2.Player", "PlaybackStatus").unpack()
        self.assertEqual(get(), "Stopped")
        self.mpris.set_playback(False, 20000, 60000, stopped=False)
        self.assertEqual(get(), "Paused")

    def test_rate_cannot_be_set_outside_supported_range(self):
        setter = self.mpris._on_set_property
        for rate in (0.0, 0.5, 2.0, float("inf"), float("nan")):
            with self.subTest(rate=rate):
                self.assertFalse(setter(None, None, None, "org.mpris.MediaPlayer2.Player",
                                        "Rate", GLib.Variant("d", rate)))
        self.assertTrue(setter(None, None, None, "org.mpris.MediaPlayer2.Player",
                               "Rate", GLib.Variant("d", 1.0)))
        self.assertEqual(self.controller.calls, [])

    def test_desktop_entry_matches_installed_desktop_file(self):
        entry = self.mpris._on_get_property(None, None, None, "org.mpris.MediaPlayer2", "DesktopEntry")
        self.assertEqual(entry.unpack(), "io.github.AnoY606_git.Impuls")

    def test_disconnected_bus_does_not_break_playback_updates(self):
        class Disconnected:
            def emit_signal(self, *args):
                raise GLib.Error("session bus disconnected")

        connection = self.mpris._connection
        try:
            self.mpris._connection = Disconnected()
            self.mpris.set_playback(False, 20000, 60000)
            self.mpris.set_track(self.track)
        finally:
            self.mpris._connection = connection

    def test_failed_volume_change_does_not_publish_an_unapplied_value(self):
        def fail(_fraction):
            raise RuntimeError("device unavailable")

        self.controller.set_volume = fail
        self.assertFalse(self.mpris._on_set_property(None, None, None, "org.mpris.MediaPlayer2.Player",
                                                     "Volume", GLib.Variant("d", 0.1)))
        value = self.mpris._on_get_property(None, None, None, "org.mpris.MediaPlayer2.Player", "Volume")
        self.assertEqual(value.unpack(), 1.0)


@unittest.skipUnless(os.environ.get("IMPULS_TEST_DBUS") == "1", "requires isolated session bus")
class MprisBusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.controller = _FakeController()
        self.mpris = Mpris(self.controller)
        self.addCleanup(self.mpris.close)
        self.track = Track("123", "A Song", ("An Artist",), "Album", 60000)
        self.mpris.set_track(self.track)
        self.mpris.set_playback(True, 20000, 60000)

    def bus_call(self, method, *args):
        process = subprocess.Popen(
            ["gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.impuls",
             "--object-path", "/org/mpris/MediaPlayer2", "--method", method, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        return stdout

    def test_properties_and_commands_over_real_session_bus(self):
        root = self.bus_call("org.freedesktop.DBus.Properties.GetAll", "org.mpris.MediaPlayer2")
        self.assertIn("io.github.AnoY606_git.Impuls", root)
        props = self.bus_call("org.freedesktop.DBus.Properties.GetAll", "org.mpris.MediaPlayer2.Player")
        self.assertIn("Playing", props)
        self.assertIn("A Song", props)
        self.assertNotIn("xesam:url", props)
        self.bus_call("org.freedesktop.DBus.Properties.Set", "org.mpris.MediaPlayer2.Player",
                      "Volume", "<0.25>")
        self.assertEqual(self.controller.calls, [("volume", 0.25)])
        self.bus_call("org.mpris.MediaPlayer2.Player.SetPosition", "/not/current", "15000000")
        self.assertEqual(self.controller.calls, [("volume", 0.25)])
        self.bus_call("org.mpris.MediaPlayer2.Player.SetPosition", track_path(self.track), "15000000")
        self.assertEqual(self.controller.calls[-1], ("seek", 0.25))
        self.bus_call("org.mpris.MediaPlayer2.Player.Pause")
        self.assertEqual(self.controller.calls[-1], ("toggle",))
        self.bus_call("org.mpris.MediaPlayer2.Player.Stop")
        self.assertEqual(self.controller.calls[-1], ("stop",))
        self.bus_call("org.mpris.MediaPlayer2.Player.Play")
        self.assertEqual(self.controller.calls[-1], ("restart",))
        self.bus_call("org.freedesktop.DBus.Properties.Set", "org.mpris.MediaPlayer2.Player",
                      "Shuffle", "<true>")
        self.bus_call("org.freedesktop.DBus.Properties.Set", "org.mpris.MediaPlayer2.Player",
                      "LoopStatus", "<'Playlist'>")
        self.assertEqual(self.controller.calls[-2:], [("shuffle", True), ("repeat", True)])
        props = self.bus_call("org.freedesktop.DBus.Properties.GetAll", "org.mpris.MediaPlayer2.Player")
        self.assertIn("Playlist", props)


if __name__ == "__main__":
    unittest.main()
