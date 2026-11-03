"""GTK application and composition root."""
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk

from .controller import Controller
from .player import AudioPlayer
from .secrets import SecretStore
from .service import MusicService
from .ui import PlayerWindow


APP_ID = 'io.github.AnoY606_git.Impuls'


class ImpulsApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.controller = None
        self.player = None
        self.mpris = None
        self._refresh_id = None
        self._mpris_track = None

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return
        actions = {}
        self.window = PlayerWindow(self, actions)
        self.window.set_icon_name(APP_ID)
        self.player = AudioPlayer(self._audio_ended, self._audio_error, self._audio_state,
                                  self._audio_spectrum, on_download_complete=self._audio_download_complete)
        self.window.set_spectrum_available(self.player.spectrum_available)
        self.controller = Controller(self.window, self.player, SecretStore(), MusicService,
                                     dispatch=lambda callback: GLib.idle_add(callback))
        actions.update(self.controller.actions())
        try:
            from .mpris import Mpris
            self.mpris = Mpris(self.controller, on_raise=self.window.present, on_quit=self.quit)
        except Exception:
            # A missing session bus must not prevent playback.
            self.mpris = None
        self.window.present()
        self._refresh_id = GLib.timeout_add(500, self._refresh)
        self.controller.restore()

    def _audio_spectrum(self, levels):
        if self.window is not None:
            self.window.set_spectrum(levels)

    def _audio_ended(self):
        if self.controller is not None:
            self.controller.audio_ended()

    def _audio_download_complete(self):
        if self.controller is not None:
            self.controller.current_download_complete()

    def _audio_error(self):
        if self.controller is not None and self.controller.audio_error():
            return
        if self.window is not None:
            self.window.show_error('Не удалось воспроизвести аудио. Проверьте подключение и аудиокодеки.')

    def _audio_state(self, playing):
        if self.controller is not None:
            self.controller.playback_state(playing)
            self._refresh()

    def _refresh(self):
        if self.window is None or self.player is None or self.controller is None:
            return False
        position, duration = self.player.position()
        track = self.controller.current_track
        if not duration and track is not None:
            duration = track.duration_ms
        self.controller.monitor_playback(position, duration)
        playing = self.controller.display_playing
        self.window.set_playback(playing, position, duration)
        if self.mpris is not None:
            if track != self._mpris_track:
                self.mpris.set_track(track)
                self._mpris_track = track
            stopped = not self.player.has_source and not self.controller.playback_pending
            self.mpris.set_playback(playing, position, duration, stopped=stopped)
            self.mpris.set_volume(self.window.volume_scale.get_value())
            self.mpris.set_modes(self.controller.shuffle_enabled, self.controller.repeat_enabled)
        return True

    def do_shutdown(self):
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        if self.mpris is not None:
            self.mpris.close()
        if self.controller is not None:
            self.controller.close()
        if self.player is not None:
            self.player.close()
        Gtk.Application.do_shutdown(self)


def main():
    GLib.set_application_name('Impuls')
    return ImpulsApplication().run(None)
