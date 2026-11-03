import array
import math
import sys
import tempfile
import threading
import unittest
import wave
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from impuls_player.player import AudioPlayer, safe_stream_url

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import GLib, Gst
    Gst.init(None)
except (ImportError, ValueError):
    Gst = None


class StreamUrlTests(unittest.TestCase):
    def test_accepts_an_https_url(self):
        self.assertEqual(safe_stream_url('https://s50vla.storage.yandex.net/audio?id=1'), 'https://s50vla.storage.yandex.net/audio?id=1')

    def test_rejects_unsafe_or_local_urls(self):
        for url in ('http://cdn.example.com/audio', 'file:///etc/passwd', 'https://user:pass@cdn.example.com/',
                    'https://127.0.0.1/test', 'https://localhost/test', 'https://server.local/test',
                    'https://cdn.example.com:444/test', 'https://cdn.example.com/\nheader',
                    'https://2130706433/', 'https://127.1/', 'https://0x7f000001/',
                    'https://localhost./', 'https://localhost.localdomain/', 'https://localhost6.localdomain6/',
                    'https://cdn.example.com/audio', 'https://yandex.net.evil.test/audio',
                    r'https://127.0.0.1\.yandex.net/audio',
                    r'https://evil.test\.yandex.net/audio', 'https://yandex.net%2Fevil.test/audio'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_stream_url(url)

    def test_gstreamer_http_source_disables_automatic_redirects(self):
        class Source:
            def __init__(self): self.options = {}
            def find_property(self, name): return name == 'automatic-redirect'
            def set_property(self, name, value): self.options[name] = value
        source = Source()
        AudioPlayer._configure_source(None, source)
        self.assertIs(source.options['automatic-redirect'], False)


class NetworkSourceTests(unittest.TestCase):
    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_http_sources_enforce_timeout_tls_and_no_redirect(self):
        for name in ('souphttpsrc', 'curlhttpsrc'):
            with self.subTest(source=name):
                source = Gst.ElementFactory.make(name, None)
                if source is None:
                    continue
                AudioPlayer._configure_source(None, source)
                self.assertEqual(source.get_property('timeout'), 15)
                self.assertTrue(source.get_property('ssl-strict'))
                self.assertFalse(source.get_property('automatic-redirect'))


class EqualizerTests(unittest.TestCase):
    @staticmethod
    def fake_gst(equalizer, spectrum=None):
        gst = MagicMock()
        playbin = MagicMock()
        gst.ElementFactory.make.side_effect = lambda name, element_name: (
            playbin if name == 'playbin' else equalizer if name == 'equalizer-10bands'
            else spectrum if name == 'spectrum' else None
        )
        return gst, playbin

    def test_attaches_equalizer_to_playbin_and_changes_band(self):
        equalizer = MagicMock()
        gst, playbin = self.fake_gst(equalizer)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, gst=gst)
        self.assertTrue(player.equalizer_available)
        playbin.set_property.assert_any_call('audio-filter', equalizer)
        player.set_equalizer(5, 4.5)
        equalizer.set_property.assert_called_with('band5', 4.5)

    def test_missing_element_keeps_player_available_but_reports_missing_equalizer(self):
        gst, playbin = self.fake_gst(None)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, gst=gst)
        self.assertFalse(player.equalizer_available)
        with self.assertRaises(RuntimeError):
            player.set_equalizer(1, 3)
        playbin.set_property.assert_any_call('volume', 0.75)

    def test_invalid_and_nonfinite_values_never_reach_element(self):
        equalizer = MagicMock()
        gst, _ = self.fake_gst(equalizer)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, gst=gst)
        equalizer.reset_mock()
        for index, gain in ((-1, 0), (10, 0), (True, 0), (1.5, 0), (0, float('nan')),
                            (0, float('inf')), (0, '3'), (0, True)):
            with self.subTest(index=index, gain=gain), self.assertRaises(ValueError):
                player.set_equalizer(index, gain)
        equalizer.set_property.assert_not_called()

    def test_gains_are_clamped_to_the_displayed_range(self):
        equalizer = MagicMock()
        gst, _ = self.fake_gst(equalizer)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, gst=gst)
        player.set_equalizer(0, -90)
        equalizer.set_property.assert_called_with('band0', -12.0)
        player.set_equalizer(9, 90)
        equalizer.set_property.assert_called_with('band9', 12.0)

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_actual_gstreamer_element_is_connected_and_configurable(self):
        if Gst.ElementFactory.find('equalizer-10bands') is None:
            self.skipTest('GStreamer equalizer plugin unavailable')
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None)
        try:
            filter_bin = player._player.get_property('audio-filter')
            equalizer = (filter_bin.get_by_name('impuls-equalizer')
                         if player.spectrum_available else filter_bin)
            self.assertIsNotNone(equalizer)
            self.assertEqual(equalizer.get_factory().get_name(), 'equalizer-10bands')
            self.assertTrue(all(equalizer.get_property(f'band{i}') == 0 for i in range(10)))
            player.set_equalizer(6, 8.25)
            self.assertAlmostEqual(equalizer.get_property('band6'), 8.25)
        finally:
            player.close()

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_equalizer_changes_the_audio_signal_in_playbin(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('equalizer-10bands', 'appsink', 'wavparse')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')

        def measure(path, gain):
            player = AudioPlayer(lambda: None, lambda: None, lambda state: None)
            sink = Gst.ElementFactory.make('appsink', None)
            sink.set_property('sync', False)
            sink.set_property('caps', Gst.Caps.from_string('audio/x-raw,format=S16LE,channels=1,rate=48000'))
            player._player.set_property('audio-sink', sink)
            player._player.set_property('uri', path.as_uri())
            player.set_equalizer(5, gain)  # This band's center is approximately 947 Hz.
            player._player.set_state(Gst.State.PLAYING)
            samples = array.array('h')
            try:
                while True:
                    sample = sink.emit('try-pull-sample', 2 * Gst.SECOND)
                    if sample is None:
                        break
                    buffer = sample.get_buffer()
                    samples.frombytes(buffer.extract_dup(0, buffer.get_size()))
            finally:
                player.close()
            self.assertGreater(len(samples), 1000, 'The audio pipeline must produce samples')
            if sys.byteorder != 'little':
                samples.byteswap()
            return math.sqrt(sum(int(value) ** 2 for value in samples) / len(samples))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tone.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                samples = array.array('h', (int(2500 * math.sin(2 * math.pi * 947 * i / 48000))
                                             for i in range(24000)))
                if sys.byteorder != 'little':
                    samples.byteswap()
                wav.writeframes(samples.tobytes())
            flat = measure(path, 0)
            boosted = measure(path, 12)
        self.assertGreater(boosted, flat * 2, 'Boosting the band must actually change decoded audio')


class SpectrumTests(unittest.TestCase):
    def test_missing_spectrum_keeps_equalizer_and_audio(self):
        equalizer = MagicMock()
        gst, playbin = EqualizerTests.fake_gst(equalizer)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        self.assertFalse(player.spectrum_available)
        self.assertTrue(player.equalizer_available)
        playbin.set_property.assert_any_call('audio-filter', equalizer)
        player.set_equalizer(2, 1)
        equalizer.set_property.assert_called_with('band2', 1.0)

    def test_missing_equalizer_still_attaches_spectrum(self):
        spectrum = MagicMock()
        gst, playbin = EqualizerTests.fake_gst(None, spectrum)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        self.assertFalse(player.equalizer_available)
        self.assertTrue(player.spectrum_available)
        playbin.set_property.assert_any_call('audio-filter', spectrum)

    def test_both_elements_are_in_order_in_a_bin(self):
        equalizer, spectrum = MagicMock(), MagicMock()
        gst, playbin = EqualizerTests.fake_gst(equalizer, spectrum)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        self.assertTrue(player.equalizer_available)
        self.assertTrue(player.spectrum_available)
        equalizer.link.assert_called_once_with(spectrum)
        playbin.set_property.assert_any_call('audio-filter', gst.Bin.new.return_value)

    def test_failed_link_falls_back_to_equalizer(self):
        equalizer, spectrum = MagicMock(), MagicMock()
        equalizer.link.return_value = False
        gst, playbin = EqualizerTests.fake_gst(equalizer, spectrum)
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        self.assertTrue(player.equalizer_available)
        self.assertFalse(player.spectrum_available)
        playbin.set_property.assert_any_call('audio-filter', equalizer)
        player.set_equalizer(1, 2)
        equalizer.set_property.assert_called_with('band1', 2.0)

    def test_link_failure_still_detaches_equalizer_when_spectrum_removal_fails(self):
        equalizer, spectrum = MagicMock(), MagicMock()
        equalizer.link.return_value = False
        gst, _ = EqualizerTests.fake_gst(equalizer, spectrum)
        gst.Bin.new.return_value.remove.side_effect = [RuntimeError('no spectrum child'), True]
        AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        gst.Bin.new.return_value.remove.assert_any_call(equalizer)

    def test_failed_detachment_does_not_attach_a_parented_equalizer(self):
        equalizer, spectrum = MagicMock(), MagicMock()
        equalizer.link.return_value = False
        equalizer.get_parent.return_value = object()
        gst, playbin = EqualizerTests.fake_gst(equalizer, spectrum)
        gst.Bin.new.return_value.remove.side_effect = RuntimeError('unable to detach')
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, lambda levels: None, gst=gst)
        self.assertFalse(player.equalizer_available)
        self.assertFalse(player.spectrum_available)
        self.assertFalse(any(call.args[0] == 'audio-filter' for call in playbin.set_property.call_args_list))

    def test_missing_both_filters_still_creates_a_player(self):
        gst, playbin = EqualizerTests.fake_gst(None)
        frames = []
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, frames.append, gst=gst)
        self.assertFalse(player.equalizer_available)
        self.assertFalse(player.spectrum_available)
        playbin.set_property.assert_any_call('volume', 0.75)
        player.stop()
        self.assertEqual(frames[-1], (0.0,) * 32)

    def test_unrelated_or_malformed_spectrum_messages_are_ignored(self):
        spectrum = MagicMock()
        gst, _ = EqualizerTests.fake_gst(None, spectrum)
        frames = []
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, frames.append, gst=gst)
        player._has_source = player._playing = True
        bad = SimpleNamespace(get_name=lambda: 'spectrum',
                              get_list=lambda name: (True, SimpleNamespace(n_values=5)))
        for source in (spectrum, MagicMock()):
            message = SimpleNamespace(type=gst.MessageType.ELEMENT, src=source, get_structure=lambda: bad)
            player._on_message(None, message)
        self.assertEqual(frames, [])

    def test_bus_normalizes_values_and_pause_and_stop_clear_them(self):
        spectrum = MagicMock()
        gst, _ = EqualizerTests.fake_gst(MagicMock(), spectrum)
        frames = []
        player = AudioPlayer(lambda: None, lambda: None, lambda state: None, frames.append, gst=gst)
        player._has_source = True
        player._playing = True
        # Higher-resolution FFT buckets are grouped into 32 logarithmic bars.
        values = [-80.0, -60.0, -20.0, 0.0, 5.0, float('nan'), float('inf')] + [-80.0] * 505
        boxed = SimpleNamespace(n_values=512, get_nth=lambda index: values[index])
        structure = SimpleNamespace(get_name=lambda: 'spectrum', get_list=lambda name: (True, boxed))
        message = SimpleNamespace(type=gst.MessageType.ELEMENT, src=spectrum, get_structure=lambda: structure)
        player._on_message(None, message)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[-1]), 32)
        self.assertTrue(all(math.isfinite(value) and 0 <= value <= 1 for value in frames[-1]))
        self.assertEqual(frames[-1][0], 0.0)
        self.assertGreater(frames[-1][2], frames[-1][1])
        self.assertEqual(frames[-1][3], 1.0)
        player.toggle()
        self.assertEqual(frames[-1], (0.0,) * 32)
        before = len(frames)
        player._on_message(None, message)  # A queued frame must not revive a paused visualizer.
        self.assertEqual(len(frames), before)
        player.stop()
        self.assertEqual(frames[-1], (0.0,) * 32)

    def test_playback_error_clears_spectrum(self):
        spectrum = MagicMock()
        gst, _ = EqualizerTests.fake_gst(None, spectrum)
        frames = []
        errors = []
        player = AudioPlayer(lambda: None, lambda: errors.append(True), lambda state: None, frames.append, gst=gst)
        player._has_source = True
        player._playing = True
        player._on_message(None, SimpleNamespace(type=gst.MessageType.ERROR))
        self.assertEqual(frames[-1], (0.0,) * 32)
        self.assertEqual(errors, [True])

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_real_audio_emits_spectrum_on_the_main_context(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('equalizer-10bands', 'spectrum', 'fakesink', 'wavparse')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')
        frames = []
        loop = GLib.MainLoop()
        main_thread = threading.get_ident()
        player = AudioPlayer(loop.quit, lambda: loop.quit(), lambda state: None,
                             lambda levels: frames.append((threading.get_ident(), levels)))
        sink = Gst.ElementFactory.make('fakesink', None)
        sink.set_property('sync', False)
        player._player.set_property('audio-sink', sink)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tone.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                samples = array.array('h', (int(6000 * math.sin(2 * math.pi * 1500 * i / 48000))
                                             for i in range(24000)))
                if sys.byteorder != 'little':
                    samples.byteswap()
                wav.writeframes(samples.tobytes())
            player._player.set_property('uri', path.as_uri())
            player._has_source = True
            player._playing = True
            timeout = GLib.timeout_add(3000, lambda: (loop.quit(), False)[1])
            try:
                player._player.set_state(Gst.State.PLAYING)
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', category=DeprecationWarning, module=r'gi\.events')
                    loop.run()
            finally:
                GLib.source_remove(timeout)
                player.close()
        audible = [values for thread, values in frames if max(values) > 0]
        self.assertTrue(audible, 'A real sine wave must generate a nonzero spectrum')
        self.assertTrue(all(thread == main_thread for thread, _ in frames))
        peak = max(range(32), key=lambda index: audible[0][index])
        self.assertIn(peak, (16, 17, 18))  # 1.5 kHz on the logarithmic scale at 48 kHz.
        self.assertGreater(audible[0][peak], audible[0][28] + 0.2)

if __name__ == '__main__': unittest.main()
