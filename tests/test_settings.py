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
