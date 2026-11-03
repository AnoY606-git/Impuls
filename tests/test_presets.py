import unittest

from impuls_player.presets import EQUALIZER_PRESETS


class PresetTests(unittest.TestCase):
    def test_presets_have_expected_names_and_ten_safe_gains(self):
        self.assertEqual(list(EQUALIZER_PRESETS), [
            'Ровный', 'Рок', 'Поп', 'Электроника', 'Джаз', 'Больше баса', 'Мягкий звук',
        ])
        for name, gains in EQUALIZER_PRESETS.items():
            with self.subTest(preset=name):
                self.assertEqual(len(gains), 10)
                self.assertTrue(all(isinstance(gain, (int, float)) and -12 <= gain <= 12 for gain in gains))
        self.assertEqual(tuple(EQUALIZER_PRESETS['Ровный']), (0,) * 10)


if __name__ == '__main__':
    unittest.main()
