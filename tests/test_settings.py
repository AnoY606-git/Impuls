import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from impuls_player.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'impuls' / 'settings.json'
        self.settings = Settings(self.path)

    def test_missing_and_corrupt_files_use_defaults(self):
        defaults = self.settings.load()
        self.assertEqual(defaults['volume'], .75)
        self.assertEqual(defaults['quality'], 'hq')
        self.assertEqual(defaults['equalizer'], (0.0,) * 10)
        self.path.parent.mkdir()
        for text in ('{broken', '[]', 'null'):
            self.path.write_text(text)
            self.assertEqual(self.settings.load(), defaults)

    def test_roundtrip_only_known_nonsecret_fields(self):
        values = {'volume': .3, 'quality': 'lossless',
                  'equalizer': tuple(range(10)), 'visualization': 'orbit',
                  'token': 'must-never-save', 'account': 'private'}
        self.assertTrue(self.settings.save(values))
        loaded = self.settings.load()
        self.assertEqual(loaded['volume'], .3)
        self.assertEqual(loaded['quality'], 'lossless')
        self.assertEqual(loaded['equalizer'], tuple(range(10)))
        self.assertEqual(loaded['visualization'], 'orbit')
        raw = self.path.read_text()
        self.assertNotIn('must-never-save', raw)
        self.assertNotIn('private', raw)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_extended_preferences_and_legacy_defaults(self):
        self.settings.save({'volume': .2})
        defaults = self.settings.load()
        self.assertFalse(defaults['shuffle'])
        self.assertFalse(defaults['repeat'])
        self.assertFalse(defaults['equalizer_expanded'])
        self.assertEqual(defaults['window_size'], (960, 800))
        self.assertFalse(defaults['window_maximized'])
        values = {'shuffle': True, 'repeat': True, 'equalizer_expanded': True,
                  'window_size': [1000, 700], 'window_maximized': True}
        self.assertTrue(self.settings.save(values))
        saved = self.settings.load()
        for name in ('shuffle', 'repeat', 'equalizer_expanded', 'window_maximized'):
            self.assertTrue(saved[name])
        self.assertEqual(saved['window_size'], (1000, 700))
        for size in ([1, 2], [999999, 700], [True, 500], '960,800', None):
            self.settings.save({'window_size': size, 'shuffle': 'false', 'repeat': 1})
            saved = self.settings.load()
            self.assertEqual(saved['window_size'], (960, 800))
            self.assertFalse(saved['shuffle'])
            self.assertFalse(saved['repeat'])

    def test_each_invalid_field_falls_back_independently(self):
        self.path.parent.mkdir()
        for volume in (-1, 2, True, '0.5', float('nan'), float('inf')):
            self.path.write_text(json.dumps({'volume': volume, 'quality': 'nq',
                                              'equalizer': [float('nan')] * 10,
                                              'visualization': 'invalid'}))
            loaded = self.settings.load()
            self.assertEqual(loaded['volume'], .75)
            self.assertEqual(loaded['quality'], 'nq')
            self.assertEqual(loaded['equalizer'], (0.0,) * 10)
            self.assertEqual(loaded['visualization'], 'ribbons')

    def test_remaining_time_preference_defaults_and_roundtrips(self):
        self.assertIs(self.settings.load().get('show_remaining'), False)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertTrue(self.settings.save({'show_remaining': enabled}))
                self.assertIs(Settings(self.path).load().get('show_remaining'), enabled)

    def test_remaining_time_preference_rejects_non_boolean_values(self):
        self.path.parent.mkdir()
        for invalid in (0, 1, 'true', 'false', None, [], {}):
            with self.subTest(invalid=invalid):
                values = {'show_remaining': invalid, 'quality': 'lossless'}
                self.path.write_text(json.dumps(values))
                loaded = self.settings.load()
                self.assertIs(loaded.get('show_remaining'), False)
                self.assertEqual(loaded['quality'], 'lossless')
                self.assertTrue(self.settings.save(values))
                self.assertIs(json.loads(self.path.read_text()).get('show_remaining'), False)

    def test_failed_atomic_write_preserves_existing_settings(self):
        self.settings.save({'volume': .4})
        before = self.path.read_bytes()
        with patch('impuls_player.settings.os.replace', side_effect=OSError):
            self.assertFalse(self.settings.save({'volume': .9}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_arbitrarily_large_json_integers_do_not_break_startup(self):
        self.path.parent.mkdir()
        values = {'volume': 10 ** 400, 'quality': 'lossless',
                  'equalizer': [10 ** 400] + [0] * 9}
        self.path.write_text(json.dumps(values))
        loaded = self.settings.load()
        self.assertEqual(loaded['volume'], .75)
        self.assertEqual(loaded['equalizer'], (0.0,) * 10)
        self.assertEqual(loaded['quality'], 'lossless')
        self.assertTrue(self.settings.save(values))

    def test_default_path_respects_xdg_config_home(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': self.temp.name}):
            self.assertEqual(Settings().path, Path(self.temp.name) / 'impuls' / 'settings.json')


if __name__ == '__main__':
    unittest.main()
