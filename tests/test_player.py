import array
import io
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import threading
import time
import unittest
import wave
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
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


@unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
class ResumePlaybackTests(unittest.TestCase):
    def make_player(self):
        gst, pipeline = EqualizerTests.fake_gst(None)
        for name in ('MessageType', 'State', 'StateChangeReturn', 'SeekFlags', 'Format', 'MSECOND'):
            setattr(gst, name, getattr(Gst, name))
        pipeline.set_state.return_value = Gst.StateChangeReturn.ASYNC
        pipeline.seek_simple.return_value = True
        errors = []
        player = AudioPlayer(lambda: None, lambda: errors.append(True), lambda _playing: None, gst=gst)
        self.addCleanup(player.close)
        message = SimpleNamespace(type=Gst.MessageType.ASYNC_DONE, src=pipeline)
        return player, pipeline, message, errors

    def test_resume_prerolls_then_seeks_before_playing(self):
        player, pipeline, message, errors = self.make_player()
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PAUSED,))
        self.assertTrue(player.playing)
        pipeline.seek_simple.assert_not_called()
        player._on_message(None, message)
        pipeline.seek_simple.assert_called_once_with(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, 3500 * Gst.MSECOND)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PAUSED,))
        player._on_message(None, message)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PLAYING,))
        self.assertEqual(errors, [])

    def test_resume_respects_pause_and_cancels_on_stop_or_new_source(self):
        player, pipeline, message, _errors = self.make_player()
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        player.toggle()
        player._on_message(None, message)
        player._on_message(None, message)
        self.assertFalse(player.playing)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PAUSED,))
        pipeline.seek_simple.reset_mock()
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        player.stop()
        player._on_message(None, message)
        pipeline.seek_simple.assert_not_called()
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        player.play_url('https://s50vla.storage.yandex.net/next')
        player._on_message(None, message)
        pipeline.seek_simple.assert_not_called()

    def test_failed_resume_seek_reports_error_instead_of_playing_from_start(self):
        player, pipeline, message, errors = self.make_player()
        pipeline.seek_simple.return_value = False
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        player._on_message(None, message)
        self.assertEqual(errors, [True])
        self.assertFalse(player.has_source)
        self.assertFalse(player.playing)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.NULL,))

    def test_failed_pause_or_resume_stops_source_and_reports_error(self):
        for initially_playing in (True, False):
            with self.subTest(initially_playing=initially_playing):
                player, pipeline, _message, errors = self.make_player()
                player.play_url('https://s50vla.storage.yandex.net/audio', autoplay=initially_playing)
                pipeline.set_state.return_value = Gst.StateChangeReturn.FAILURE
                player.toggle()
                self.assertEqual(errors, [True])
                self.assertFalse(player.has_source)
                self.assertFalse(player.playing)
                self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.NULL,))

    def test_user_seek_cancels_the_pending_resume_seek(self):
        player, pipeline, message, _errors = self.make_player()
        pipeline.query_duration.return_value = (True, 5000 * Gst.MSECOND)
        pipeline.query_position.return_value = (True, 0)
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3500)
        self.assertEqual(player.position(), (3500, 5000))
        self.assertEqual(player.seek(0.5), 2500)
        self.assertEqual(player.position(), (2500, 5000))
        pipeline.seek_simple.assert_not_called()
        player._on_message(None, message)
        pipeline.seek_simple.assert_called_once_with(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, 2500 * Gst.MSECOND)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PAUSED,))
        player._on_message(None, message)
        pipeline.seek_simple.assert_called_once()
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PLAYING,))

    def test_seek_during_an_inflight_seek_waits_for_its_own_completion(self):
        player, pipeline, message, _errors = self.make_player()
        pipeline.query_duration.return_value = (True, 8000 * Gst.MSECOND)
        pipeline.query_position.return_value = (True, 0)
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3000)
        player._on_message(None, message)  # Start the recovery seek.
        self.assertEqual(player.seek(0.75), 6000)
        self.assertEqual(player.position(), (6000, 8000))
        self.assertEqual(pipeline.seek_simple.call_count, 1)
        player._on_message(None, message)  # Finish recovery; begin the user seek.
        self.assertEqual(pipeline.seek_simple.call_count, 2)
        self.assertEqual(pipeline.seek_simple.call_args.args[-1], 6000 * Gst.MSECOND)
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PAUSED,))
        player._on_message(None, message)  # Only the new seek may resume playback.
        self.assertEqual(pipeline.set_state.call_args.args, (Gst.State.PLAYING,))

    def test_seek_returns_none_when_duration_is_not_known(self):
        player, pipeline, message, _errors = self.make_player()
        pipeline.query_duration.return_value = (False, 0)
        player.play_url('https://s50vla.storage.yandex.net/audio', start_ms=3000)
        self.assertIsNone(player.seek(0.5))
        player._on_message(None, message)
        self.assertEqual(pipeline.seek_simple.call_args.args[-1], 3000 * Gst.MSECOND)

    def test_real_playbin_user_seek_resumes_at_the_requested_position(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('wavparse', 'fdsrc', 'fakesink')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(array.array('h', [1500] * (48000 * 5)).tobytes())
        loop, errors = GLib.MainLoop(), []
        player = AudioPlayer(lambda: loop.quit(), lambda: (errors.append(True), loop.quit()), lambda _playing: None)
        sink = Gst.ElementFactory.make('fakesink')
        sink.set_property('sync', True)
        sink.set_property('signal-handoffs', True)
        player._player.set_property('audio-sink', sink)
        result = {'target': None, 'pts': None}
        def handoff(_sink, buffer, _pad):
            if result['target'] is not None and result['pts'] is None:
                result['pts'] = buffer.pts // Gst.MSECOND
                GLib.idle_add(loop.quit)
        sink.connect('handoff', handoff)
        def poll():
            if result['target'] is None and player.position()[0] >= 400:
                result['target'] = player.seek(0.5)
            return True
        poll_id = GLib.timeout_add(100, poll)
        deadline = GLib.timeout_add(5000, lambda: (loop.quit(), False)[1])
        try:
            player.play_buffer(wav_bytes.getvalue())
            loop.run()
            self.assertEqual(errors, [])
            self.assertEqual(result['target'], 2500)
            self.assertIsNotNone(result['pts'])
            self.assertGreaterEqual(result['pts'], 2400)
        finally:
            GLib.source_remove(poll_id)
            if result['pts'] is not None:
                GLib.source_remove(deadline)
            player.close()

    def test_stalled_http_stream_resumes_at_last_position_without_playing_its_start(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('wavparse', 'souphttpsrc', 'fakesink')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(array.array('h', [1500] * (48000 * 6)).tobytes())
        data = wav_bytes.getvalue()
        release = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.headers.get('Range'))
                start = 0
                header = self.headers.get('Range')
                if header and header.startswith('bytes='):
                    start = int(header[6:].split('-')[0])
                self.send_response(206 if start else 200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(len(data) - start))
                if start:
                    self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
                self.end_headers()
                try:
                    if len(requests) == 1:
                        self.wfile.write(data[:96044])
                        self.wfile.flush()
                        release.wait(10)
                    else:
                        self.wfile.write(data[start:])
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_args):
                pass

        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except OSError:
            self.skipTest('Local TCP server is not available in this environment')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        loop, errors = GLib.MainLoop(), []
        player = AudioPlayer(lambda: loop.quit(), lambda: (errors.append(True), loop.quit()), lambda _playing: None)
        sink = Gst.ElementFactory.make('fakesink')
        sink.set_property('sync', True)
        sink.set_property('signal-handoffs', True)
        player._player.set_property('audio-sink', sink)
        observation = {'last': 0, 'changed_at': time.monotonic(), 'resume': None, 'first_pts': None}
        def handoff(_sink, buffer, _pad):
            if observation['resume'] is not None and observation['first_pts'] is None:
                observation['first_pts'] = buffer.pts // Gst.MSECOND
                GLib.idle_add(loop.quit)
        sink.connect('handoff', handoff)
        uri = f'http://127.0.0.1:{server.server_port}/audio.wav'
        def observe():
            position, _duration = player.position()
            if position > observation['last']:
                observation['last'] = position
                observation['changed_at'] = time.monotonic()
            elif (position >= 400 and observation['resume'] is None
                  and time.monotonic() - observation['changed_at'] >= 0.5):
                observation['resume'] = position
                player._start_source(uri, True, network=True, start_ms=position)
            return True
        poll = GLib.timeout_add(100, observe)
        deadline = GLib.timeout_add(7000, lambda: (loop.quit(), False)[1])
        try:
            player._start_source(uri, True, network=True)
            loop.run()
            self.assertEqual(errors, [])
            self.assertIsNotNone(observation['resume'], 'The first HTTP response must actually stall')
            self.assertIsNotNone(observation['first_pts'], 'The replacement source must produce audio')
            self.assertGreaterEqual(observation['first_pts'], observation['resume'] - 100,
                                    'Recovery must not play the beginning of the track')
            self.assertGreaterEqual(len(requests), 2)
        finally:
            release.set()
            GLib.source_remove(poll)
            if observation['first_pts'] is not None:
                GLib.source_remove(deadline)
            player.close()
            server.shutdown()
            server.server_close()


