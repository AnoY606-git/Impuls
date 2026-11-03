"""GStreamer audio playback. Network resolution is handled elsewhere."""

import math
from numbers import Real
import warnings

from .service import _music_url


_SPECTRUM_BANDS = 32
_SPECTRUM_INPUT_BANDS = 512
_SPECTRUM_FLOOR_DB = -80
_SPECTRUM_ZERO = (0.0,) * _SPECTRUM_BANDS


def _spectrum_edges():
    """Map high-resolution FFT bins to perceptually useful logarithmic bars."""
    edges = [0]
    for index in range(1, _SPECTRUM_BANDS):
        edge = round((_SPECTRUM_INPUT_BANDS - 1) ** (index / _SPECTRUM_BANDS))
        edges.append(max(edges[-1] + 1, edge))
    edges.append(_SPECTRUM_INPUT_BANDS)
    return tuple(edges)


_SPECTRUM_EDGES = _spectrum_edges()


def safe_stream_url(url):
    value = _music_url(url)
    if not value:
        raise ValueError('Invalid HTTPS audio URL')
    return value


def _normalize_equalizer(index, gain):
    """Validate a band and clamp its finite gain to the visible slider range."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 10:
        raise ValueError('Invalid equalizer band')
    if isinstance(gain, bool) or not isinstance(gain, Real):
        raise ValueError('Invalid equalizer gain')
    try:
        value = float(gain)
    except (ValueError, OverflowError):
        raise ValueError('Invalid equalizer gain') from None
    if not math.isfinite(value):
        raise ValueError('Invalid equalizer gain')
    return index, min(12.0, max(-12.0, value))


class AudioPlayer:
    def __init__(self, on_end, on_error, on_state, on_spectrum=None, *, gst=None):
        if gst is None:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            gst = Gst
        gst.init(None)
        self._gst = gst
        self._player = gst.ElementFactory.make('playbin', 'impuls-player')
        if not self._player:
            raise RuntimeError('GStreamer playbin is unavailable')
        self._on_spectrum = on_spectrum
        # Both filters persist across source changes. Either one can be absent
        # without preventing playback or disabling the other one.
        self._equalizer = gst.ElementFactory.make('equalizer-10bands', 'impuls-equalizer')
        self._spectrum = gst.ElementFactory.make('spectrum', 'impuls-spectrum')
        if self._spectrum is not None:
            self._spectrum.set_property('bands', _SPECTRUM_INPUT_BANDS)
            self._spectrum.set_property('interval', 50_000_000)  # 50 ms in nanoseconds
            self._spectrum.set_property('threshold', _SPECTRUM_FLOOR_DB)
            self._spectrum.set_property('post-messages', True)
            self._spectrum.set_property('message-magnitude', True)
            self._spectrum.set_property('message-phase', False)
        self._filter = self._make_audio_filter()
        if self._filter is not None:
            self._player.set_property('audio-filter', self._filter)
        self._player.connect('source-setup', self._configure_source)
        self._bus = self._player.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect('message', self._on_message)
        self._on_end = on_end
        self._on_error = on_error
        self._on_state = on_state
        self._has_source = False
        self._playing = False
        self.set_volume(0.75)

    def _make_audio_filter(self):
        equalizer, spectrum = self._equalizer, self._spectrum
        if equalizer is None:
            return spectrum
        if spectrum is None:
            return equalizer
        audio_filter = self._gst.Bin.new('impuls-audio-filter')
        if audio_filter is None:
            self._spectrum = None
            return equalizer
        try:
            if not audio_filter.add(equalizer) or not audio_filter.add(spectrum):
                raise RuntimeError('Unable to add audio filters')
            if not equalizer.link(spectrum):
                raise RuntimeError('Unable to link audio filters')
            for name, element in (('sink', equalizer), ('src', spectrum)):
                pad = element.get_static_pad(name)
                ghost = self._gst.GhostPad.new(name, pad) if pad is not None else None
                if ghost is None or not audio_filter.add_pad(ghost):
                    raise RuntimeError('Unable to expose audio filter pads')
        except Exception:
            # Preserve the working equalizer if the optional spectrum cannot be
            # linked. Removing the elements detaches them from the abandoned bin.
            try:
                equalizer.unlink(spectrum)
            except Exception:
                pass
            try:
                audio_filter.remove(spectrum)
            except Exception:
                pass
            self._spectrum = None
            try:
                detached = audio_filter.remove(equalizer)
            except Exception:
                detached = False
            if not detached and equalizer.get_parent() is not None:
                self._equalizer = None
                return None
            return equalizer
        return audio_filter

    @staticmethod
    def _configure_source(player, source):
        # A checked URL must not redirect GStreamer to an unchecked host.
        if source.find_property('automatic-redirect'):
            source.set_property('automatic-redirect', False)
        if source.find_property('timeout'):
            source.set_property('timeout', 15)
        if source.find_property('ssl-strict'):
            source.set_property('ssl-strict', True)

    def _on_message(self, bus, message):
        gst = self._gst
        if message.type == gst.MessageType.EOS and self._has_source:
            self._playing = False
            self._clear_spectrum()
            self._on_state(False)
            self._on_end()
        elif message.type == gst.MessageType.ERROR and self._has_source:
            # Do not expose the GStreamer diagnostic: it may contain a signed URL.
            self.stop()
            self._on_error()
        elif message.type == gst.MessageType.STATE_CHANGED and message.src == self._player:
            _, state, _ = message.parse_state_changed()
            self._on_state(state == gst.State.PLAYING)
        elif (message.type == gst.MessageType.ELEMENT and self._on_spectrum is not None
              and self._has_source and self._playing and message.src == self._spectrum):
            self._receive_spectrum(message)

    def _clear_spectrum(self):
        if self._on_spectrum is not None:
            self._on_spectrum(_SPECTRUM_ZERO)

    def _receive_spectrum(self, message):
        structure = message.get_structure()
        if structure is None or structure.get_name() != 'spectrum':
            return
        try:
            success, values = structure.get_list('magnitude')
            if not success or values is None or values.n_values != _SPECTRUM_INPUT_BANDS:
                return
            # GstValueList is not directly convertible by PyGObject. The
            # introspected get_list API returns a GObject.ValueArray instead.
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                magnitudes = tuple(float(values.get_nth(index)) for index in range(_SPECTRUM_INPUT_BANDS))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return
        sanitized = tuple(value if math.isfinite(value) else float(_SPECTRUM_FLOOR_DB) for value in magnitudes)
        levels = tuple(max(0.0, min(1.0, (max(sanitized[start:end]) - _SPECTRUM_FLOOR_DB)
                                           / -_SPECTRUM_FLOOR_DB))
                       for start, end in zip(_SPECTRUM_EDGES, _SPECTRUM_EDGES[1:]))
        self._on_spectrum(levels)

    def play_url(self, url, autoplay=True):
        url = safe_stream_url(url)
        self._player.set_state(self._gst.State.NULL)
        self._clear_spectrum()
        self._player.set_property('uri', url)
        self._has_source = True
        self._playing = bool(autoplay)
        state = self._gst.State.PLAYING if self._playing else self._gst.State.PAUSED
        if self._player.set_state(state) == self._gst.StateChangeReturn.FAILURE:
            self._has_source = False
            self._playing = False
            raise RuntimeError('Unable to start audio')

    def toggle(self):
        if not self._has_source:
            return
        self._playing = not self._playing
        self._player.set_state(self._gst.State.PLAYING if self._playing else self._gst.State.PAUSED)
        if not self._playing:
            self._clear_spectrum()

    def stop(self):
        self._has_source = False
        self._playing = False
        self._player.set_state(self._gst.State.NULL)
        self._clear_spectrum()
        self._on_state(False)

    def seek(self, fraction):
        if not self._has_source:
            return
        success, duration = self._player.query_duration(self._gst.Format.TIME)
        if success and duration > 0:
            position = int(max(0.0, min(1.0, float(fraction))) * duration)
            self._player.seek_simple(self._gst.Format.TIME,
                                     self._gst.SeekFlags.FLUSH | self._gst.SeekFlags.KEY_UNIT,
                                     position)

    def set_volume(self, fraction):
        self._player.set_property('volume', max(0.0, min(1.0, float(fraction))))

    @property
    def equalizer_available(self):
        return self._equalizer is not None

    @property
    def spectrum_available(self):
        return self._spectrum is not None

    def set_equalizer(self, index, gain):
        index, gain = _normalize_equalizer(index, gain)
        if self._equalizer is None:
            raise RuntimeError('GStreamer equalizer-10bands is unavailable')
        self._equalizer.set_property(f'band{index}', gain)

    def position(self):
        if not self._has_source:
            return 0, 0
        found_position, position = self._player.query_position(self._gst.Format.TIME)
        found_duration, duration = self._player.query_duration(self._gst.Format.TIME)
        return (max(0, position // self._gst.MSECOND) if found_position else 0,
                max(0, duration // self._gst.MSECOND) if found_duration else 0)

    @property
    def playing(self):
        return self._playing

    @property
    def has_source(self):
        return self._has_source

    def close(self):
        self.stop()
        self._bus.remove_signal_watch()
