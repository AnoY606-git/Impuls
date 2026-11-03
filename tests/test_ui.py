import unittest
import time
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk, Pango
Gtk.init_check()
HAS_DISPLAY = Gdk.Display.get_default() is not None

try:
    from impuls_player.ui import PlayerWindow
except ModuleNotFoundError as exc:
    if exc.name not in ('impuls_player', 'impuls_player.ui'):
        raise
    PlayerWindow = None


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


def button(window, label):
    for widget in walk(window):
        if isinstance(widget, Gtk.Button) and widget.get_label() == label:
            return widget
    raise AssertionError(f'No button with label {label!r}')


def drain_until(predicate, timeout=2):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            time.sleep(0.001)
    if not predicate():
        raise AssertionError('Timed out waiting for GTK idle work')


def supports_maximize():
    display = Gdk.Display.get_default()
    screen = getattr(display, 'get_screen', lambda: None)()
    return screen is not None and screen.supports_net_wm_hint('_NET_WM_STATE_MAXIMIZED_VERT')


def near_bottom_adjustment(window, allocate=True):
    # Keep the GTK adjustment deterministic instead of depending on a window
    # manager delivering a frame and recalculating its page size mid-test.
    adjustment = Gtk.Adjustment(value=700, lower=0, upper=1000, step_increment=1,
                                page_increment=100, page_size=200)
    adjustment.connect('value-changed', window._on_track_scroll)
    adjustment.connect('changed', window._on_track_scroll)
    class MappedScroll:
        def get_mapped(self): return True
        def get_vadjustment(self): return adjustment
    window.track_scroll = MappedScroll()
    last = window.tracks_list.get_row_at_index(window._rendered_track_count - 1)
    if allocate and last is not None:
        last.allocate(400, 56, -1, None)
    return adjustment


