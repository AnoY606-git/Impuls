"""MPRIS integration for local desktop media controls."""

from __future__ import annotations

import hashlib
import math

from gi.repository import Gio, GLib


NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
_TRACK_PREFIX = "/io/github/impuls/Impuls/Track/"
_MAX_DURATION_MS = (2**63 - 1) // 1000
_BUS_NAME = "org.mpris.MediaPlayer2.impuls"
_OBJECT_PATH = "/org/mpris/MediaPlayer2"
_ROOT = "org.mpris.MediaPlayer2"
_PLAYER = "org.mpris.MediaPlayer2.Player"
_PROPERTIES = "org.freedesktop.DBus.Properties"

_INTROSPECTION_XML = """<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/>
    <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg direction="in" type="x" name="Offset"/></method>
    <method name="SetPosition"><arg direction="in" type="o" name="TrackId"/>
      <arg direction="in" type="x" name="Position"/></method>
    <method name="OpenUri"><arg direction="in" type="s" name="Uri"/></method>
    <signal name="Seeked"><arg type="x" name="Position"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""


def track_path(track) -> str:
    """Return a valid, stable D-Bus object path for the current track."""
    if track is None:
        return NO_TRACK
    digest = hashlib.sha256(str(track.id).encode("utf-8", "replace")).hexdigest()
    return _TRACK_PREFIX + digest


def _text(value) -> str:
    return str(value or "").replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def metadata_for_track(track) -> dict[str, GLib.Variant]:
    """Build MPRIS metadata without exposing streaming or cover URLs."""
    metadata = {"mpris:trackid": GLib.Variant("o", track_path(track))}
    if track is None:
        return metadata
    metadata["xesam:title"] = GLib.Variant("s", _text(track.title))
    metadata["xesam:artist"] = GLib.Variant("as", [_text(artist) for artist in track.artists])
    metadata["xesam:album"] = GLib.Variant("s", _text(track.album))
    duration = track.duration_ms
    if isinstance(duration, int) and duration > 0:
        metadata["mpris:length"] = GLib.Variant("x", min(duration, _MAX_DURATION_MS) * 1000)
    return metadata


def seek_fraction(position_ms, duration_ms) -> float | None:
    """Convert an absolute position to the controller's bounded fraction."""
    if (not isinstance(duration_ms, (int, float)) or not math.isfinite(duration_ms)
            or duration_ms <= 0 or not isinstance(position_ms, (int, float))
            or not math.isfinite(position_ms)):
        return None
    return max(0.0, min(1.0, position_ms / duration_ms))


