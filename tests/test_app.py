"""Smoke test the wired GTK application without a real account or keyring."""
import unittest
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


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display is unavailable')
class ApplicationSmokeTests(unittest.TestCase):
    def test_application_connects_window_and_actions(self):
        from impuls_player.app import ImpulsApplication
        class Store:
            def load(self): return None
            def clear(self): pass
            def save(self, token): pass
        with patch('impuls_player.app.SecretStore', return_value=Store()):
            application = ImpulsApplication()
            self.assertTrue(application.register())
            application.activate()
            self.assertIsNotNone(application.window)
            self.assertIn('wave', application.window.actions)
            self.assertIn('device_login', application.window.actions)
            self.assertIn('equalizer_preset', application.window.actions)
            self.assertIn('set_shuffle', application.window.actions)
            self.assertIn('set_repeat', application.window.actions)
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
            if application.mpris is not None:
                application.mpris.close()
            application.controller.close()
            application.player.close()
            application.window.destroy()
            application.quit()

if __name__ == '__main__': unittest.main()
