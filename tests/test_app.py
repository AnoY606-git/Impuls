"""Smoke test the wired GTK application without a real account or keyring."""
import unittest
import tempfile
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gdk', '4.0')
    from gi.repository import Gdk, Gtk
    Gtk.init_check()
    GTK_AVAILABLE = Gdk.Display.get_default() is not None
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class _Window:
    def __init__(self):
        self.visible = True
        self.tray_available = False

    def set_visible(self, value): self.visible = value
    def get_visible(self): return self.visible
    def present(self): self.visible = True
    def set_tray_available(self, value): self.tray_available = value


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        from impuls_player.app import ImpulsApplication
        self.application = ImpulsApplication()
        self.application.window = _Window()

    def test_close_hides_only_when_tray_host_is_available(self):
        application = self.application
        application.tray = SimpleNamespace(available=False)
        self.assertFalse(application.close_window())
        self.assertTrue(application.window.visible)
        application.tray.available = True
        self.assertTrue(application.close_window())
        self.assertFalse(application.window.visible)
        application.present_window()
        self.assertTrue(application.window.visible)

    def test_host_loss_restores_hidden_window_and_quit_never_hides(self):
        application = self.application
        application.tray = SimpleNamespace(available=True)
        application.hide_window()
        application._tray_available(False)
        self.assertTrue(application.window.visible)
        self.assertFalse(application.window.tray_available)
        with patch.object(application, 'quit'):
            application.request_quit()
        self.assertFalse(application.close_window())

    def test_settings_apply_and_save_actual_controls_without_network(self):
        from impuls_player.settings import Settings
        class Scale:
            def __init__(self, value=0): self.value = value
            def get_value(self): return self.value
        class Selector:
            def __init__(self): self.selected = 1
            def get_selected(self): return self.selected
            def set_selected(self, value): self.selected = value
        application = self.application
        with tempfile.TemporaryDirectory() as folder:
            application.settings = Settings(Path(folder) / 'settings.json')
            application.settings.save({'volume': .3, 'quality': 'lossless',
                                       'equalizer': [2] * 10, 'visualization': 'orbit'})
            window = application.window
            window.volume_scale = Scale()
            window.equalizer_sliders = [Scale() for _ in range(10)]
            window.quality_selector = Selector()
            window.visualization_selector = Selector()
            window._visualization_mode = 'ribbons'
            def set_gains(values):
                for slider, value in zip(window.equalizer_sliders, values): slider.value = value
            window.set_equalizer_values = set_gains
            gains = [0] * 10
            def set_volume(value): window.volume_scale.value = value
            def set_quality(value): application.controller._quality = value
            def set_equalizer(index, value): gains[index] = value
            application.player = SimpleNamespace(equalizer_available=True)
            application.controller = SimpleNamespace(set_volume=set_volume, set_quality=set_quality,
                                                     set_equalizer=set_equalizer)
            application._restore_settings()
            self.assertEqual(window.volume_scale.value, .3)
            self.assertEqual(gains, [2] * 10)
            self.assertEqual(window.quality_selector.selected, 2)
            self.assertEqual(window.visualization_selector.selected, 3)
            window.volume_scale.value = .6
            window.quality_selector.selected = 0
            window.equalizer_sliders[4].value = -3
            window._visualization_mode = 'mirror'
            application._save_settings()
            saved = application.settings.load()
            self.assertEqual(saved['volume'], .6)
            self.assertEqual(saved['quality'], 'nq')
            self.assertEqual(saved['equalizer'][4], -3)
            self.assertEqual(saved['visualization'], 'mirror')

            # An atomic write failure keeps the previous file and is visible;
            # subsequent successful changes must resume persistence.
            before = application.settings.path.read_bytes()
            errors = []
            window.show_error = errors.append
            window.volume_scale.value = .8
            with (patch('impuls_player.settings.os.replace', side_effect=OSError),
                  self.assertLogs('impuls_player.app', level='WARNING')):
                self.assertFalse(application._save_settings())
                self.assertFalse(application._save_settings())
            self.assertEqual(application.settings.path.read_bytes(), before)
            self.assertEqual(len(errors), 1)
            self.assertTrue(application._save_settings())
            self.assertEqual(application.settings.load()['volume'], .8)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display is unavailable')
