import unittest

import cairo

from impuls_player.visualizations import VISUALIZATION_MODES, draw_visualization


def render(mode, levels, phase=0.0, size=(320, 180)):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *size)
    context = cairo.Context(surface)
    draw_visualization(context, *size, levels, mode, phase)
    surface.flush()
    return bytes(surface.get_data())


class VisualizationTests(unittest.TestCase):
    def test_four_distinct_reproducible_modes_react_to_audio(self):
        levels = tuple((i % 11 + 1) / 12 for i in range(32))
        active = []
        self.assertEqual(len(VISUALIZATION_MODES), 4)
        for mode, _name in VISUALIZATION_MODES:
            with self.subTest(mode=mode):
                pixels = render(mode, levels)
                self.assertNotEqual(pixels, render(mode, (0.0,) * 32))
                self.assertEqual(pixels, render(mode, levels))
                active.append(pixels)
        self.assertEqual(len(set(active)), 4)

    def test_silent_modes_do_not_animate_fake_signal(self):
        for mode, _name in VISUALIZATION_MODES:
            self.assertEqual(render(mode, (0.0,) * 32, 0),
                             render(mode, (0.0,) * 32, 2))

    def test_ribbons_and_orbit_move_with_phase_while_signal_is_present(self):
        for mode in ('ribbons', 'orbit'):
            self.assertNotEqual(render(mode, (.5,) * 32, 0),
                                render(mode, (.5,) * 32, 1))

    def test_every_mode_handles_tiny_and_wide_canvas(self):
        for mode, _name in VISUALIZATION_MODES:
            for size in ((1, 1), (80, 240), (840, 180)):
                self.assertTrue(render(mode, (.4,) * 32, size=size))

    def test_unknown_mode_falls_back_to_ribbons(self):
        self.assertEqual(render('unknown', (.5,) * 32),
                         render('ribbons', (.5,) * 32))


if __name__ == '__main__':
    unittest.main()
