import unittest
import time
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk
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
        last.allocate(400, 24, -1, None)
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
        names = ('login', 'logout', 'search', 'liked_tracks', 'playlists', 'open_playlist', 'play_track', 'toggle_playback', 'next_track', 'previous_track', 'seek', 'set_volume', 'set_liked', 'device_login', 'wave', 'set_equalizer', 'equalizer_preset', 'set_shuffle', 'set_repeat', 'load_more', 'set_quality')
        self.actions = {name: (lambda *args, name=name: self.calls.append((name, args))) for name in names}
        self.window = PlayerWindow(self.app, self.actions)

    def tearDown(self):
        if hasattr(self, 'window'):
            self.window.destroy()

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
        last.allocate(400, 24, -1, None)
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
                row.allocate(400, 24, -1, None)
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
        for widget, text in ((self.window.shuffle_button, '⇄'), (self.window.repeat_button, '↻'),
                             (self.window.favorite_button, '♥'), (self.window.wave_button, 'Моя волна')):
            backdrop(widget)
            self.assertLess(brightness(label(widget, text)), 0.45, text)
        backdrop(self.window.view_switcher)
        self.assertLess(brightness(label(self.window.view_switcher, 'Медиатека')), 0.45)
        self.window.shuffle_button.set_active(True)
        self.window.shuffle_button.set_state_flags(Gtk.StateFlags.BACKDROP | Gtk.StateFlags.FOCUSED, False)
        self.assertGreater(brightness(label(self.window.shuffle_button, '⇄')), 0.7)
        self.window.set_now_playing(None)
        backdrop(self.window.favorite_button)
        disabled = brightness(label(self.window.favorite_button, '♥'))
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