class Mpris:
    """Export player controls on the session bus; all calls run on the GTK thread.

    A missing session bus is allowed: the player continues to work normally.
    """

    def __init__(self, controller, on_raise=None, on_quit=None):
        self._controller = controller
        self._on_raise = on_raise
        self._on_quit = on_quit
        self._track = None
        self._status = "Stopped"
        self._position_ms = 0
        self._duration_ms = 0
        self._volume = 1.0
        self._shuffle = False
        self._repeat = False
        self._connection = None
        self._registrations = []
        self._owner_id = 0
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            interfaces = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML).interfaces
            for interface in interfaces:
                registration = self._connection.register_object(
                    _OBJECT_PATH, interface, self._on_method_call,
                    self._on_get_property, self._on_set_property,
                )
                if not registration:
                    raise RuntimeError("Could not register MPRIS object")
                self._registrations.append(registration)
            self._owner_id = Gio.bus_own_name_on_connection(
                self._connection, _BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None,
            )
        except (GLib.Error, RuntimeError):
            self.close()

    def close(self):
        """Release the name and exported interfaces. Safe to call repeatedly."""
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._connection is not None:
            for registration in self._registrations:
                self._connection.unregister_object(registration)
        self._registrations.clear()
        self._connection = None

    def set_track(self, track):
        self._track = track
        self._position_ms = 0
        self._duration_ms = _safe_milliseconds(getattr(track, "duration_ms", 0))
        self._status = "Stopped"
        self._notify("Metadata", "PlaybackStatus", "CanGoNext", "CanGoPrevious",
                     "CanPlay", "CanPause", "CanSeek")

    def set_playback(self, playing: bool, position_ms: int = 0, duration_ms: int = 0,
                     stopped: bool = False):
        old_status = self._status
        old_can_seek = self._can_seek()
        duration = _safe_milliseconds(duration_ms)
        if duration:
            self._duration_ms = duration
        self._position_ms = min(_safe_milliseconds(position_ms), self._duration_ms) if self._duration_ms else 0
        if stopped or not self._track:
            self._status = "Stopped"
        elif playing:
            self._status = "Playing"
        else:
            self._status = "Paused"
        changed = []
        if old_status != self._status:
            changed.append("PlaybackStatus")
        if old_can_seek != self._can_seek():
            changed.append("CanSeek")
        if changed:
            self._notify(*changed)

    def set_volume(self, fraction: float):
        """Update the D-Bus property after the application's volume changes."""
        if not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
            return
        value = min(1.0, max(0.0, fraction))
        if value != self._volume:
            self._volume = value
            self._notify("Volume")

    def set_modes(self, shuffle: bool, repeat: bool):
        """Publish queue modes after changes made in the application."""
        changed = []
        if bool(shuffle) != self._shuffle:
            self._shuffle = bool(shuffle)
            changed.append("Shuffle")
        if bool(repeat) != self._repeat:
            self._repeat = bool(repeat)
            changed.append("LoopStatus")
        if changed:
            self._notify(*changed)

    def _can_seek(self):
        return self._track is not None and self._duration_ms > 0

    def _on_get_property(self, connection, sender, path, interface, name):
        if interface == _ROOT:
            values = {
                "CanQuit": ("b", self._on_quit is not None),
                "CanRaise": ("b", self._on_raise is not None),
                "HasTrackList": ("b", False),
                "Identity": ("s", "Impuls"),
                "DesktopEntry": ("s", "io.github.AnoY606_git.Impuls"),
                "SupportedUriSchemes": ("as", []),
                "SupportedMimeTypes": ("as", []),
            }
        elif interface == _PLAYER:
            available = self._track is not None
            values = {
                "PlaybackStatus": ("s", self._status),
                "LoopStatus": ("s", "Playlist" if self._repeat else "None"),
                "Rate": ("d", 1.0),
                "Shuffle": ("b", self._shuffle),
                "Metadata": ("a{sv}", metadata_for_track(self._track)),
                "Volume": ("d", self._volume),
                "Position": ("x", self._position_ms * 1000),
                "MinimumRate": ("d", 1.0),
                "MaximumRate": ("d", 1.0),
                "CanGoNext": ("b", available),
                "CanGoPrevious": ("b", available),
                "CanPlay": ("b", available),
                "CanPause": ("b", available),
                "CanSeek": ("b", self._can_seek()),
                "CanControl": ("b", True),
            }
        else:
            return None
        value = values.get(name)
        return GLib.Variant(*value) if value is not None else None

    def _on_set_property(self, connection, sender, path, interface, name, value):
        if interface != _PLAYER:
            return False
        if name == "Volume" and value.get_type_string() == "d":
            requested = value.unpack()
            if not math.isfinite(requested):
                return False
            normalized = min(1.0, max(0.0, requested))
            try:
                self._controller.set_volume(normalized)
            except Exception:
                return False
            self.set_volume(normalized)
            return True
        if name == "Rate" and value.get_type_string() == "d":
            return value.unpack() == 1.0
        if name == "LoopStatus" and value.get_type_string() == "s":
            requested = value.unpack()
            if requested not in ("None", "Playlist"):
                return False
            try:
                self._controller.set_repeat(requested == "Playlist")
            except Exception:
                return False
            self.set_modes(self._shuffle, requested == "Playlist")
            return True
        if name == "Shuffle" and value.get_type_string() == "b":
            requested = value.unpack()
            try:
                self._controller.set_shuffle(requested)
            except Exception:
                return False
            self.set_modes(requested, self._repeat)
            return True
        return False

    def _on_method_call(self, connection, sender, path, interface, method, parameters, invocation):
        try:
            if interface == _ROOT and method in ("Raise", "Quit"):
                callback = self._on_raise if method == "Raise" else self._on_quit
                if callback is not None:
                    callback()
            elif interface == _PLAYER:
                if method == "Next" and self._track:
                    self._controller.next_track()
                elif method == "Previous" and self._track:
                    self._controller.previous_track()
                elif method in ("Play", "Pause") and self._track:
                    set_playing = getattr(self._controller, "set_playing", None)
                    if callable(set_playing):
                        set_playing(method == "Play")
                        active = bool(getattr(self._controller.player, "has_source", False)
                                      or getattr(self._controller, "playback_pending", False))
                        status = "Playing" if method == "Play" else "Paused" if active else "Stopped"
                        if status != self._status:
                            self._status = status
                            self._notify("PlaybackStatus")
                    elif method == "Play" and self._status == "Stopped":
                        restart = getattr(self._controller, "restart_current", None)
                        (restart if callable(restart) else self._controller.toggle_playback)()
                    elif ((method == "Play" and self._status != "Playing")
                          or (method == "Pause" and self._status == "Playing")):
                        self._controller.toggle_playback()
                elif method == "PlayPause" and self._track:
                    if self._status == "Stopped" and not hasattr(self._controller, "set_playing"):
                        restart = getattr(self._controller, "restart_current", None)
                        (restart if callable(restart) else self._controller.toggle_playback)()
                    else:
                        self._controller.toggle_playback()
                elif method == "Stop":
                    stop = getattr(self._controller, "stop_playback", None)
                    if callable(stop):
                        stop()
                    else:
                        self._controller.player.stop()
                    self._status = "Stopped"
                    self._position_ms = 0
                    self._notify("PlaybackStatus")
                elif method == "Seek" and self._can_seek():
                    offset_us, = parameters.unpack()
                    target_us = self._position_ms * 1000 + offset_us
                    if target_us > self._duration_ms * 1000:
                        self._controller.next_track()
                    else:
                        self._seek_to(max(0, target_us))
                elif method == "SetPosition" and self._can_seek():
                    requested_path, target_us = parameters.unpack()
                    if (requested_path == track_path(self._track)
                            and 0 <= target_us <= self._duration_ms * 1000):
                        self._seek_to(target_us)
                elif method == "OpenUri":
                    invocation.return_dbus_error("org.freedesktop.DBus.Error.NotSupported", "Not supported")
                    return
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", "Unknown method")
                return
            invocation.return_value(None)
        except Exception:
            # Underlying errors may contain private track URLs or account data.
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "Player operation failed")

    def _seek_to(self, position_us):
        fraction = seek_fraction(position_us, self._duration_ms * 1000)
        if fraction is None:
            return
        accepted = self._controller.seek(fraction)
        if not isinstance(accepted, int) or isinstance(accepted, bool) or accepted < 0:
            return
        self._position_ms = _safe_milliseconds(accepted)
        if self._connection is not None:
            try:
                self._connection.emit_signal(None, _OBJECT_PATH, _PLAYER, "Seeked",
                                             GLib.Variant("(x)", (self._position_ms * 1000,)))
            except GLib.Error:
                pass  # The desktop session can close while the player is running.

    def _notify(self, *names):
        if self._connection is None:
            return
        changes = {name: self._on_get_property(None, None, None, _PLAYER, name) for name in names}
        try:
            self._connection.emit_signal(None, _OBJECT_PATH, _PROPERTIES, "PropertiesChanged",
                                         GLib.Variant("(sa{sv}as)", (_PLAYER, changes, [])))
        except GLib.Error:
            pass  # A lost session bus must not interrupt playback.


def _safe_milliseconds(value):
    if not isinstance(value, int):
        return 0
    return max(0, min(value, _MAX_DURATION_MS))