class PrefetchedPlaybackTests(unittest.TestCase):
    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_network_read_ahead_has_a_bounded_memory_queue_without_download_mode(self):
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None)
        try:
            self.assertEqual(player._player.get_property('buffer-size'), 64 * 1024 * 1024)
            self.assertEqual(player._player.get_property('buffer-duration'), 0)
            self.assertFalse(int(player._player.get_property('flags')) & 0x80,
                             'download mode creates a disk cache')
        finally:
            player.close()

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_complete_network_source_can_read_a_long_mp3_ahead_without_disk_cache(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('lamemp3enc', 'appsink', 'souphttpsrc', 'fakesink')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')
        encoder = Gst.parse_launch(
            'audiotestsrc samplesperbuffer=4800 num-buffers=1200 ! '
            'audio/x-raw,rate=48000,channels=2 ! '
            'lamemp3enc target=bitrate bitrate=192 cbr=true ! appsink name=sink sync=false')
        sink = encoder.get_by_name('sink')
        chunks = []
        encoder.set_state(Gst.State.PLAYING)
        try:
            while True:
                sample = sink.emit('try-pull-sample', 3 * Gst.SECOND)
                if sample is None:
                    break
                buffer = sample.get_buffer()
                chunks.append(buffer.extract_dup(0, buffer.get_size()))
        finally:
            encoder.set_state(Gst.State.NULL)
        data = b''.join(chunks)
        self.assertGreater(len(data), 2 * 1024 * 1024)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'audio/mpeg')
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_args):
                pass

        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except OSError:
            self.skipTest('Local TCP server is not available in this environment')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        loop, complete = GLib.MainLoop(), []
        player = AudioPlayer(lambda: None, lambda: loop.quit(), lambda playing: None,
                             on_download_complete=lambda: (complete.append(True), loop.quit()))
        audio_sink = Gst.ElementFactory.make('fakesink')
        audio_sink.set_property('sync', True)
        player._player.set_property('audio-sink', audio_sink)
        timeout = GLib.timeout_add(5000, lambda: (loop.quit(), False)[1])
        try:
            # Internal entry point is used solely to test a local synthetic HTTP source;
            # public play_url continues to reject loopback URLs.
            player._start_source(f'http://127.0.0.1:{server.server_port}/audio', True, network=True)
            loop.run()
            self.assertEqual(complete, [True])
            queues = []
            iterator = player._player.iterate_recurse()
            while True:
                status, element = iterator.next()
                if status != Gst.IteratorResult.OK:
                    break
                factory = element.get_factory()
                if factory is not None and factory.get_name() == 'queue2':
                    queues.append(element)
            self.assertTrue(queues)
            for queue in queues:
                self.assertLessEqual(queue.get_property('max-size-bytes'), 64 * 1024 * 1024)
                self.assertEqual(queue.get_property('max-size-time'), 0)
                self.assertEqual(queue.get_property('max-size-buffers'), 0)
                self.assertIsNone(queue.get_property('temp-template'))
        finally:
            if complete:
                GLib.source_remove(timeout)
            player.close()
            server.shutdown()
            server.server_close()

    def test_buffer_uses_memfd_and_closes_it_on_stop_and_source_change(self):
        gst, pipeline = EqualizerTests.fake_gst(None)
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None, gst=gst)
        player.play_buffer(b'first')
        first_uri = next(call.args[1] for call in pipeline.set_property.call_args_list
                         if call.args[0] == 'uri')
        first_fd = int(first_uri.removeprefix('fd://'))
        self.assertEqual(os.pread(first_fd, 5, 0), b'first')
        player.play_url('https://s50vla.storage.yandex.net/audio')
        with self.assertRaises(OSError):
            os.fstat(first_fd)
        player.play_buffer(b'second', autoplay=False)
        second_uri = [call.args[1] for call in pipeline.set_property.call_args_list
                      if call.args[0] == 'uri'][-1]
        second_fd = int(second_uri.removeprefix('fd://'))
        self.assertFalse(player.playing)
        player.stop()
        with self.assertRaises(OSError):
            os.fstat(second_fd)
        player.close()

    def test_buffer_validation_rejects_empty_oversized_or_wrong_type(self):
        gst, _ = EqualizerTests.fake_gst(None)
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None, gst=gst)
        with patch('impuls_player.player._MAX_BUFFER_BYTES', 3):
            for value in (b'', b'abcd', 'audio', None):
                with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                    player.play_buffer(value)
        player.close()

    def test_failed_pipeline_start_does_not_leak_memfd(self):
        gst, pipeline = EqualizerTests.fake_gst(None)
        pipeline.set_state.return_value = gst.StateChangeReturn.FAILURE
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None, gst=gst)
        real_create = os.memfd_create
        descriptors = []
        def create(*args, **kwargs):
            fd = real_create(*args, **kwargs)
            descriptors.append(fd)
            return fd
        with patch('impuls_player.player.os.memfd_create', side_effect=create):
            with self.assertRaises(RuntimeError):
                player.play_buffer(b'audio')
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        player.close()

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_real_playbin_decodes_wav_bytes_from_memory(self):
        if any(Gst.ElementFactory.find(name) is None for name in ('appsink', 'wavparse', 'fdsrc')):
            self.skipTest('GStreamer plugins needed for this integration test are unavailable')
        blob = io.BytesIO()
        with wave.open(blob, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(array.array('h', [1500] * 4800).tobytes())
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None)
        sink = Gst.ElementFactory.make('appsink', None)
        sink.set_property('sync', False)
        player._player.set_property('audio-sink', sink)
        try:
            player.play_buffer(blob.getvalue())
            sample = sink.emit('try-pull-sample', 3 * Gst.SECOND)
            self.assertIsNotNone(sample, 'playbin must decode audio from an in-memory descriptor')
            self.assertGreater(sample.get_buffer().get_size(), 0)
        finally:
            player.close()

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_source_eos_callback_is_one_shot_and_invalidated_by_seek_or_stop(self):
        gst, pipeline = EqualizerTests.fake_gst(None)
        # Use real event enums and pad probe values while retaining a fake pipeline.
        gst.PadProbeType = Gst.PadProbeType
        gst.PadProbeReturn = Gst.PadProbeReturn
        gst.EventType = Gst.EventType
        callbacks, scheduled = [], []
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None,
                             on_download_complete=lambda: callbacks.append('complete'), gst=gst)
        source, pad = MagicMock(), MagicMock()
        source.get_static_pad.return_value = pad
        source.find_property.return_value = True
        def idle_add(callback, *args):
            scheduled.append((callback, args))
            return 1
        info = MagicMock()
        info.get_event.return_value = Gst.Event.new_eos()
        with patch.object(GLib, 'idle_add', side_effect=idle_add):
            player.play_url('https://s50vla.storage.yandex.net/audio')
            player._setup_download_monitor(None, source)
            probe = pad.add_probe.call_args.args[1]
            probe(pad, info)
            probe(pad, info)
            self.assertEqual(len(scheduled), 1)
            callback, args = scheduled.pop()
            callback(*args)
            self.assertEqual(callbacks, ['complete'])

            player.play_url('https://s50vla.storage.yandex.net/audio')
            player._setup_download_monitor(None, source)
            probe = pad.add_probe.call_args.args[1]
            info.get_event.return_value = Gst.Event.new_seek(
                1.0, Gst.Format.BYTES, Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, 4, Gst.SeekType.NONE, -1)
            probe(pad, info)  # A demuxer may seek even when the user did not.
            info.get_event.return_value = Gst.Event.new_eos()
            probe(pad, info)
            self.assertEqual(scheduled, [])

            player.play_url('https://s50vla.storage.yandex.net/audio')
            player._setup_download_monitor(None, source)
            probe = pad.add_probe.call_args.args[1]
            probe(pad, info)
            pipeline.query_duration.return_value = (True, 100)
            player.seek(0.5)
            callback, args = scheduled.pop()
            callback(*args)
            self.assertEqual(callbacks, ['complete'])

            player.play_url('https://s50vla.storage.yandex.net/audio')
            player._setup_download_monitor(None, source)
            probe = pad.add_probe.call_args.args[1]
            probe(pad, info)
            player.stop()
            callback, args = scheduled.pop()
            callback(*args)
            self.assertEqual(callbacks, ['complete'])
        player.close()

    @unittest.skipIf(Gst is None, 'GStreamer introspection unavailable')
    def test_playing_complete_buffer_schedules_completion_on_main_context(self):
        gst, _ = EqualizerTests.fake_gst(None)
        callbacks, scheduled = [], []
        player = AudioPlayer(lambda: None, lambda: None, lambda playing: None,
                             on_download_complete=lambda: callbacks.append('complete'), gst=gst)
        with patch.object(GLib, 'idle_add', side_effect=lambda callback, *args: scheduled.append((callback, args))):
            player.play_buffer(b'complete file')
            self.assertEqual(callbacks, [])
            self.assertEqual(len(scheduled), 1)
            callback, args = scheduled.pop()
            callback(*args)
            self.assertEqual(callbacks, ['complete'])
        player.close()


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