@unittest.skipUnless(HAS_DISPLAY, 'GTK display is unavailable')
class PlayerWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id='io.github.impuls.UiTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register()

    def setUp(self):
        self.assertIsNotNone(PlayerWindow, 'Player window module must be implemented')
        self.calls = []
        names = ('login', 'logout', 'search', 'liked_tracks', 'playlists', 'open_playlist', 'play_track', 'toggle_playback', 'next_track', 'previous_track', 'seek', 'set_volume', 'set_liked', 'device_login', 'wave', 'set_equalizer', 'equalizer_preset', 'set_shuffle', 'set_repeat', 'load_more', 'set_quality', 'refresh_view', 'play_all', 'hide_window', 'quit')
        self.actions = {name: (lambda *args, name=name: self.calls.append((name, args))) for name in names}
        self.window = PlayerWindow(self.app, self.actions)

    def tearDown(self):
        if hasattr(self, 'window'):
            self.window.destroy()

    def test_dropdown_popups_have_readable_spaced_rows(self):
        self.window.set_equalizer_available(True)
        self.window.present()
        drain_until(lambda: self.window.get_width() > 0)
        for selector in (self.window.quality_selector, self.window.preset_selector,
                         self.window.visualization_selector):
            with self.subTest(selector=selector):
                if selector is self.window.visualization_selector:
                    self.window.view_stack.set_visible_child_name('now')
                selector.activate()
                popover = next(w for w in walk(selector) if isinstance(w, Gtk.Popover))
                drain_until(lambda: popover.get_mapped() and popover.get_height() > 0)
                rows = [w for w in walk(popover) if w.get_css_name() == 'row']
                self.assertGreater(len(rows), 1)
                for row in rows:
                    self.assertGreaterEqual(row.get_height(), 30)
                    for label in (w for w in walk(row) if isinstance(w, Gtk.Label)):
                        color = label.get_style_context().get_color()
                        brightness = (color.red + color.green + color.blue) / 3
                        if row.get_state_flags() & Gtk.StateFlags.SELECTED:
                            self.assertGreater(brightness, 0.8)
                        else:
                            self.assertLess(brightness, 0.45)
                popover.popdown()

    def test_track_menu_copies_service_link_and_disables_invalid_ids(self):
        from unittest.mock import Mock
        self.window.get_clipboard = Mock(return_value=Mock())
        track = SimpleNamespace(id='123:456', title='Песня')
        menu = self.window._track_menu(track, 'Автор')
        button(menu, 'Копировать ссылку на трек').emit('clicked')
        self.window.get_clipboard().set.assert_called_once_with('https://music.yandex.ru/track/123')
        self.assertEqual(self.window.status_label.get_label(), 'Ссылка на трек скопирована')
        menu = self.window._track_menu(SimpleNamespace(id='invalid', title='Песня'), 'Автор')
        self.assertFalse(button(menu, 'Копировать ссылку на трек').get_sensitive())

    def test_restored_equalizer_expansion_survives_resizing(self):
        self.window.set_equalizer_expanded(True)
        for width, height in ((480, 360), (960, 800)):
            self.window.allocate(width, height, -1, None)
            self.assertTrue(self.window.equalizer_body.get_visible())
        self.window.set_equalizer_expanded(False)
        self.assertFalse(self.window.equalizer_body.get_visible())

    def test_quality_popup_marks_the_current_setting(self):
        self.window.present()
        self.window.quality_selector.activate()
        popover = next(w for w in walk(self.window.quality_selector) if isinstance(w, Gtk.Popover))
        drain_until(lambda: popover.get_mapped() and popover.get_height() > 0)
        for selected in (0, 2, 1):
            self.window.quality_selector.set_selected(selected)
            images = [w for w in walk(popover) if isinstance(w, Gtk.Image)
                      and w.get_icon_name() == 'object-select-symbolic']
            self.assertEqual(len(images), 3)
            self.assertEqual([i for i, image in enumerate(images) if image.get_opacity() > 0.9], [selected])
        popover.popdown()

    def test_transitions_honor_disabled_system_animations(self):
        settings = Gtk.Settings.get_default()
        enabled = settings.get_property('gtk-enable-animations')
        settings.set_property('gtk-enable-animations', False)
        try:
            self.window.present()
            drain_until(lambda: self.window.get_width() > 0)
            self.assertEqual(self.window.view_stack.get_transition_type(), Gtk.StackTransitionType.CROSSFADE)
            self.assertEqual(self.window.equalizer_revealer.get_transition_type(), Gtk.RevealerTransitionType.SLIDE_DOWN)
            self.window.set_equalizer_expanded(True)
            drain_until(lambda: self.window.equalizer_revealer.get_child_revealed())
            self.window.set_equalizer_expanded(False)
            drain_until(lambda: not self.window.equalizer_body.get_visible())
            self.window.view_stack.set_visible_child_name('now')
            self.assertFalse(self.window.view_stack.get_transition_running())
        finally:
            settings.set_property('gtk-enable-animations', enabled)

    def test_transport_symbols_are_centered_images(self):
        for widget, icon in ((self.window.favorite_button, 'emblem-favorite-symbolic'),
                             (self.window.shuffle_button, 'media-playlist-shuffle-symbolic'),
                             (self.window.repeat_button, 'media-playlist-repeat-symbolic')):
            with self.subTest(icon=icon):
                images = [w for w in walk(widget) if isinstance(w, Gtk.Image)]
                self.assertTrue(any(image.get_icon_name() == icon for image in images))
        self.assertFalse(self.window.favorite_button.get_always_show_arrow())

    def test_keyboard_volume_and_seek_respect_limits_and_editable_focus(self):
        self.window.set_playback(True, 2000, 60000)
        self.window.set_focus(None)
        for key, modifier in ((Gdk.KEY_Left, Gdk.ModifierType.ALT_MASK),
                              (Gdk.KEY_Right, Gdk.ModifierType.ALT_MASK),
                              (Gdk.KEY_Up, Gdk.ModifierType.CONTROL_MASK)):
            self.assertTrue(self.window._keyboard.emit('key-pressed', key, 0, modifier))
        self.assertEqual(self.calls[:2], [('seek', (0.0,)), ('seek', (7 / 60,))])
        self.assertAlmostEqual(self.window.volume_scale.get_value(), 0.8)
        self.window.set_playback(True, 59000, 60000)
        self.window._keyboard.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType.ALT_MASK)
        self.assertEqual(self.calls[-1], ('seek', (1.0,)))
        self.window.set_focus(self.window.search_entry)
        count = len(self.calls)
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_Left, 0, Gdk.ModifierType.ALT_MASK))
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_Up, 0, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(len(self.calls), count)
        self.window.set_focus(None)
        self.window.set_playback(False, 0, 0)
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType.ALT_MASK))

    def test_sleep_timer_menu_and_countdown(self):
        self.window.actions['set_sleep_timer'] = lambda minutes: self.calls.append(('set_sleep_timer', (minutes,)))
        button(self.window, '15 мин').emit('clicked')
        button(self.window, '30 мин').emit('clicked')
        button(self.window, '60 мин').emit('clicked')
        button(self.window, 'Отменить таймер').emit('clicked')
        self.assertEqual(self.calls, [('set_sleep_timer', (15,)), ('set_sleep_timer', (30,)),
                                      ('set_sleep_timer', (60,)), ('set_sleep_timer', (0,))])
        self.window.set_sleep_timer_remaining(89)
        self.assertTrue(self.window.sleep_timer_label.get_visible())
        self.assertIn('1:29', self.window.sleep_timer_label.get_label())
        self.window.set_sleep_timer_remaining(None)
        self.assertFalse(self.window.sleep_timer_label.get_visible())

    def test_rapid_keyboard_seeks_accumulate_only_after_success(self):
        self.window.set_playback(True, 10000, 100000)
        self.window.set_focus(None)
        fractions = []
        def seek(fraction):
            fractions.append(fraction)
            return round(fraction * 100000)
        self.window.actions['seek'] = seek
        for _ in range(3):
            self.window._keyboard.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType.ALT_MASK)
        self.assertEqual(fractions, [.15, .20, .25])
        self.assertEqual(self.window.position_label.get_label(), '0:25')
        self.window.actions['seek'] = lambda fraction: None
        self.window._keyboard.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType.ALT_MASK)
        self.assertEqual(self.window._position_ms, 25000)

    def test_remaining_clock_handles_unknown_duration_and_track_end(self):
        self.window.set_playback(True, 65000, 180000)
        self.window.time_toggle.set_active(True)
        self.assertEqual(self.window.position_label.get_label(), '−1:55')
        self.window.set_playback(True, 190000, 180000)
        self.assertEqual(self.window.position_label.get_label(), '−0:00')
        self.window.set_playback(False, 0, 0)
        self.assertEqual(self.window.position_label.get_label(), '−−:−−')
        self.window.time_toggle.set_active(False)
        self.assertEqual(self.window.position_label.get_label(), '0:00')

    def test_moments_seek_and_follow_each_track_without_restarting(self):
        first = SimpleNamespace(id='first', title='First', artists=('Artist',), duration_ms=100000)
        second = SimpleNamespace(id='second', title='Second', artists=('Artist',), duration_ms=100000)
        self.assertFalse(self.window.moment_add_button.get_sensitive())
        self.window.set_now_playing(first)
        self.window.set_playback(True, 12000, 100000)
        self.window.moment_add_button.emit('clicked')
        marker = button(self.window.moments_list, '0:12')
        self.window.set_playback(True, 70000, 100000)
        seeks = []
        def seek(fraction):
            seeks.append(fraction)
            return round(fraction * 100000)
        self.window.actions['seek'] = seek
        marker.emit('clicked')
        self.assertEqual(seeks, [.12])
        self.assertEqual(self.window.position_label.get_label(), '0:12')
        self.assertEqual(self.calls, [])
        self.window.set_now_playing(second)
        self.assertFalse(self.window.moment_clear_button.get_sensitive())
        self.window.set_now_playing(first)
        self.assertEqual(button(self.window.moments_list, '0:12').get_label(), '0:12')
        self.window.moment_clear_button.emit('clicked')
        self.assertFalse(self.window.moment_clear_button.get_sensitive())
        self.window.set_playback(False, 0, 0)
        self.assertFalse(self.window.moment_add_button.get_sensitive())

    def test_moment_shortcut_respects_text_entry_and_mini_hides_the_strip(self):
        track = SimpleNamespace(id='first', title='First', artists=('Artist',), duration_ms=100000)
        self.window.set_now_playing(track)
        self.window.set_playback(True, 5000, 100000)
        self.window.set_focus(self.window.search_entry)
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_b, 0, Gdk.ModifierType.CONTROL_MASK))
        self.assertFalse(self.window.moment_clear_button.get_sensitive())
        self.window.set_focus(None)
        self.assertTrue(self.window._keyboard.emit('key-pressed', Gdk.KEY_b, 0, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(button(self.window.moments_list, '0:05').get_label(), '0:05')
        self.window.set_mini_mode(True)
        self.assertFalse(self.window.moments_row.get_visible())
        self.window.set_mini_mode(False)
        self.assertTrue(self.window.moments_row.get_visible())

    def test_mini_mode_preserves_full_size_equalizer_and_playback(self):
        self.window.set_default_size(980, 760)
        self.window.set_equalizer_expanded(True)
        self.window.set_playback(True, 15000, 90000)
        self.window.set_mini_mode(True)
        self.assertFalse(self.window.view_stack.get_visible())
        self.assertFalse(self.window.equalizer_panel.get_visible())
        self.assertEqual(self.window.normal_window_state(), ((980, 760), False))
        self.assertEqual(self.window.position_label.get_label(), '0:15')
        self.assertEqual(self.calls, [])
        self.window.set_mini_mode(False)
        self.assertEqual(tuple(self.window.get_default_size()), (980, 760))
        self.assertTrue(self.window.equalizer_panel.get_visible())
        self.assertTrue(self.window.equalizer_toggle.get_active())
        self.assertTrue(self.window.view_stack.get_visible())

    def test_search_shortcut_restores_library_from_mini_mode(self):
        self.window.set_mini_mode(True)
        self.window._keyboard.emit('key-pressed', Gdk.KEY_f, 0, Gdk.ModifierType.CONTROL_MASK)
        self.assertFalse(self.window.mini_button.get_active())
        self.assertTrue(self.window.view_stack.get_visible())
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'library')

    @unittest.skipUnless(supports_maximize(), 'Requires an X11 window manager with maximization')
    def test_maximizing_mini_does_not_change_full_window_state(self):
        self.window.set_default_size(980, 760)
        self.window.present()
        drain_until(lambda: self.window.get_width() > 0)
        self.window.set_mini_mode(True)
        self.window.maximize()
        drain_until(self.window.is_maximized)
        self.window.set_mini_mode(False)
        drain_until(lambda: not self.window.is_maximized())
        self.assertEqual(self.window.normal_window_state(), ((980, 760), False))

    @unittest.skipUnless(supports_maximize(), 'Requires an X11 window manager with maximization')
    def test_mini_from_maximized_resizes_and_restores_full_geometry(self):
        self.window.set_default_size(980, 760)
        self.window.present()
        drain_until(lambda: self.window.get_width() > 0)
        self.window.maximize()
        drain_until(lambda: self.window.is_maximized() and self.window.get_width() != 980)
        self.window.set_mini_mode(True)
        drain_until(lambda: not self.window.is_maximized()
                    and self.window.get_width() == 480 and self.window.get_height() == 340)
        self.assertEqual(self.window.normal_window_state(), ((980, 760), True))
        self.window.set_mini_mode(False)
        drain_until(self.window.is_maximized)
        self.window.unmaximize()
        drain_until(lambda: not self.window.is_maximized()
                    and self.window.get_width() == 980 and self.window.get_height() == 760)

    def test_jump_to_current_uses_duplicate_occurrence_without_playing(self):
        track = SimpleNamespace(id='same', title='Same', artists=('Artist',), album='', duration_ms=90000)
        self.window.set_tracks([track, track], 'List')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(1) is not None)
        self.window.set_now_playing(track, index=1)
        self.window.present()
        drain_until(lambda: self.window.get_width() > 0)
        self.window.set_mini_mode(True)
        self.window.jump_to_current()
        row = self.window.tracks_list.get_row_at_index(1)
        self.assertIs(self.window.get_focus(), row)
        self.assertTrue(self.window.view_stack.get_visible())
        self.assertEqual(self.calls, [])
        self.window.set_tracks([], 'Other list')
        self.window.jump_to_current()
        self.assertIn('открытом списке', self.window.status_label.get_label())

    def test_jump_scrolls_to_current_track_outside_visible_page(self):
        tracks = [SimpleNamespace(id=str(i), title=f'Track {i}', artists=('Artist',),
                                  album='', duration_ms=90000) for i in range(50)]
        self.window.set_default_size(720, 720)
        self.window.set_tracks(tracks, 'List')
        self.window.set_now_playing(tracks[40], index=40)
        self.window.present()
        drain_until(lambda: self.window.tracks_list.get_row_at_index(49) is not None
                    and self.window.track_scroll.get_height() > 0)
        self.window.jump_to_current()
        adjustment = self.window.track_scroll.get_vadjustment()
        drain_until(lambda: adjustment.get_value() > 0)
        row = self.window.tracks_list.get_row_at_index(40)
        self.assertIs(self.window.get_focus(), row)
        allocated, bounds = row.compute_bounds(self.window.tracks_list)
        self.assertTrue(allocated)
        self.assertLessEqual(adjustment.get_value(), bounds.get_y())
        self.assertGreaterEqual(adjustment.get_value() + adjustment.get_page_size(),
                                bounds.get_y() + bounds.get_height())

    def test_help_is_searchable_local_and_has_verified_project_links(self):
        from impuls_player import __version__
        button(self.window, 'Справка и о программе').emit('clicked')
        dialog = self.window._help_window
        self.assertIsNotNone(dialog)
        self.window._show_help()
        self.assertIs(self.window._help_window, dialog)
        labels = [w.get_label() for w in walk(dialog) if isinstance(w, Gtk.Label)]
        self.assertIn(f'Impuls {__version__}', labels)
        self.assertTrue(any('GPL-3.0-only' in text for text in labels))
        uris = {w.get_uri() for w in walk(dialog) if isinstance(w, Gtk.LinkButton)}
        self.assertEqual(uris, {'https://github.com/AnoY606-git/Impuls',
                               'https://github.com/AnoY606-git/Impuls/issues',
                               'https://github.com/AnoY606-git/Impuls/releases',
                               'https://github.com/AnoY606-git/Impuls/blob/main/LICENSE'})
        search = next(w for w in walk(dialog) if isinstance(w, Gtk.SearchEntry))
        search.set_text('lossless')
        search.emit('search-changed')
        topics = [w for w in walk(dialog) if isinstance(w, Gtk.Expander)]
        matches = [w for w in topics if w.get_visible()]
        self.assertEqual([w.get_label() for w in matches], ['Звук, качество и эквалайзер'])
        self.assertTrue(matches[0].get_expanded())
        search.set_text('несуществующий-раздел')
        search.emit('search-changed')
        self.assertFalse(any(w.get_visible() for w in topics))
        self.assertTrue(any(isinstance(w, Gtk.Label) and w.get_label() == 'Ничего не найдено. Попробуйте другое слово.'
                            and w.get_visible() for w in walk(dialog)))
        self.assertEqual(self.calls, [])

    def test_help_fits_small_windows_and_closes_with_player(self):
        self.window._show_help()
        dialog = self.window._help_window
        dialog.set_default_size(360, 360)
        # present() may already have allocated the initial 560px window;
        # wait for the asynchronous resize, not just any existing allocation.
        drain_until(lambda: 0 < dialog.get_width() <= 380)
        self.assertLessEqual(dialog.get_width(), 380)
        scroller = next(w for w in walk(dialog) if isinstance(w, Gtk.ScrolledWindow))
        self.assertEqual(scroller.get_policy(), (Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC))
        button(dialog, 'Закрыть').emit('clicked')
        self.assertIsNone(self.window._help_window)
        self.window._show_help()
        dialog = self.window._help_window
        dialog.close()
        self.assertIsNone(self.window._help_window)
        self.window._show_help()
        self.window.destroy()
        self.assertIsNone(self.window._help_window)

    def test_album_column_header_and_rows_share_pixel_alignment(self):
        tracks = [SimpleNamespace(id=str(i), title='Длинное название 🎵 ' * 12,
                                  artists=('Исполнитель ' * 12,), album=album, duration_ms=123456)
                  for i, album in enumerate(('Короткий', 'Альбом с длинным названием 漢字 ' * 10))]
        self.window.set_tracks(tracks, 'Проверка колонок')
        self.window.present()
        drain_until(lambda: self.window.tracks_list.get_row_at_index(1) is not None)
        self.window.allocate(960, 800, -1, None)
        header_ok, header = self.window.column_album.compute_bounds(self.window)
        self.assertTrue(header_ok)
        for label in self.window._album_labels:
            success, bounds = label.compute_bounds(self.window)
            self.assertTrue(success)
            self.assertAlmostEqual(bounds.get_x(), header.get_x(), delta=1)
            self.assertAlmostEqual(bounds.get_width(), header.get_width(), delta=1)
            self.assertEqual(label.get_xalign(), 0)
            self.assertEqual(label.get_ellipsize(), Pango.EllipsizeMode.END)
        self.window.allocate(480, 600, -1, None)
        self.assertFalse(self.window.column_album.get_visible())
        self.assertTrue(all(not label.get_visible() for label in self.window._album_labels))
        self.window.set_now_playing(tracks[0])
        self.assertEqual(self.window.now_title.get_tooltip_text(), tracks[0].title)
        self.assertEqual(self.window.now_artist.get_tooltip_text(), tracks[0].artists[0])
        self.assertEqual(self.window.visual_track_title.get_tooltip_text(), tracks[0].artists[0] + ' — ' + tracks[0].title)
        self.window.set_playlists([SimpleNamespace(title='Плейлист 🎹 ' * 20, track_count=2)])
        drain_until(lambda: self.window.playlists_list.get_row_at_index(0) is not None)
        title = next(w for w in walk(self.window.playlists_list) if isinstance(w, Gtk.Label) and w.has_css_class('track-title'))
        self.assertEqual(title.get_tooltip_text(), 'Плейлист 🎹 ' * 20)

    def test_refresh_and_list_playback_use_current_view_actions(self):
        self.assertTrue(hasattr(self.window, 'refresh_button'), 'Refresh control is missing')
        self.window.refresh_button.emit('clicked')
        self.assertFalse(self.window.play_all_button.get_sensitive())

        track = SimpleNamespace(id='1', title='Трек', artists=('Автор',), album='', duration_ms=60000)
        self.window.set_tracks([track], 'Любимые треки')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        self.window.play_all_button.emit('clicked')
        self.window.shuffle_all_button.emit('clicked')
        self.assertEqual(self.calls, [('refresh_view', ()), ('play_all', (False,)), ('play_all', (True,))])
        self.window.set_playlists([])
        self.assertFalse(self.window.play_all_button.get_sensitive())

    def test_window_titlebar_matches_player_and_keeps_window_controls(self):
        titlebar = self.window.get_titlebar()
        self.assertIsInstance(titlebar, Gtk.HeaderBar)
        self.assertTrue(titlebar.has_css_class('window-header'))
        self.assertTrue(titlebar.get_show_title_buttons())
        self.assertEqual(titlebar.get_decoration_layout(), ':minimize,maximize,close')
        self.assertTrue(self.window.get_resizable())
        self.window.present()
        drain_until(lambda: titlebar.get_width() > 0)
        for name in ('minimize', 'maximize', 'close'):
            control = next(w for w in walk(titlebar) if isinstance(w, Gtk.Button) and w.has_css_class(name))
            self.assertTrue(control.get_visible())
            self.assertGreater(control.get_width(), 0)
            icon = next(w for w in walk(control) if isinstance(w, Gtk.Image))
            self.assertGreater(icon.get_style_context().get_color().alpha, 0.5)

    def test_muting_restores_last_nonzero_volume(self):
        self.assertTrue(hasattr(self.window, 'mute_button'), 'Mute control is missing')
        self.window.volume_scale.set_value(0.4)
        self.calls.clear()
        self.window.mute_button.emit('clicked')
        self.assertEqual(self.window.volume_scale.get_value(), 0.0)
        self.window.mute_button.emit('clicked')
        self.assertAlmostEqual(self.window.volume_scale.get_value(), 0.4)
        self.assertEqual(self.calls, [('set_volume', (0.0,)), ('set_volume', (0.4,))])

    def test_manual_equalizer_choice_survives_resizing(self):
        self.window.allocate(480, 360, -1, None)
        self.window.equalizer_toggle.emit('clicked')
        self.assertTrue(self.window.equalizer_body.get_visible())
        self.window.allocate(860, 720, -1, None)
        self.window.allocate(480, 360, -1, None)
        self.assertTrue(self.window.equalizer_body.get_visible())

    def test_equalizer_starts_collapsed_at_all_window_sizes(self):
        for width, height in ((480, 360), (600, 600), (960, 800), (1280, 1000)):
            with self.subTest(size=(width, height)):
                self.window.allocate(width, height, -1, None)
                self.assertFalse(self.window.equalizer_body.get_visible())

    def test_compact_track_rows_hide_album_but_keep_title_and_artist(self):
        track = SimpleNamespace(id='1', title='Название', artists=('Исполнитель',), album='Альбом', duration_ms=60000)
        self.window.set_tracks([track], 'Поиск')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        row = self.window.tracks_list.get_row_at_index(0)
        self.assertTrue(any(isinstance(w, Gtk.Label) and w.get_label() == 'Название' for w in walk(row)))
        self.assertTrue(any(isinstance(w, Gtk.Label) and w.get_label() == 'Исполнитель' for w in walk(row)))
        self.window.allocate(480, 360, -1, None)
        album = next(w for w in walk(row) if isinstance(w, Gtk.Label) and w.get_label() == 'Альбом')
        self.assertFalse(album.get_visible())
        self.assertFalse(self.window.column_album.get_visible())
        self.window.allocate(860, 720, -1, None)
        self.assertTrue(album.get_visible())
        self.assertTrue(self.window.column_album.get_visible())

    def test_navigation_marks_current_section(self):
        self.window.liked_button.emit('clicked')
        self.assertTrue(self.window.liked_button.has_css_class('active-nav'))
        self.window.wave_button.emit('clicked')
        self.assertFalse(self.window.liked_button.has_css_class('active-nav'))
        self.assertTrue(self.window.wave_button.has_css_class('active-nav'))
        self.window.search_entry.set_text('Кино')
        self.window.search_entry.emit('activate')
        self.assertFalse(self.window.wave_button.has_css_class('active-nav'))

    def test_status_tooltip_preserves_full_message(self):
        self.window.set_status('Очень длинное сообщение об ошибке запроса')
        self.assertEqual(self.window.status_label.get_tooltip_text(), 'Очень длинное сообщение об ошибке запроса')

    def test_close_interception_preserves_live_window_sources(self):
        self.window.actions['close_window'] = lambda: True
        self.assertTrue(self.window._on_close_request(self.window))
        self.assertFalse(self.window._destroyed)
        self.window.actions['close_window'] = lambda: False
        self.assertFalse(self.window._on_close_request(self.window))
        self.assertTrue(self.window._destroyed)

    def test_hide_window_is_available_only_with_tray_host(self):
        self.assertTrue(hasattr(self.window, 'hide_button'), 'Hide menu action is missing')
        self.assertFalse(self.window.hide_button.get_sensitive())
        self.window.set_tray_available(True)
        self.assertTrue(self.window.hide_button.get_sensitive())
        self.window.set_tray_available(False)
        self.assertFalse(self.window.hide_button.get_sensitive())

    def test_visualizer_choice_changes_renderer_without_resetting_audio(self):
        self.assertTrue(hasattr(self.window, 'visualization_selector'), 'Visualization selector is missing')
        self.window.set_spectrum_available(True)
        self.window.set_spectrum((0.5,) * 32)
        self.window.visualization_selector.set_selected(1)
        self.assertEqual(self.window._visualization_mode, 'bars')
        self.assertEqual(self.window._spectrum_levels, (0.5,) * 32)

    def test_long_metadata_and_controls_fit_compact_width(self):
        track = SimpleNamespace(id='1', title='Длинное название ' * 30, artists=('Длинное имя ' * 20,), album='Длинный альбом ' * 20, duration_ms=60000)
        self.window.set_tracks([track], 'Медиатека ' * 30)
        self.window.set_account('Длинное имя аккаунта ' * 20)
        self.window.set_now_playing(track)
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        for width in (480, 600, 860):
            with self.subTest(width=width):
                self.window.allocate(width, 360, -1, None)
                self.assertLessEqual(self.window._root_box.get_width(), width)
                controls = (self.window.play_button, self.window.volume_scale, self.window.quality_selector,
                            self.window.search_entry, self.window.account_button, self.window.refresh_button)
                for control in controls:
                    success, bounds = control.compute_bounds(self.window._root_box)
                    self.assertTrue(success)
                    self.assertGreater(control.get_width(), 0)
                    self.assertGreaterEqual(bounds.get_x(), 0)
                    self.assertLessEqual(bounds.get_x() + bounds.get_width(), width)

    def test_search_and_library_navigation_emit_actions(self):
        search = self.window.search_entry
        search.set_text('кино')
        search.emit('activate')
        button(self.window, 'Моя волна').emit('clicked')
        button(self.window, 'Любимые треки').emit('clicked')
        button(self.window, 'Плейлисты').emit('clicked')
        self.assertEqual(self.calls, [('search', ('кино',)), ('wave', ()), ('liked_tracks', ()), ('playlists', ())])

    def test_track_and_playlist_activation_passes_selected_object(self):
        track = SimpleNamespace(id='1', title='Кукушка', artists=('Кино',), album='Чёрный альбом', duration_ms=200000, cover_url=None)
        playlist = SimpleNamespace(id='2', title='Рок', owner_uid='42', track_count=1, cover_url=None)
        self.window.set_tracks([track], 'Поиск')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        track_list = next(w for w in walk(self.window) if isinstance(w, Gtk.ListBox) and w.get_visible())
        row = track_list.get_row_at_index(0)
        track_list.emit('row-activated', row)
        self.window.set_playlists([playlist])
        drain_until(lambda: self.window.playlists_list.get_row_at_index(0) is not None)
        lists = [w for w in walk(self.window) if isinstance(w, Gtk.ListBox) and w.get_visible()]
        lists[-1].emit('row-activated', lists[-1].get_row_at_index(0))
        self.assertEqual(self.calls, [('play_track', (track, 0)), ('open_playlist', (playlist,))])

    def test_duplicate_tracks_play_the_clicked_occurrence(self):
        track = SimpleNamespace(id='1', title='Кукушка', artists=('Кино',), album='Чёрный альбом', duration_ms=200000, cover_url=None)
        self.window.set_tracks([track, track], 'Плейлист')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(1) is not None)
        second = self.window.tracks_list.get_row_at_index(1)
        self.window.tracks_list.emit('row-activated', second)
        self.assertEqual(self.calls, [('play_track', (track, 1))])
        self.calls.clear()
        next(w for w in walk(second) if isinstance(w, Gtk.Button)).emit('clicked')
        self.assertEqual(self.calls, [('play_track', (track, 1))])

    def test_large_list_yields_to_the_main_loop_before_finishing(self):
        track = SimpleNamespace(id='1', title='Песня', artists=('Музыкант',), album='', duration_ms=200000, cover_url=None)
        yielded = []
        GLib.idle_add(lambda: (yielded.append(True), False)[1])
        self.window.set_tracks([track] * 500, 'Песни')
        self.assertLess(len(self.window._track_rows), 500)
        drain_until(lambda: bool(yielded))
        self.assertLess(len(self.window._track_rows), 500)
        drain_until(lambda: self.window.tracks_list.get_row_at_index(499) is not None)

    def test_new_view_cancels_old_render_and_loading_is_immediate(self):
        old = SimpleNamespace(id='1', title='Старый', artists=('A',), album='', duration_ms=0, cover_url=None)
        new = SimpleNamespace(id='2', title='Новый', artists=('B',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([old] * 300, 'Старые')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        old_button = next(w for w in walk(self.window.tracks_list.get_row_at_index(0)) if isinstance(w, Gtk.Button))
        self.window.view_stack.set_visible_child_name('now')
        self.window.show_loading('Любимые')
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'library')
        self.assertEqual(self.window.heading_label.get_label(), 'Любимые')
        self.assertIn('Загрузка', self.window.track_empty.get_label())
        old_button.emit('clicked')
        self.assertEqual(self.calls, [])
        self.window.set_tracks([new], 'Любимые')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        self.assertIsNone(self.window.tracks_list.get_row_at_index(1))
        self.window.tracks_list.emit('row-activated', self.window.tracks_list.get_row_at_index(0))
        self.assertEqual(self.calls, [('play_track', (new, 0))])

    def test_hidden_previous_list_cannot_activate_a_stale_track(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track], 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        old_list = self.window.tracks_list
        old_row = old_list.get_row_at_index(0)
        self.window.set_playlists([])
        old_list.emit('row-activated', old_row)
        self.assertEqual(self.calls, [])

    def test_loaded_summary_counts_duplicate_occurrences_and_appended_pages(self):
        track = SimpleNamespace(id='1', title='Повтор', artists=('A',), album='', duration_ms=90500)
        self.window.set_tracks([track, track], 'Песни')
        summary = getattr(self.window, 'list_summary_label', None)
        self.assertIsNotNone(summary, 'Loaded tracks need a count and total duration')
        self.assertEqual(summary.get_label(), 'Загружено: 2 · 3:01')
        self.window.set_pagination(True)
        self.assertEqual(summary.get_label(), 'Загружено: 2 · 3:01')
        self.window.append_tracks([track])
        self.assertEqual(summary.get_label(), 'Загружено: 3 · 4:31')
        self.window.append_tracks([])
        self.assertEqual(summary.get_label(), 'Загружено: 3 · 4:31')
        self.assertIn('загруженных', summary.get_tooltip_text())

    def test_loaded_summary_marks_unknown_duration_and_resets_for_new_views(self):
        known = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=60000)
        unknown = SimpleNamespace(id='2', title='B', artists=('A',), album='', duration_ms=0)
        self.window.set_tracks([known, unknown], 'Песни')
        summary = getattr(self.window, 'list_summary_label', None)
        self.assertIsNotNone(summary, 'Loaded tracks need a count and total duration')
        self.assertEqual(summary.get_label(), 'Загружено: 2 · 1:00+')
        self.assertIn('неизвестна', summary.get_tooltip_text())
        self.window.set_playlists([])
        self.assertFalse(summary.get_visible())
        self.assertEqual(summary.get_label(), '')
        self.window.set_tracks([unknown], 'Другой список')
        self.assertTrue(summary.get_visible())
        self.assertEqual(summary.get_label(), 'Загружено: 1 · 0:00+')
        self.window.set_tracks([], 'Пустой список')
        self.assertEqual(summary.get_label(), 'Загружено: 0 · 0:00')

    def test_loaded_summary_does_not_expand_compact_window(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=5400000)
        self.window.set_tracks([track] * 30, 'Песни')
        summary = getattr(self.window, 'list_summary_label', None)
        self.assertIsNotNone(summary, 'Loaded tracks need a count and total duration')
        self.window.set_default_size(480, 600)
        self.window.present()
        drain_until(lambda: self.window.get_width() > 0 and summary.get_width() > 0)
        self.assertLessEqual(self.window.get_width(), 480)
        self.assertIn('2700:00', summary.get_label())
        self.assertEqual(self.window.get_child().get_hadjustment().get_upper(),
                         self.window.get_child().get_hadjustment().get_page_size())

    def test_append_tracks_preserves_indices_duplicates_and_pagination(self):
        track = SimpleNamespace(id='1', title='Повтор', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track], 'Песни')
        self.window.append_tracks([track, track])
        drain_until(lambda: self.window.tracks_list.get_row_at_index(2) is not None)
        self.window.tracks_list.emit('row-activated', self.window.tracks_list.get_row_at_index(2))
        self.assertEqual(self.calls, [('play_track', (track, 2))])
        self.window.set_pagination(True)
        self.assertTrue(self.window.more_button.get_visible())
        self.assertTrue(self.window.more_button.get_sensitive())
        self.window.more_button.emit('clicked')
        self.assertEqual(self.calls[-1], ('load_more', ()))
        self.window.more_button.emit('clicked')
        self.assertEqual(sum(name == 'load_more' for name, _ in self.calls), 1)
        self.window.set_pagination(True, loading=True)
        self.assertFalse(self.window.more_button.get_sensitive())
        self.window.set_pagination(False)
        self.assertFalse(self.window.more_button.get_visible())

    def test_destroy_cancels_pending_idle_render(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track] * 300, 'Песни')
        self.assertTrue(self.window._render_source_id)
        self.window.destroy()
        self.assertFalse(self.window._render_source_id)

    def test_bottom_scroll_coalesces_duplicate_adjustment_signals(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track] * 100, 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(99) is not None)
        adjustment = near_bottom_adjustment(self.window)
        self.window.set_pagination(True)
        adjustment.emit('value-changed')
        adjustment.emit('changed')
        adjustment.emit('value-changed')
        drain_until(lambda: any(name == 'load_more' for name, _ in self.calls))
        self.assertEqual(sum(name == 'load_more' for name, _ in self.calls), 1)
        self.assertEqual(self.calls[-1], ('load_more', (True,)))
        self.assertFalse(self.window.more_button.get_sensitive())

    def test_page_error_stops_auto_retry_until_manual_button(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track] * 100, 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(99) is not None)
        adjustment = near_bottom_adjustment(self.window)
        self.window.set_pagination(True)
        adjustment.emit('value-changed')
        drain_until(lambda: any(name == 'load_more' for name, _ in self.calls))
        self.window.set_pagination(True, error=True)
        self.assertEqual(self.window.more_button.get_label(), 'Повторить загрузку')
        for _ in range(4):
            adjustment.emit('changed')
            adjustment.emit('value-changed')
            GLib.MainContext.default().iteration(False)
        self.assertEqual(sum(name == 'load_more' for name, _ in self.calls), 1)
        self.window.more_button.emit('clicked')
        self.assertEqual(sum(name == 'load_more' for name, _ in self.calls), 2)

    def test_autoload_waits_for_layout_before_reading_scroll_extent(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track] * 40, 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(39) is not None)
        last = self.window.tracks_list.get_row_at_index(39)
        self.assertEqual(last.get_height(), 0)
        adjustment = near_bottom_adjustment(self.window, allocate=False)
        self.window.set_pagination(True)
        adjustment.emit('changed')
        for _ in range(4):
            GLib.MainContext.default().iteration(False)
        self.assertEqual(self.calls, [])
        last.allocate(400, 56, -1, None)
        adjustment.emit('changed')
        drain_until(lambda: bool(self.calls))
        self.assertEqual(self.calls, [('load_more', (True,))])

    def test_empty_page_does_not_spin_forever(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track], 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        adjustment = near_bottom_adjustment(self.window)
        def empty_page(background=False):
            self.calls.append(('load_more', (background,)))
            self.window.append_tracks([])
            self.window.set_pagination(True)
        self.window.actions['load_more'] = empty_page
        self.window.set_pagination(True)
        adjustment.emit('changed')
        drain_until(lambda: bool(self.calls))
        for _ in range(4):
            adjustment.emit('changed')
            GLib.MainContext.default().iteration(False)
        self.assertEqual(self.calls, [('load_more', (True,))])
        self.assertTrue(self.window.more_button.get_sensitive())

    def test_short_pages_continue_until_controller_reports_end(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track], 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        adjustment = near_bottom_adjustment(self.window)
        def next_page(background=False):
            self.calls.append(('load_more', (background,)))
            self.window.append_tracks([track])
            self.window.set_pagination(len(self.calls) < 3)
            def layout_last_row():
                row = self.window.tracks_list.get_row_at_index(self.window._rendered_track_count - 1)
                if row is None:
                    return GLib.SOURCE_CONTINUE
                row.allocate(400, 56, -1, None)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(layout_last_row)
        self.window.actions['load_more'] = next_page
        self.window.set_pagination(True)
        adjustment.emit('changed')
        drain_until(lambda: len(self.calls) == 3)
        drain_until(lambda: self.window.tracks_list.get_row_at_index(3) is not None)
        self.assertFalse(self.window.more_button.get_visible())

    def test_loading_a_new_view_clears_pending_pagination(self):
        self.window.set_pagination(True, loading=True)
        self.window.show_loading('Новый список')
        self.assertFalse(self.window.more_button.get_visible())
        self.window.set_pagination(True)
        self.window.more_button.emit('clicked')
        self.assertEqual(self.calls, [('load_more', ())])

    def test_load_error_replaces_loading_placeholder_without_changing_rows(self):
        self.window.show_loading('Избранное')
        self.window.show_load_error('Не удалось загрузить')
        self.assertEqual(self.window.track_empty.get_label(), 'Не удалось загрузить')
        self.assertEqual(self.window.status_label.get_label(), 'Не удалось загрузить')
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track], 'Песни')
        drain_until(lambda: self.window.tracks_list.get_row_at_index(0) is not None)
        self.window.show_load_error('Ошибка следующей страницы')
        self.assertIsNotNone(self.window.tracks_list.get_row_at_index(0))

    def test_playing_track_is_highlighted_after_idle_render(self):
        a = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        b = SimpleNamespace(id='2', title='B', artists=('B',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([a, b], 'Песни')
        self.window.set_now_playing(b)
        drain_until(lambda: self.window.tracks_list.get_row_at_index(1) is not None)
        self.assertFalse(self.window.tracks_list.get_row_at_index(0).has_css_class('current-track'))
        self.assertTrue(self.window.tracks_list.get_row_at_index(1).has_css_class('current-track'))

    def test_selected_occurrence_of_duplicate_is_highlighted(self):
        track = SimpleNamespace(id='1', title='Повтор', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_tracks([track, track], 'Песни')
        self.window.set_now_playing(track, index=1)
        drain_until(lambda: self.window.tracks_list.get_row_at_index(1) is not None)
        self.assertFalse(self.window.tracks_list.get_row_at_index(0).has_css_class('current-track'))
        self.assertTrue(self.window.tracks_list.get_row_at_index(1).has_css_class('current-track'))

    def test_navigation_from_visualizer_opens_library_immediately(self):
        self.window.view_stack.set_visible_child_name('now')
        self.window.wave_button.emit('clicked')
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'library')
        self.assertEqual(self.calls, [('wave', ())])

    def test_backdrop_controls_keep_contrasting_foregrounds(self):
        track = SimpleNamespace(id='1', title='A', artists=('A',), album='', duration_ms=0, cover_url=None)
        self.window.set_now_playing(track)
        self.window.set_equalizer_available(True)
        def label(widget, text):
            return next(item for item in walk(widget) if isinstance(item, Gtk.Label) and item.get_label() == text)
        def backdrop(widget):
            for item in walk(widget):
                item.set_state_flags(Gtk.StateFlags.BACKDROP, False)
        def brightness(widget):
            c = widget.get_style_context().get_color()
            return 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue
        def icon(widget):
            return next(item for item in walk(widget) if isinstance(item, Gtk.Image))
        for widget in (self.window.shuffle_button, self.window.repeat_button, self.window.favorite_button):
            backdrop(widget)
            self.assertLess(brightness(icon(widget)), 0.45)
        backdrop(self.window.wave_button)
        self.assertLess(brightness(label(self.window.wave_button, 'Моя волна')), 0.45)
        backdrop(self.window.view_switcher)
        self.assertLess(brightness(label(self.window.view_switcher, 'Медиатека')), 0.45)
        self.window.shuffle_button.set_active(True)
        self.window.shuffle_button.set_state_flags(Gtk.StateFlags.BACKDROP | Gtk.StateFlags.FOCUSED, False)
        self.assertGreater(brightness(icon(self.window.shuffle_button)), 0.7)
        self.window.set_now_playing(None)
        backdrop(self.window.favorite_button)
        disabled = brightness(icon(self.window.favorite_button))
        self.assertFalse(self.window.favorite_button.get_sensitive())
        self.assertGreater(disabled, 0.3)
        self.assertLess(disabled, 0.6)
        self.window.set_equalizer_available(False)
        backdrop(self.window.preset_selector)
        self.assertFalse(self.window.preset_selector.get_sensitive())
        self.assertLess(brightness(label(self.window.preset_selector, 'Ровный')), 0.5)

    def test_displaying_position_does_not_trigger_seek(self):
        self.window.set_playback(True, 15000, 60000)
        self.window.set_playback(True, 16000, 60000)
        self.assertFalse(any(name == 'seek' for name, _ in self.calls))
        labels = [w.get_label() for w in walk(self.window) if isinstance(w, Gtk.Label)]
        self.assertIn('0:16', labels)
        self.assertIn('1:00', labels)

    def test_small_displays_can_scroll_to_all_controls(self):
        width, height = self.window.get_size_request()
        self.assertLessEqual(width, 480)
        self.assertLessEqual(height, 360)
        self.assertIsInstance(self.window.get_child(), Gtk.ScrolledWindow)
        horizontal, vertical = self.window.get_child().get_policy()
        self.assertEqual((horizontal, vertical), (Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC))

    def test_compact_window_collapses_equalizer_but_can_expand_it(self):
        self.window.allocate(480, 360, -1, None)
        self.assertFalse(self.window.equalizer_body.get_visible())
        self.assertTrue(all(not widget.get_visible() for widget in self.window._compact_captions))
        self.window.equalizer_toggle.emit('clicked')
        self.assertTrue(self.window.equalizer_body.get_visible())
        self.window.allocate(860, 720, -1, None)
        self.assertTrue(self.window.equalizer_body.get_visible())
        self.assertTrue(all(widget.get_visible() for widget in self.window._compact_captions))

    def test_status_is_visible_in_both_library_and_visualizer_views(self):
        self.window.view_stack.set_visible_child_name('now')
        self.window.set_status('Не удалось отправить обратную связь')
        self.assertEqual(self.window.status_label.get_label(), 'Не удалось отправить обратную связь')
        self.assertIsNone(self.window.status_label.get_ancestor(Gtk.Stack))

    def test_background_work_keeps_navigation_and_sign_in_available(self):
        self.window.set_busy(True)
        for widget in (self.window.search_entry, self.window.wave_button, self.window.liked_button, self.window.playlists_button, self.window.account_button):
            self.assertTrue(widget.get_sensitive())

    def test_empty_search_does_not_emit_action(self):
        search = self.window.search_entry
        search.set_text('  ')
        search.emit('activate')
        self.assertEqual(self.calls, [])

    def test_device_login_respects_remember_choice_and_shows_valid_code(self):
        button(self.window, 'Войти').emit('clicked')
        dialog = self.window._login_window
        remember = next(w for w in walk(dialog) if isinstance(w, Gtk.CheckButton))
        remember.set_active(True)
        button(dialog, 'Войти по коду').emit('clicked')
        self.assertEqual(self.calls[-1], ('device_login', (True,)))
        self.window.show_device_code('A1B2', 'https://ya.ru/device')
        dialog = self.window._device_window
        self.assertIsNotNone(dialog)
        link = next(w for w in walk(dialog) if isinstance(w, Gtk.LinkButton))
        self.assertEqual(link.get_uri(), 'https://ya.ru/device')
        self.window.set_account('listener')
        self.assertIsNone(self.window._device_window)

    def test_manual_login_passes_token_only_after_explicit_submission(self):
        button(self.window, 'Войти').emit('clicked')
        dialog = self.window._login_window
        password = next(w for w in walk(dialog) if isinstance(w, Gtk.PasswordEntry))
        password.set_text('test-token')
        self.assertEqual(self.calls, [])
        button(dialog, 'Войти с токеном').emit('clicked')
        self.assertEqual(self.calls[-1], ('login', ('test-token', False)))
        self.assertEqual(password.get_text(), '')

    def test_unsafe_device_url_is_not_presented_as_link(self):
        self.window.show_device_code('A1B2', 'https://ya.ru.evil.test/device')
        self.assertIsNone(self.window._device_window)
        self.assertIsNotNone(self.window._error_window)
        self.window._close_error_window()

    def test_playback_and_favorite_actions_are_wired(self):
        track = SimpleNamespace(id='1', title='Кукушка', artists=('Кино',), album='Чёрный альбом', duration_ms=200000, cover_url=None)
        self.window.set_now_playing(track)
        self.window.set_playback(True, 15000, 60000)
        self.window.play_button.emit('clicked')
        self.window.seek_scale.emit('change-value', Gtk.ScrollType.JUMP, 0.5)
        self.window.volume_scale.set_value(0.2)
        popover = self.window.favorite_button.get_popover()
        button(popover, 'Добавить в любимое').emit('clicked')
        button(popover, 'Удалить из любимого').emit('clicked')
        self.assertEqual(self.calls, [('toggle_playback', ()), ('seek', (0.5,)), ('set_volume', (0.2,)), ('set_liked', (track, True)), ('set_liked', (track, False))])

    def test_quality_choice_is_for_next_track_and_emits_the_correct_codes(self):
        self.assertEqual(self.window.quality_selector.get_selected(), 1)
        self.assertEqual(self.calls, [])
        self.window.set_stream_quality('mp3', 192)
        self.window.quality_selector.set_selected(0)
        self.window.quality_selector.set_selected(2)
        self.window.quality_selector.set_selected(1)
        self.assertEqual(self.calls, [('set_quality', ('nq',)), ('set_quality', ('lossless',)), ('set_quality', ('hq',))])
        self.assertIn('MP3', self.window.stream_quality_label.get_label())
        self.assertNotIn('FLAC', self.window.stream_quality_label.get_label())

    def test_actual_stream_format_uses_codec_and_clears_when_unknown(self):
        self.window.set_stream_quality('mp3', 192)
        self.assertIn('MP3', self.window.stream_quality_label.get_label())
        self.assertIn('192', self.window.stream_quality_label.get_label())
        for codec in ('flac', 'flac-mp4'):
            with self.subTest(codec=codec):
                self.window.set_stream_quality(codec, None)
                self.assertIn('FLAC', self.window.stream_quality_label.get_label())
                self.assertTrue(self.window.stream_quality_label.get_visible())
        self.window.set_stream_quality('aac', 320)
        self.assertIn('AAC', self.window.stream_quality_label.get_label())
        self.assertNotIn('FLAC', self.window.stream_quality_label.get_label())
        for codec, label in (('aac-mp4', 'AAC'), ('he-aac', 'HE-AAC'), ('he-aac-mp4', 'HE-AAC')):
            with self.subTest(codec=codec):
                self.window.set_stream_quality(codec, 192)
                self.assertIn(label, self.window.stream_quality_label.get_label())
                self.assertNotIn('неизвестен', self.window.stream_quality_label.get_label())
        self.window.set_stream_quality(None, None)
        self.assertFalse(self.window.stream_quality_label.get_visible())

    def test_quality_control_fits_beside_volume_at_compact_size(self):
        self.window.quality_selector.set_selected(2)
        self.window.set_stream_quality('flac-mp4', 1411)
        for width, height in ((480, 360), (720, 560), (860, 720)):
            with self.subTest(size=(width, height)):
                self.window.allocate(width, height, -1, None)
                deck = self.window.quality_selector.get_parent()
                self.assertLessEqual(deck.get_width(), width)
                for control in (self.window.quality_selector, self.window.volume_scale):
                    success, bounds = control.compute_bounds(deck)
                    self.assertTrue(success)
                    self.assertGreater(control.get_width(), 0)
                    self.assertLessEqual(bounds.get_x() + bounds.get_width(), deck.get_width() + 1)

    def test_equalizer_bands_send_gain_and_reset_only_changes_adjusted_bands(self):
        self.window.set_equalizer_available(True)
        sliders = self.window.equalizer_sliders
        self.assertEqual(len(sliders), 10)
        self.assertEqual([slider.get_value() for slider in sliders], [0.0] * 10)
        self.assertEqual(self.calls, [])
        sliders[0].set_value(-12)
        sliders[9].set_value(12)
        self.assertEqual(self.calls, [('set_equalizer', (0, -12.0)), ('set_equalizer', (9, 12.0))])
        button(self.window, 'Сброс').emit('clicked')
        self.assertEqual([slider.get_value() for slider in sliders], [0.0] * 10)
        self.assertEqual(self.calls[-2:], [('set_equalizer', (0, 0.0)), ('set_equalizer', (9, 0.0))])

    def test_equalizer_controls_are_disabled_without_plugin(self):
        self.window.set_equalizer_available(False)
        self.assertTrue(all(not slider.get_sensitive() for slider in self.window.equalizer_sliders))
        self.assertFalse(button(self.window, 'Сброс').get_sensitive())
        self.assertIn('gstreamer1-plugins-good', self.window.equalizer_hint.get_label())
        self.window.set_equalizer_available(True)
        self.assertTrue(all(slider.get_sensitive() for slider in self.window.equalizer_sliders))
        self.assertTrue(button(self.window, 'Сброс').get_sensitive())

    def test_preset_selection_and_external_values_do_not_feed_back(self):
        from impuls_player.presets import EQUALIZER_PRESETS
        self.window.set_equalizer_available(True)
        names = list(EQUALIZER_PRESETS)
        self.window.preset_selector.set_selected(names.index('Рок'))
        self.assertEqual(self.calls, [('equalizer_preset', ('Рок',))])
        self.calls.clear()
        self.window.set_equalizer_values(EQUALIZER_PRESETS['Рок'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.window.preset_selector.get_selected_item().get_string(), 'Рок')
        self.window.equalizer_sliders[0].set_value(11.5)
        self.assertEqual(self.calls, [('set_equalizer', (0, 11.5))])
        self.assertEqual(self.window.preset_selector.get_selected_item().get_string(), 'Пользовательский')
        self.calls.clear()
        self.window.set_equalizer_values(EQUALIZER_PRESETS['Ровный'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.window.preset_selector.get_selected_item().get_string(), 'Ровный')

    def test_shuffle_and_repeat_state_updates_do_not_trigger_actions(self):
        self.window.set_shuffle_state(True)
        self.window.set_repeat_state(True)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.window.shuffle_button.get_active())
        self.assertTrue(self.window.repeat_button.get_active())
        self.window.shuffle_button.set_active(False)
        self.window.repeat_button.set_active(False)
        self.assertEqual(self.calls, [('set_shuffle', (False,)), ('set_repeat', (False,))])

    def test_spectrum_requires_real_data_and_reports_unavailable_plugin(self):
        self.window.set_spectrum_available(False)
        self.assertIn('gstreamer1-plugins-good', self.window.spectrum_hint.get_label())
        self.window.set_spectrum_available(True)
        self.assertEqual(self.window._spectrum_levels, (0.0,) * 32)
        self.window.set_spectrum((0.5,) * 32)
        self.assertEqual(self.window._spectrum_levels, (0.5,) * 32)
        self.window.set_spectrum_available(False)
        self.assertEqual(self.window._spectrum_levels, (0.0,) * 32)

    def test_spectrum_accepts_worker_data_and_rejects_malformed_levels(self):
        import threading
        self.window.set_spectrum_available(True)
        worker = threading.Thread(target=self.window.set_spectrum, args=((0.25,) * 32,))
        worker.start()
        worker.join()
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        self.assertEqual(self.window._spectrum_levels, (0.25,) * 32)
        with self.assertRaises(ValueError):
            self.window.set_spectrum((0.2,))
        with self.assertRaises(ValueError):
            self.window.set_spectrum((float('nan'),) * 32)

    def test_spectrum_drawing_is_static_without_signal_and_changes_with_bands(self):
        import cairo
        def pixels():
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 160, 100)
            self.window._draw_spectrum(None, cairo.Context(surface), 160, 100)
            surface.flush()
            return bytes(surface.get_data())
        self.window.set_spectrum_available(True)
        neutral = pixels()
        self.assertEqual(neutral, pixels())
        self.window.set_spectrum(tuple((i + 1) / 32 for i in range(32)))
        active = pixels()
        self.assertNotEqual(neutral, active)
        self.assertEqual(active, pixels())

    def test_spectrum_updates_reuse_a_single_throttled_source(self):
        self.window.set_spectrum_available(True)
        self.window.set_spectrum((0.4,) * 32)
        source = self.window._spectrum_source_id
        self.assertNotEqual(source, 0)
        self.window.set_spectrum((0.8,) * 32)
        self.assertEqual(self.window._spectrum_source_id, source)
        self.window.set_spectrum_available(False)
        self.assertEqual(self.window._spectrum_source_id, 0)

    def test_visible_stack_switcher_changes_views(self):
        def switcher_button(title):
            for item in walk(self.window.view_switcher):
                if isinstance(item, Gtk.ToggleButton) and any(
                    isinstance(child, Gtk.Label) and child.get_label() == title for child in walk(item)
                ):
                    return item
            self.fail('View switcher button is missing')
        switcher_button('Сейчас играет').emit('clicked')
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'now')
        switcher_button('Медиатека').emit('clicked')
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'library')

    def test_keyboard_shortcuts_leave_text_editing_intact(self):
        self.window.view_stack.set_visible_child_name('now')
        self.window._keyboard.emit('key-pressed', Gdk.KEY_f, 0, Gdk.ModifierType.CONTROL_MASK)
        self.assertEqual(self.window.view_stack.get_visible_child_name(), 'library')
        self.window.set_focus(None)
        self.window._keyboard.emit('key-pressed', Gdk.KEY_Left, 0, Gdk.ModifierType.CONTROL_MASK)
        self.window._keyboard.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType.CONTROL_MASK)
        self.window._keyboard.emit('key-pressed', Gdk.KEY_space, 0, Gdk.ModifierType(0))
        self.assertEqual(self.calls, [('previous_track', ()), ('next_track', ()), ('toggle_playback', ())])
        self.window.set_focus(self.window.search_entry)
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_space, 0, Gdk.ModifierType(0)))
        self.assertFalse(self.window._keyboard.emit('key-pressed', Gdk.KEY_Left, 0, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(len(self.calls), 3)

    def test_visualizer_returns_to_neutral_without_new_audio(self):
        self.window.set_spectrum_available(True)
        self.window.set_spectrum((0.5,) * 32)
        loop = GLib.MainLoop()
        GLib.timeout_add(850, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertEqual(self.window._spectrum_levels, (0.0,) * 32)
        self.assertEqual(self.window._spectrum_source_id, 0)
        self.assertTrue(self.window.spectrum_hint.get_visible())

if __name__ == '__main__':
    unittest.main()
