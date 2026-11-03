"""GTK application and composition root."""
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk

from .controller import Controller
from .player import AudioPlayer
from .secrets import SecretStore
from .service import MusicService
from .settings import Settings
from .ui import PlayerWindow


APP_ID = 'io.github.AnoY606_git.Impuls'


class ImpulsApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.controller = None
        self.player = None
        self.mpris = None
        self.tray = None
        self.settings = Settings()
        self._settings_loaded = False
        self._quitting = False
        self._refresh_id = None
        self._mpris_track = None

    def do_activate(self):
        if self.window is not None:
            self.present_window()
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
        actions.update(close_window=self.close_window, hide_window=self.hide_window,
                       quit=self.request_quit)
        self._restore_settings()
        try:
            from .mpris import Mpris
            self.mpris = Mpris(self.controller, on_raise=self.present_window, on_quit=self.request_quit)
        except Exception:
            # A missing session bus must not prevent playback.
            self.mpris = None
        from .tray import Tray
        self.tray = Tray(self.controller, self.present_window, self.request_quit, self._tray_available)
        self.window.set_tray_available(self.tray.available)
        self.present_window()
        self._refresh_id = GLib.timeout_add(500, self._refresh)
        self.controller.restore()

    def present_window(self):
        if self.window is not None and not self._quitting:
            self.window.present()

    def hide_window(self):
        if (not self._quitting and self.window is not None
                and self.tray is not None and self.tray.available):
            self.window.set_visible(False)
            return True
        return False

    def close_window(self):
        """Keep the window alive only when a tray host can restore it."""
        return self.hide_window()

    def request_quit(self):
        self._quitting = True
        self.quit()

    def _tray_available(self, available):
        if self.window is not None:
            self.window.set_tray_available(available)
            if not available and not self.window.get_visible():
                self.present_window()

    def _restore_settings(self):
        values = self.settings.load()
        self.controller.set_volume(values['volume'])
        self.controller.set_quality(values['quality'])
        # Keep selector synchronization passive so restoration does not repeat
        # a controller action or cancel prefetch twice.
        set_quality = getattr(self.window, 'set_quality', None)
        if callable(set_quality):
            set_quality(values['quality'])
        else:
            selector = self.window.quality_selector
            handler = getattr(self.window, '_on_quality_selected', None)
            if handler is not None:
                selector.handler_block_by_func(handler)
            try:
                selector.set_selected(('nq', 'hq', 'lossless').index(values['quality']))
            finally:
                if handler is not None:
                    selector.handler_unblock_by_func(handler)
        gains = values['equalizer']
        if self.player.equalizer_available:
            for index, gain in enumerate(gains):
                self.controller.set_equalizer(index, gain)
        self.window.set_equalizer_values(gains)
        mode = values.get('visualization', 'ribbons')
        selector = getattr(self.window, 'visualization_selector', None)
        if selector is not None:
            selector.set_selected(('ribbons', 'bars', 'mirror', 'orbit').index(mode))
        self._settings_loaded = True

    def _save_settings(self):
        if self.window is None or not self._settings_loaded:
            return
        self.settings.save({
            'volume': self.window.volume_scale.get_value(),
            'quality': ('nq', 'hq', 'lossless')[self.window.quality_selector.get_selected()],
            'equalizer': [slider.get_value() for slider in self.window.equalizer_sliders],
            'visualization': getattr(self.window, '_visualization_mode', 'ribbons'),
        })

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
        if self.tray is not None:
            self.tray.set_playback(track, playing)
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
        self._quitting = True
        self._save_settings()
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        if self.mpris is not None:
            self.mpris.close()
        if self.tray is not None:
            self.tray.close()
        if self.controller is not None:
            self.controller.close()
        if self.player is not None:
            self.player.close()
        Gtk.Application.do_shutdown(self)


def main():
    GLib.set_application_name('Impuls')
    return ImpulsApplication().run(None)