class ApplicationSmokeTests(unittest.TestCase):
    def test_settings_survive_real_close_reopen_and_tray_hide(self):
        # Each run is a fresh GTK process, with a real main loop and fake
        # credentials; closing a real window exercises shutdown ordering.
        script = textwrap.dedent("""
            import sys
            import traceback
            from unittest.mock import patch
            from gi.repository import Gio, GLib
            from impuls_player.app import ImpulsApplication
            from impuls_player.settings import Settings
            class Store:
                def load(self): return None
                def clear(self): pass
                def save(self, token): pass
            errors = []
            phase, path = sys.argv[1:]
            def check_saved(app):
                values = app.settings.load()
                assert values['volume'] == .31, values
                assert values['quality'] == 'lossless', values
                assert values['equalizer'][0] == 4, values
                assert values['visualization'] == 'orbit', values
                assert values['shuffle'] and values['repeat'], values
                assert values['equalizer_expanded'], values
                assert values['window_size'] == (1000, 720), values
                assert values['show_remaining'], values
            with patch('impuls_player.app.SecretStore', return_value=Store()):
                app = ImpulsApplication()
                app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
                app.settings = Settings(path)
                def verify_open():
                    try:
                        check_saved(app)
                        assert app.window.get_visible()
                        app.window.close()
                    except Exception as exc:
                        errors.append(traceback.format_exc())
                        app.request_quit()
                    return False
                def activated(_):
                    def exercise():
                        try:
                            win = app.window
                            if phase == 'reopen':
                                assert win.volume_scale.get_value() == .31
                                assert win.quality_selector.get_selected() == 2
                                assert win.equalizer_sliders[0].get_value() == 4
                                assert win._visualization_mode == 'orbit'
                                assert app.controller.shuffle_enabled
                                assert app.controller.repeat_enabled
                                assert win.equalizer_toggle.get_active()
                                assert tuple(win.get_default_size()) == (1000, 720), tuple(win.get_default_size())
                                assert win.time_toggle.get_active()
                                assert not win.mini_button.get_active()
                                app.request_quit()
                            else:
                                win.volume_scale.set_value(.31)
                                win.quality_selector.set_selected(2)
                                win.equalizer_sliders[0].set_value(4)
                                win.visualization_selector.set_selected(3)
                                win.shuffle_button.set_active(True)
                                win.repeat_button.set_active(True)
                                win.equalizer_toggle.set_active(True)
                                win.set_default_size(1000, 720)
                                win.time_toggle.set_active(True)
                                win.set_mini_mode(True)
                                if phase == 'autosave':
                                    GLib.timeout_add(900, verify_open)
                                elif phase == 'tray':
                                    with patch.object(app.tray, 'available', True):
                                        win.close()
                                        assert not win.get_visible()
                                        check_saved(app)
                                        app.present_window()
                                        assert win.get_visible()
                                    app.request_quit()
                                else:
                                    win.close()
                                    # Preferences must be durable before GTK shutdown.
                                    check_saved(app)
                        except Exception as exc:
                            errors.append(traceback.format_exc())
                            app.request_quit()
                        return False
                    GLib.timeout_add(150, exercise)
                app.connect('activate', activated)
                GLib.timeout_add_seconds(10, lambda: (errors.append('timeout'), app.request_quit(), False)[2])
                app.run([])
                assert not errors, errors
                check_saved(app)
        """)
        with tempfile.TemporaryDirectory() as folder:
            for phase in ('close', 'autosave', 'tray'):
                path = str(Path(folder) / f'{phase}.json')
                for step in (phase, 'reopen'):
                    with self.subTest(phase=phase, step=step):
                        result = subprocess.run(
                            [sys.executable, '-c', script, step, path],
                            text=True, capture_output=True, timeout=15,
                            env={**os.environ, 'GTK_A11Y': 'none'})
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_application_connects_window_and_actions(self):
        from impuls_player.app import ImpulsApplication
        from impuls_player.settings import Settings
        class Store:
            def load(self): return None
            def clear(self): pass
            def save(self, token): pass
        with (tempfile.TemporaryDirectory() as folder,
              patch('impuls_player.app.SecretStore', return_value=Store()),
              patch('impuls_player.app.Settings', return_value=Settings(Path(folder) / 'settings.json'))):
            application = ImpulsApplication()
            self.assertTrue(application.register())
            application.activate()
            self.assertIsNotNone(application.window)
            self.assertIn('wave', application.window.actions)
            self.assertIn('device_login', application.window.actions)
            self.assertIn('equalizer_preset', application.window.actions)
            self.assertIn('set_shuffle', application.window.actions)
            self.assertIn('set_repeat', application.window.actions)
            self.assertIn('close_window', application.window.actions)
            self.assertIn('quit', application.window.actions)
            with patch.object(application.controller, 'current_download_complete') as received:
                application.player._on_download_complete()
                received.assert_called_once_with()
            if application.player.spectrum_available:
                levels = (0.4,) * 32
                application.player._on_spectrum(levels)
                self.assertEqual(application.window._spectrum_levels, levels)
                application.player.stop()
                self.assertFalse(any(application.window._spectrum_levels))
            application.window.actions['set_shuffle'](True)
            application._refresh()
            if application.mpris is not None:
                value = application.mpris._on_get_property(
                    None, None, None, 'org.mpris.MediaPlayer2.Player', 'Shuffle')
                self.assertTrue(value.unpack())
            # Real GTK close-request must preserve render sources and the
            # same live window when the application handles a tray hide.
            with patch.object(application.tray, 'available', True):
                self.assertTrue(application.window._on_close_request(application.window))
                self.assertFalse(application.window.get_visible())
                self.assertFalse(application.window._destroyed)
                application._tray_available(False)
                self.assertTrue(application.window.get_visible())
                self.assertFalse(application.window._destroyed)
            if application.mpris is not None:
                application.mpris.close()
            if application.tray is not None:
                application.tray.close()
            application.controller.close()
            application.player.close()
            application.window.destroy()
            application.quit()

if __name__ == '__main__': unittest.main()
