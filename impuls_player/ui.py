"""GTK presentation for Impuls. This module never performs network requests."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, GLib, Gtk, Pango

from impuls_player.presets import EQUALIZER_PRESETS
from impuls_player.moments import MomentBook


def _format_time(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds or 0)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _track_url(track_id: object) -> str | None:
    """Build a service link only for numeric track identifiers."""
    if not isinstance(track_id, str):
        return None
    parts = track_id.split(":")
    if not 1 <= len(parts) <= 2 or any(not part.isascii() or not part.isdigit() for part in parts):
        return None
    return f"https://music.yandex.ru/track/{parts[0]}"


def _is_safe_login_url(url: str) -> bool:
    """Only offer device sign-in links served over HTTPS by Yandex."""
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if any(char in host for char in ("%", "\\", " ")):
            return False
        valid_host = any(host == domain or host.endswith("." + domain) for domain in ("ya.ru", "yandex.ru", "yandex.com"))
        return (parsed.scheme == "https" and valid_host and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443))
    except ValueError:
        return False


def _label(text: str = "", *classes: str, xalign: float = 0) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        widget.add_css_class(name)
    return widget



def _column_label(text: str, width: int, *classes: str, xalign: float = 0) -> Gtk.Label:
    """Fixed pixel slots keep headings and rows aligned across font sizes."""
    widget = _label(text, *classes, xalign=xalign)
    widget.set_ellipsize(Pango.EllipsizeMode.END)
    widget.set_width_chars(1)
    widget.set_max_width_chars(1)
    widget.set_size_request(width, -1)
    return widget

def _box(orientation: Gtk.Orientation, spacing: int = 0, *classes: str) -> Gtk.Box:
    widget = Gtk.Box(orientation=orientation, spacing=spacing)
    for name in classes:
        widget.add_css_class(name)
    return widget


def _icon_button(icon: str, tooltip: str, *classes: str) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon)
    button.set_tooltip_text(tooltip)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    for name in classes:
        button.add_css_class(name)
    return button


class PlayerWindow(Gtk.ApplicationWindow):
    """A passive view: controller actions are injected as callbacks."""

    def __init__(self, application: Gtk.Application, actions: dict[str, Callable[..., None]]):
        super().__init__(application=application, title="Impuls")
        self.actions = actions
        self._account: str | None = None
        self._current_track = None
        self._moments = MomentBook()
        self._track_rows: dict[Gtk.ListBoxRow, tuple[object, int]] = {}
        self._playlist_rows: dict[Gtk.ListBoxRow, object] = {}
        self._rows_by_track_id: dict[str, list[Gtk.ListBoxRow]] = {}
        self._highlighted_row = None
        self._selected_track_index: int | None = None
        self._render_queue = deque()
        self._render_source_id = 0
        self._render_generation = 0
        self._render_kind = "tracks"
        self._rendered_track_count = 0
        self._loaded_duration_ms = 0
        self._unknown_duration_count = 0
        self._retired_lists = deque()
        self._dispose_source_id = 0
        self._pagination_has_more = False
        self._pagination_loading = False
        self._pagination_error = False
        self._auto_more_empty_page = False
        self._auto_more_source_id = 0
        self._auto_more_layout_retries = 0
        self._destroyed = False
        self._compact_layout = None
        self._narrow_columns = False
        self._album_labels = []
        self._equalizer_user_choice = None
        self._auto_equalizer_change = False
        self._last_volume = 0.75
        self._position_ms = 0
        self._duration_ms = 0
        self._mini_mode = False
        self._full_window_size = (960, 800)
        self._full_window_maximized = False
        self._jump_tick_id = 0
        self._geometry_tick_id = 0
        self._tray_available = False
        self._visualization_mode = "ribbons"
        self._visualization_phase = 0.0
        self._login_window: Gtk.Window | None = None
        self._device_window: Gtk.Window | None = None
        self._error_window: Gtk.Window | None = None
        self._help_window: Gtk.Window | None = None
        self._ui_thread = threading.get_ident()
        self._spectrum_lock = threading.Lock()
        self._pending_spectrum = None
        self._spectrum_idle_pending = False
        self._spectrum_available = False
        self._spectrum_levels = (0.0,) * 32
        self._spectrum_last_update = 0.0
        self._spectrum_source_id = 0
        self._syncing_equalizer = False
        self._syncing_preset = False
        self.set_default_size(960, 800)
        self.set_size_request(480, 360)
        self._install_css()
        header = Gtk.HeaderBar()
        header.add_css_class("window-header")
        header.set_show_title_buttons(True)
        header.set_decoration_layout(":minimize,maximize,close")
        header.set_title_widget(_label("Impuls", "window-title", xalign=0.5))
        self.mini_button = Gtk.ToggleButton(icon_name="view-restore-symbolic")
        self.mini_button.add_css_class("mini-mode-toggle")
        self.mini_button.set_tooltip_text("Мини-плеер · Ctrl+Shift+M")
        self.mini_button.update_property([Gtk.AccessibleProperty.LABEL], ["Мини-плеер"])
        self.mini_button.connect("toggled", lambda button: self.set_mini_mode(button.get_active()))
        header.pack_start(self.mini_button)
        self.set_titlebar(header)
        self._build()
        self.connect("destroy", self._on_destroy)
        self.connect("close-request", self._on_close_request)

    def _install_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = provider

    def _call(self, name: str, *args):
        action = self.actions.get(name)
        if action is not None:
            return action(*args)
        return None

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        compact = width < 600 or height < 500
        narrow = width < 680
        if self._narrow_columns != narrow and hasattr(self, "column_album"):
            self._narrow_columns = narrow
            self.column_album.set_visible(not narrow)
            for label in self._album_labels:
                label.set_visible(not narrow)
        if self._compact_layout != compact and hasattr(self, "equalizer_toggle"):
            self._compact_layout = compact
            if self._equalizer_user_choice is None:
                self._auto_equalizer_change = True
                self.equalizer_toggle.set_active(False)
                self._auto_equalizer_change = False
            for caption in self._compact_captions:
                caption.set_visible(not compact)
            self.account_label.set_visible(not compact)
            self.deck_controls.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
            self.sound_controls.set_hexpand(not compact)
            self.sound_controls.set_halign(Gtk.Align.END if not compact else Gtk.Align.FILL)
            if compact:
                self._root_box.add_css_class("compact-layout")
            else:
                self._root_box.remove_css_class("compact-layout")
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)

    def _build(self) -> None:
        root = _box(Gtk.Orientation.VERTICAL, 5, "app-shell")
        self._root_box = root
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(root)
        self.set_child(scroller)
        self.view_stack = Gtk.Stack()
        self.view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.set_transition_duration(180)
        self.view_stack.set_vexpand(True)
        root.append(self._build_deck())
        self.equalizer_panel = self._build_equalizer()
        root.append(self.equalizer_panel)
        self.view_stack.add_titled(self._build_visualizer(), "now", "Сейчас играет")
        self.view_stack.add_titled(self._build_library(), "library", "Медиатека")
        self.view_stack.set_visible_child_name("library")
        root.append(self.view_stack)
        root.append(self._build_statusbar())
        keyboard = Gtk.EventControllerKey()
        keyboard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keyboard.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keyboard)
        self._keyboard = keyboard

    def normal_window_state(self) -> tuple[tuple[int, int], bool]:
        """Mini mode must never overwrite the user's normal window geometry."""
        if self._mini_mode or self._geometry_tick_id:
            return self._full_window_size, self._full_window_maximized
        return tuple(self.get_default_size()), self.is_maximized()

    def set_mini_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._mini_mode:
            return
        if enabled:
            self._full_window_size, self._full_window_maximized = self.normal_window_state()
        self._mini_mode = enabled
        self.mini_button.set_active(enabled)
        self.mini_button.set_tooltip_text("Полный плеер · Ctrl+Shift+M" if enabled else "Мини-плеер · Ctrl+Shift+M")
        self.mini_button.update_property([Gtk.AccessibleProperty.LABEL],
                                        ["Полный плеер" if enabled else "Мини-плеер"])
        self.view_stack.set_visible(not enabled)
        self.equalizer_panel.set_visible(not enabled)
        self.view_switcher.set_visible(not enabled)
        self.moments_row.set_visible(not enabled)
        if enabled:
            self.unmaximize()
            self.set_size_request(480, 240)
            self.set_default_size(480, 340)
        else:
            self.set_size_request(480, 360)
            self.set_default_size(*self._full_window_size)
            if self._full_window_maximized:
                self.maximize()
            else:
                self.unmaximize()
        self._settle_window_geometry((480, 340) if enabled else self._full_window_size,
                                     False if enabled else self._full_window_maximized)

    def _settle_window_geometry(self, size: tuple[int, int], maximized: bool) -> None:
        # Native unmaximize is asynchronous and may overwrite default_size with
        # the old restored size. Apply our target after that state has settled.
        if self._geometry_tick_id:
            self.remove_tick_callback(self._geometry_tick_id)
        attempts = stable = 0
        def settle(_widget, _clock):
            nonlocal attempts, stable
            attempts += 1
            if self._destroyed or attempts > 60:
                self._geometry_tick_id = 0
                return GLib.SOURCE_REMOVE
            if self.is_maximized() != maximized:
                stable = 0
                return GLib.SOURCE_CONTINUE
            if tuple(self.get_default_size()) != size:
                self.set_default_size(*size)
                stable = 0
                return GLib.SOURCE_CONTINUE
            if not maximized and (self.get_width(), self.get_height()) != size:
                stable = 0
                return GLib.SOURCE_CONTINUE
            stable += 1
            if stable < 2:
                return GLib.SOURCE_CONTINUE
            self._geometry_tick_id = 0
            return GLib.SOURCE_REMOVE
        self._geometry_tick_id = self.add_tick_callback(settle)

    def jump_to_current(self) -> None:
        """Focus the current occurrence in the loaded list without restarting it."""
        self.set_mini_mode(False)
        self.view_stack.set_visible_child_name("library")
        self._refresh_current_row()
        if self._jump_tick_id:
            self.remove_tick_callback(self._jump_tick_id)
            self._jump_tick_id = 0
        row = self._highlighted_row
        if row is None or self.stack.get_visible_child_name() != "tracks":
            self.set_status("Текущий трек отсутствует в открытом списке")
            return
        row.grab_focus()
        # ListBox focus alone does not scroll with selection mode NONE.
        # Wait for layout after restoring a hidden mini-player library.
        attempts = 0
        def scroll_to_row(_widget, _clock):
            nonlocal attempts
            attempts += 1
            if self._destroyed or row.get_parent() is not self.tracks_list or attempts > 30:
                self._jump_tick_id = 0
                return GLib.SOURCE_REMOVE
            if not row.get_mapped() or row.get_height() <= 0:
                return GLib.SOURCE_CONTINUE
            valid, bounds = row.compute_bounds(self.tracks_list)
            if valid:
                adjustment = self.track_scroll.get_vadjustment()
                if adjustment.get_page_size() <= 0 or adjustment.get_upper() < bounds.get_y() + bounds.get_height():
                    return GLib.SOURCE_CONTINUE
                target = min(bounds.get_y(), max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
                if abs(adjustment.get_value() - target) > 0.5:
                    adjustment.set_value(target)
                    # Focus/layout can reset the adjustment during this frame.
                    # Confirm the position on the following frame.
                    return GLib.SOURCE_CONTINUE
            self._jump_tick_id = 0
            return GLib.SOURCE_REMOVE
        self._jump_tick_id = self.add_tick_callback(scroll_to_row)

    def _panel_title(self, title: str) -> Gtk.Box:
        bar = _box(Gtk.Orientation.HORIZONTAL, 8, "panel-title")
        label = _label(title, "panel-title-text")
        label.set_hexpand(True)
        bar.append(label)
        return bar

    def _nav_button(self, label: str, action: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.add_css_class("nav-button")
        button.set_tooltip_text(label)
        button.connect("clicked", lambda _: self._navigate(action))
        return button

    def _navigate(self, action: str) -> None:
        self.view_stack.set_visible_child_name("library")
        self._set_active_navigation(action)
        self._call(action)

    def _set_active_navigation(self, action: str) -> None:
        for name, button in (("wave", self.wave_button), ("liked_tracks", self.liked_button), ("playlists", self.playlists_button)):
            if action == name:
                button.add_css_class("active-nav")
            else:
                button.remove_css_class("active-nav")

    def _build_deck(self) -> Gtk.Box:
        deck = _box(Gtk.Orientation.VERTICAL, 4, "chrome-panel", "deck")
        title = self._panel_title("IMPULS")
        title.get_first_child().set_hexpand(False)
        self.view_switcher = Gtk.StackSwitcher(stack=self.view_stack)
        self.view_switcher.add_css_class("view-switcher")
        self.view_switcher.set_hexpand(True)
        self.view_switcher.set_halign(Gtk.Align.CENTER)
        title.append(self.view_switcher)
        self.account_label = _label("Гость", "title-account")
        self.account_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.account_label.set_max_width_chars(12)
        title.append(self.account_label)
        self.account_button = Gtk.Button(label="Войти")
        self.account_button.add_css_class("title-button")
        self.account_button.connect("clicked", self._on_account_clicked)
        title.append(self.account_button)
        title.append(self._build_app_menu())
        deck.append(title)

        display = _box(Gtk.Orientation.HORIZONTAL, 12, "led-display")
        clock = _box(Gtk.Orientation.VERTICAL, 0, "clock-display")
        clock.set_valign(Gtk.Align.CENTER)
        clock_caption = _label("ВРЕМЯ", "display-caption")
        clock.append(clock_caption)
        self.position_label = _label("0:00", "display-time")
        self.position_label.update_property([Gtk.AccessibleProperty.LABEL], ["Текущая позиция"])
        self.position_label.set_width_chars(5)
        self.time_toggle = Gtk.ToggleButton()
        self.time_toggle.add_css_class("time-toggle")
        self.time_toggle.set_tooltip_text("Прошло / осталось — нажмите для переключения")
        self.time_toggle.update_property([Gtk.AccessibleProperty.LABEL], ["Показывать оставшееся время"])
        self.time_toggle.set_child(self.position_label)
        self.time_toggle.connect("toggled", lambda _: self._refresh_clock())
        clock.append(self.time_toggle)
        display.append(clock)
        display_details = _box(Gtk.Orientation.VERTICAL, 3, "display-details")
        display_details.set_hexpand(True)
        display_details.set_valign(Gtk.Align.CENTER)
        playing_caption = _label("ВОСПРОИЗВОДИТСЯ", "display-caption")
        display_details.append(playing_caption)
        self._compact_captions = (clock_caption, playing_caption)
        self.now_title = _label("Ничего не играет", "now-title")
        self.now_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.now_artist = _label("Выберите трек", "now-artist")
        self.now_artist.set_ellipsize(Pango.EllipsizeMode.END)
        self.now_artist.set_hexpand(True)
        display_details.append(self.now_title)
        display_meta = _box(Gtk.Orientation.HORIZONTAL, 8)
        display_meta.append(self.now_artist)
        self.stream_quality_label = _label("", "stream-quality", xalign=1)
        self.stream_quality_label.set_visible(False)
        display_meta.append(self.stream_quality_label)
        display_details.append(display_meta)
        display.append(display_details)
        self.duration_label = _label("0:00", "display-duration", xalign=1)
        self.duration_label.set_width_chars(5)
        self.duration_label.set_valign(Gtk.Align.END)
        display.append(self.duration_label)
        deck.append(display)

        seek_row = _box(Gtk.Orientation.HORIZONTAL, 8, "seek-row")
        seek_caption = _label("ПОЗИЦИЯ", "metal-caption")
        seek_row.append(seek_caption)
        self._compact_captions += (seek_caption,)
        self.seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self.seek_scale.set_draw_value(False)
        self.seek_scale.set_hexpand(True)
        self.seek_scale.set_sensitive(False)
        self.seek_scale.set_tooltip_text("Позиция воспроизведения")
        self.seek_scale.update_property([Gtk.AccessibleProperty.LABEL], ["Позиция воспроизведения"])
        self.seek_scale.connect("change-value", self._on_seek)
        seek_row.append(self.seek_scale)
        deck.append(seek_row)
        self.moments_row = _box(Gtk.Orientation.HORIZONTAL, 6, "moments-row")
        self.moments_row.append(_label("МОМЕНТЫ", "metal-caption"))
        self.moment_add_button = _icon_button("bookmark-new-symbolic", "Запомнить этот момент · Ctrl+B", "moment-button")
        self.moment_add_button.set_sensitive(False)
        self.moment_add_button.connect("clicked", lambda _: self._add_moment())
        self.moments_row.append(self.moment_add_button)
        self.moments_list = _box(Gtk.Orientation.HORIZONTAL, 4)
        self.moments_list.set_hexpand(True)
        self.moments_row.append(self.moments_list)
        self.moment_clear_button = _icon_button("edit-clear-symbolic", "Очистить моменты этого трека", "moment-button")
        self.moment_clear_button.connect("clicked", lambda _: self._clear_moments())
        self.moments_row.append(self.moment_clear_button)
        deck.append(self.moments_row)
        self._render_moments()

        controls = _box(Gtk.Orientation.HORIZONTAL, 8, "deck-controls")
        self.deck_controls = controls
        transport = _box(Gtk.Orientation.HORIZONTAL, 6, "transport-controls")
        # Natural heights keep the smaller transport buttons circular beside
        # the larger play button, instead of stretching them into ovals.
        transport.set_valign(Gtk.Align.CENTER)
        controls.append(transport)
        previous = _icon_button("media-skip-backward-symbolic", "Предыдущий трек", "transport-button")
        previous.set_valign(Gtk.Align.CENTER)
        previous.connect("clicked", lambda _: self._call("previous_track"))
        transport.append(previous)
        self.play_button = _icon_button("media-playback-start-symbolic", "Воспроизвести", "transport-button", "play-button")
        self.play_button.connect("clicked", lambda _: self._call("toggle_playback"))
        transport.append(self.play_button)
        next_button = _icon_button("media-skip-forward-symbolic", "Следующий трек", "transport-button")
        next_button.set_valign(Gtk.Align.CENTER)
        next_button.connect("clicked", lambda _: self._call("next_track"))
        transport.append(next_button)
        self.shuffle_button = Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic")
        self.shuffle_button.add_css_class("transport-toggle")
        self.shuffle_button.set_valign(Gtk.Align.CENTER)
        self.shuffle_button.set_tooltip_text("Случайный порядок обычной медиатеки. «Моя волна» сохраняет порядок рекомендаций.")
        self.shuffle_button.update_property([Gtk.AccessibleProperty.LABEL], ["Случайный порядок"])
        self._shuffle_handler = self.shuffle_button.connect("toggled", lambda button: self._call("set_shuffle", button.get_active()))
        transport.append(self.shuffle_button)
        self.repeat_button = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic")
        self.repeat_button.add_css_class("transport-toggle")
        self.repeat_button.set_valign(Gtk.Align.CENTER)
        self.repeat_button.set_tooltip_text("Повтор обычной очереди. «Моя волна» автоматически загружает следующую партию.")
        self.repeat_button.update_property([Gtk.AccessibleProperty.LABEL], ["Повтор очереди"])
        self._repeat_handler = self.repeat_button.connect("toggled", lambda button: self._call("set_repeat", button.get_active()))
        transport.append(self.repeat_button)
        self.favorite_button = Gtk.MenuButton(icon_name="emblem-favorite-symbolic")
        self.favorite_button.set_always_show_arrow(False)
        self.favorite_button.set_tooltip_text("Действия с любимыми треками")
        self.favorite_button.update_property([Gtk.AccessibleProperty.LABEL], ["Действия с любимыми треками"])
        self.favorite_button.add_css_class("favorite-button")
        self.favorite_button.set_valign(Gtk.Align.CENTER)
        self.favorite_button.set_sensitive(False)
        favorite_popover = Gtk.Popover()
        favorite_actions = _box(Gtk.Orientation.VERTICAL, 3, "popover-actions")
        add_favorite = Gtk.Button(label="Добавить в любимое")
        add_favorite.connect("clicked", lambda _: self._favorite(True, favorite_popover))
        remove_favorite = Gtk.Button(label="Удалить из любимого")
        remove_favorite.connect("clicked", lambda _: self._favorite(False, favorite_popover))
        favorite_actions.append(add_favorite)
        favorite_actions.append(remove_favorite)
        favorite_popover.set_child(favorite_actions)
        self.favorite_button.set_popover(favorite_popover)
        transport.append(self.favorite_button)
        sound_controls = _box(Gtk.Orientation.HORIZONTAL, 8, "sound-controls")
        self.sound_controls = sound_controls
        sound_controls.set_hexpand(True)
        sound_controls.set_halign(Gtk.Align.END)
        controls.append(sound_controls)
        self.mute_button = _icon_button("audio-volume-high-symbolic", "Выключить звук", "mute-button")
        self.mute_button.set_valign(Gtk.Align.CENTER)
        self.mute_button.connect("clicked", self._toggle_mute)
        sound_controls.append(self.mute_button)
        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.01)
        self.volume_scale.set_size_request(80, -1)
        self.volume_scale.set_hexpand(True)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_value(0.75)
        self.volume_scale.set_tooltip_text("Громкость")
        self.volume_scale.update_property([Gtk.AccessibleProperty.LABEL], ["Громкость"])
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        sound_controls.append(self.volume_scale)

        self.quality_selector = Gtk.DropDown.new_from_strings(["NQ", "HQ", "Lossless"])
        self.quality_selector.add_css_class("quality-selector")
        self.quality_selector.set_valign(Gtk.Align.CENTER)
        self.quality_selector.set_tooltip_text("Качество следующих треков; текущий трек продолжит играть без изменений")
        self.quality_selector.update_property([Gtk.AccessibleProperty.LABEL], ["Качество следующих треков"])
        choices = ("Экономичное", "Высокое", "Lossless")
        factory = Gtk.SignalListItemFactory()
        def setup_quality_row(_factory, item):
            row = _box(Gtk.Orientation.HORIZONTAL, 10)
            label = Gtk.Label(xalign=0, hexpand=True)
            row.append(label)
            row.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            item.set_child(row)
        def bind_quality_row(_factory, item):
            row = item.get_child()
            row.get_first_child().set_text(choices[item.get_position()])
            check = row.get_last_child()
            def update_check(*_args):
                check.set_opacity(1.0 if item.get_position() == self.quality_selector.get_selected() else 0.0)
            update_check()
            item._selection_handler = self.quality_selector.connect("notify::selected", update_check)
        def unbind_quality_row(_factory, item):
            self.quality_selector.disconnect(item._selection_handler)
        factory.connect("setup", setup_quality_row)
        factory.connect("bind", bind_quality_row)
        factory.connect("unbind", unbind_quality_row)
        self.quality_selector.set_list_factory(factory)
        self.quality_selector.set_selected(1)
        self.quality_selector.connect("notify::selected", self._on_quality_selected)
        sound_controls.append(self.quality_selector)
        deck.append(controls)
        return deck

    def _on_quality_selected(self, selector: Gtk.DropDown, _param) -> None:
        selected = selector.get_selected()
        choices = ("nq", "hq", "lossless")
        if selected < len(choices):
            self._call("set_quality", choices[selected])

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        value = scale.get_value()
        if value > 0:
            self._last_volume = value
        icon = "audio-volume-muted-symbolic" if value == 0 else ("audio-volume-low-symbolic" if value < 0.5 else "audio-volume-high-symbolic")
        self.mute_button.set_icon_name(icon)
        description = "Включить звук" if value == 0 else "Выключить звук"
        self.mute_button.set_tooltip_text(description)
        self.mute_button.update_property([Gtk.AccessibleProperty.LABEL], [description])
        self._call("set_volume", value)

    def _toggle_mute(self, _button=None) -> None:
        self.volume_scale.set_value(self._last_volume if self.volume_scale.get_value() == 0 else 0.0)

    def _moment_track_id(self) -> str:
        return str(self._current_track.id) if self._current_track is not None else ""

    def _add_moment(self) -> None:
        result = self._moments.add(self._moment_track_id(), self._position_ms, self._duration_ms)
        messages = {"added": "Момент сохранён до закрытия плеера", "duplicate": "Этот момент уже отмечен",
                    "full": "У трека уже 5 моментов. Очистите их, чтобы добавить новые",
                    "unavailable": "Момент можно отметить после загрузки трека"}
        self.set_status(messages[result])
        self._render_moments()

    def _clear_moments(self) -> None:
        self._moments.clear(self._moment_track_id())
        self._render_moments()

    def _render_moments(self) -> None:
        child = self.moments_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.moments_list.remove(child)
            child = following
        track_id = self._moment_track_id()
        positions = self._moments.positions(track_id)
        self.moment_clear_button.set_sensitive(bool(positions))
        if not positions:
            hint = _label("Любимый фрагмент — в одно нажатие", "moment-hint")
            hint.set_ellipsize(Pango.EllipsizeMode.END)
            hint.set_width_chars(1)
            hint.set_hexpand(True)
            self.moments_list.append(hint)
        for position in positions:
            marker = Gtk.Button(label=_format_time(position))
            marker.add_css_class("moment-button")
            marker.set_tooltip_text(f"Перейти к {_format_time(position)} · отметка текущего сеанса")
            marker.connect("clicked", lambda _, key=track_id, point=position: self._jump_moment(key, point))
            self.moments_list.append(marker)

    def _jump_moment(self, track_id: str, position: int) -> None:
        if track_id == self._moment_track_id() and self._duration_ms > 0:
            self._seek_to(min(1.0, position / self._duration_ms))

    def _build_app_menu(self) -> Gtk.MenuButton:
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu.add_css_class("app-menu")
        menu.set_tooltip_text("Меню приложения")
        menu.update_property([Gtk.AccessibleProperty.LABEL], ["Меню приложения"])
        popover = Gtk.Popover()
        body = _box(Gtk.Orientation.VERTICAL, 5, "popover-actions")
        guide_button = Gtk.Button(label="Справка и о программе")
        guide_button.connect("clicked", lambda _: (popover.popdown(), self._show_help()))
        body.append(guide_button)
        help_button = Gtk.Button(label="Горячие клавиши")
        help_button.connect("clicked", lambda _: (popover.popdown(), self._show_shortcuts()))
        body.append(help_button)
        mini = Gtk.Button(label="Мини-плеер / полный вид · Ctrl+Shift+M")
        mini.connect("clicked", lambda _: (popover.popdown(), self.set_mini_mode(not self._mini_mode)))
        body.append(mini)
        current = Gtk.Button(label="К играющему треку · Ctrl+J")
        current.connect("clicked", lambda _: (popover.popdown(), self.jump_to_current()))
        body.append(current)
        self.hide_button = Gtk.Button(label="Свернуть в трей")
        self.hide_button.set_sensitive(False)
        self.hide_button.connect("clicked", lambda _: (popover.popdown(), self._call("hide_window")))
        body.append(self.hide_button)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.sleep_timer_menu_label = _label("Таймер сна", "popover-caption")
        body.append(self.sleep_timer_menu_label)
        timer_choices = _box(Gtk.Orientation.HORIZONTAL, 5)
        timer_choices.set_homogeneous(True)
        for minutes in (15, 30, 60):
            choice = Gtk.Button(label=f"{minutes} мин")
            choice.connect("clicked", lambda _button, value=minutes:
                           (popover.popdown(), self._call("set_sleep_timer", value)))
            timer_choices.append(choice)
        body.append(timer_choices)
        self.cancel_sleep_button = Gtk.Button(label="Отменить таймер")
        self.cancel_sleep_button.set_sensitive(False)
        self.cancel_sleep_button.connect("clicked", lambda _:
                                         (popover.popdown(), self._call("set_sleep_timer", 0)))
        body.append(self.cancel_sleep_button)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        quit_button = Gtk.Button(label="Выйти из Impuls · Ctrl+Q")
        quit_button.connect("clicked", lambda _: self._call("quit"))
        body.append(quit_button)
        popover.set_child(body)
        menu.set_popover(popover)
        return menu

    def set_tray_available(self, available: bool) -> None:
        self._tray_available = bool(available)
        self.hide_button.set_sensitive(self._tray_available)
        self.hide_button.set_tooltip_text("Скрыть окно, продолжая воспроизведение" if available else "Системный трей недоступен")

    def _show_help(self) -> None:
        """Local, searchable guidance; external links open only on activation."""
        if self._help_window is not None:
            self._help_window.present()
            return
        from impuls_player import __version__
        window = Gtk.Window(title="Справка · Impuls", transient_for=self, modal=False,
                            destroy_with_parent=True, default_width=560, default_height=620)
        self._help_window = window
        window.connect("close-request", self._on_help_closed)
        header = Gtk.HeaderBar()
        header.add_css_class("window-header")
        header.set_decoration_layout(":close")
        header.set_title_widget(_label("Справка · Impuls", "window-title", xalign=0.5))
        window.set_titlebar(header)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = _box(Gtk.Orientation.VERTICAL, 12, "dialog-content", "help-content")
        scroller.set_child(content)
        window.set_child(scroller)
        content.append(_label("Справка", "dialog-title"))
        intro = _label("Всё необходимое для прослушивания музыки. Справка доступна без интернета.", "dialog-copy")
        intro.set_wrap(True)
        intro.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        content.append(intro)
        search = Gtk.SearchEntry(placeholder_text="Поиск по справке")
        search.add_css_class("help-search")
        content.append(search)
        topics = (
            ("Моменты и итоги списка", "Кнопка закладки под перемоткой или Ctrl+B отмечает любимый фрагмент. Нажмите на время отметки, чтобы вернуться к нему. До пяти отметок на трек; кнопка очистки удаляет отметки только текущей песни. Отметки действуют до закрытия плеера и хранятся для 32 треков; при добавлении 33-го освобождаются отметки самого давнего. В мини-плеере полоса скрыта, Ctrl+B продолжает работать.\n\nСправа от кнопок списка показаны число загруженных треков и общая длительность. Повторы считаются отдельно. Знак + означает, что длительность части треков неизвестна. Итог относится к загруженным страницам, а не ко всей коллекции."),
            ("Мини-плеер и счётчик времени", "Кнопка слева в заголовке окна, меню приложения или Ctrl+Shift+M включают мини-плеер. Музыка продолжает играть; полный вид возвращается с прежним размером и состоянием эквалайзера. При новом запуске открывается полный вид.\n\nНажмите на крупные цифры времени, чтобы переключить прошедшее и оставшееся время. Минус означает остаток; −−:−− — неизвестную длительность. Выбор сохраняется.\n\nCtrl+J или «К играющему треку» в меню прокручивает открытый список к текущему треку, если он уже загружен. Ctrl+F возвращает полный вид и поиск."),
            ("Начало работы и медиатека", "Нажмите «Войти», затем откройте «Мою волну», любимые треки или плейлисты. Для поиска введите название или исполнителя и нажмите Enter. Щелчок по строке или кнопка ▶ запускает трек.\n\n«Слушать всё» запускает текущий список с начала; «Перемешать» меняет его порядок. Следующие страницы загружаются при прокрутке. Меню ⋮ у трека копирует название или ссылку и открывает страницу Яндекс Музыки. Сердце рядом с кнопками проигрывания добавляет текущий трек в любимое или удаляет его."),
            ("Вход и конфиденциальность", "Выберите «Войти по коду» и подтвердите вход на предложенной странице Яндекса. Пароль вводится только на странице Яндекса. Существующий токен можно ввести вручную.\n\n«Запомнить вход» сохраняет токен в системном Secret Service (например, GNOME Keyring или совместимом KWallet). Если хранилище недоступно, можно войти только на текущий сеанс. «Выйти» рядом с аккаунтом удаляет сохранённый токен, но не отзывает его у Яндекса.\n\nВ файле настроек нет токенов. У Impuls нет своего сервера, рекламы и встроенной аналитики; музыка и данные аккаунта запрашиваются у Яндекса. Не передавайте токен в сообщениях об ошибках."),
            ("Звук, качество и эквалайзер", "NQ — экономичное, HQ — высокое, Lossless — запрос звука без потерь. Выбор действует со следующего трека. Формат рядом с исполнителем показывает фактически полученный поток: сервис может вернуть AAC или MP3 вместо FLAC. Доступность зависит от трека, аккаунта и подписки.\n\nКнопка динамика отключает звук и возвращает прежнюю громкость. Кнопка + раскрывает десятиполосный эквалайзер: выберите пресет или настройте полосы вручную. «Сброс» возвращает их к 0 дБ. Эквалайзер и визуализации требуют gstreamer1-plugins-good.\n\nПовтор и случайный порядок действуют в обычной медиатеке. «Моя волна» сохраняет порядок рекомендаций и сама запрашивает следующую партию."),
            ("Настройки и визуализация", "Громкость, качество, полосы и видимость эквалайзера, случайный порядок, повтор, выбранная визуализация и размер окна сохраняются автоматически. Файл: ~/.config/impuls/settings.json, либо каталог XDG_CONFIG_HOME/impuls.\n\nВо вкладке «Сейчас играет» выберите «Ленты», «Спектр», «Зеркало» или «Орбита». Визуализация использует реальный звук и останавливается в тишине. Системное отключение анимаций учитывается переходами интерфейса."),
            ("Трей и таймер сна", "При доступном системном трее закрытие окна скрывает плеер, сохраняя воспроизведение. Вернуть его можно через значок Impuls. Без трея закрытие завершает приложение. «Выйти из Impuls» в меню и Ctrl+Q всегда завершают плеер; «Выйти» рядом с аккаунтом означает выход из аккаунта.\n\nВ меню ⋮ можно установить таймер сна на 15, 30 или 60 минут. Оставшееся время видно внизу окна; «Отменить таймер» отключает его. По окончании музыка останавливается. Таймер действует только в текущем сеансе."),
            ("Горячие клавиши", "Пробел — воспроизведение / пауза.\nCtrl + ← / → — предыдущий / следующий трек.\nAlt + ← / → — назад / вперёд на 5 секунд.\nCtrl + ↑ / ↓ — громкость ±5%.\nCtrl + M — выключить / включить звук.\nCtrl+Shift+M — мини-плеер / полный вид.\nCtrl + J — к играющему треку.\nCtrl + B — отметить момент.\nCtrl + F — поиск музыки.\nCtrl + R или F5 — обновить текущий список.\nCtrl + Q — выйти из Impuls.\nF1 — открыть эту справку.\n\nКлавиши воспроизведения не мешают редактированию текста. Системные медиаклавиши работают через MPRIS."),
            ("Если что-то не работает", "Нет звука: проверьте громкость плеера, выбранное аудиоустройство и системный микшер. Если трек недоступен, проверьте соединение и доступ к нему в Яндекс Музыке. Обновите список кнопкой ↻ или Ctrl+R.\n\nНет эквалайзера или визуализации: установите пакет gstreamer1-plugins-good и перезапустите плеер. Нет трея: нужен совместимый StatusNotifierHost; в GNOME может потребоваться расширение AppIndicator.\n\nНе сохраняется вход: проверьте, что системное хранилище секретов запущено и разблокировано. Настройки не сохраняются: проверьте права записи в каталог конфигурации.\n\nЕсли ошибка повторяется, откройте раздел Issues по ссылке ниже. Укажите версию Impuls, систему, шаги воспроизведения и текст ошибки без токенов и личных данных."),
        )
        sections = []
        for index, (title, body) in enumerate(topics):
            section = Gtk.Expander(label=title, expanded=index == 0)
            section.add_css_class("help-topic")
            copy = _label(body, "dialog-copy")
            copy.set_wrap(True)
            copy.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            copy.set_selectable(True)
            copy.set_margin_top(10)
            section.set_child(copy)
            content.append(section)
            sections.append((section, (title + " " + body).casefold()))
        empty = _label("Ничего не найдено. Попробуйте другое слово.", "dialog-copy")
        empty.set_wrap(True)
        empty.set_visible(False)
        content.append(empty)
        def filter_topics(entry):
            query = entry.get_text().strip().casefold()
            found = False
            for section, searchable in sections:
                matches = not query or query in searchable
                section.set_visible(matches)
                if query and matches:
                    section.set_expanded(True)
                found = found or matches
            empty.set_visible(not found)
        search.connect("search-changed", filter_topics)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        content.append(_label("О программе", "dialog-subtitle"))
        content.append(_label(f"Impuls {__version__}", "dialog-title"))
        about = _label("Неофициальный плеер Яндекс Музыки для Linux.\n© 2026 AnoY606-git и участники Impuls.\nСобственный код: GPL-3.0-only.\n\nyandex-music 3.2.2: LGPL-3.0-only. GTK и GStreamer распространяются на условиях их авторов. Impuls — независимый проект, не связанный с Яндексом.", "dialog-copy")
        about.set_wrap(True)
        about.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        about.set_selectable(True)
        content.append(about)
        for title, url in (("Исходный код на GitHub", "https://github.com/AnoY606-git/Impuls"),
                           ("Сообщить об ошибке / предложить улучшение", "https://github.com/AnoY606-git/Impuls/issues"),
                           ("Скачать обновления", "https://github.com/AnoY606-git/Impuls/releases"),
                           ("Лицензия GPL v3", "https://github.com/AnoY606-git/Impuls/blob/main/LICENSE")):
            link = Gtk.LinkButton.new_with_label(url, title)
            link.get_child().set_wrap(True)
            link.get_child().set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            link.set_tooltip_text(url)
            content.append(link)
        close = Gtk.Button(label="Закрыть")
        close.connect("clicked", lambda _: self._close_help_window())
        content.append(close)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _controller, key, _code, _state:
                     (self._close_help_window(), True)[1] if key == Gdk.KEY_Escape else False)
        window.add_controller(keys)
        window.present()

    def _on_help_closed(self, _window) -> bool:
        self._help_window = None
        return False

    def _close_help_window(self) -> None:
        if self._help_window is not None:
            window, self._help_window = self._help_window, None
            window.destroy()

    def _show_shortcuts(self) -> None:
        window, content = self._modal("Горячие клавиши")
        content.append(_label("Управляйте музыкой с клавиатуры", "dialog-title"))
        for keys, description in (("Пробел", "Воспроизведение / пауза"), ("Ctrl + ← / →", "Предыдущий / следующий трек"), ("Alt + ← / →", "Назад / вперёд на 5 секунд"), ("Ctrl + ↑ / ↓", "Громкость ±5%"), ("Ctrl + F", "Поиск музыки"), ("Ctrl + R / F5", "Обновить текущий список"), ("Ctrl + M", "Выключить / включить звук"), ("Ctrl+Shift+M", "Мини-плеер / полный вид"), ("Ctrl + J", "К играющему треку"), ("Ctrl + B", "Отметить момент"), ("Ctrl + Q", "Выйти из Impuls"), ("F1", "Справка и о программе")):
            line = _box(Gtk.Orientation.HORIZONTAL, 20)
            key = _label(keys, "shortcut-key")
            key.set_width_chars(15)
            line.append(key)
            line.append(_label(description, "dialog-copy"))
            content.append(line)
        close = Gtk.Button(label="Закрыть")
        close.connect("clicked", lambda _: window.destroy())
        content.append(close)
        window.present()

    def _build_equalizer(self) -> Gtk.Box:
        equalizer = _box(Gtk.Orientation.VERTICAL, 2, "chrome-panel", "equalizer")
        title = self._panel_title("ЭКВАЛАЙЗЕР")
        title.get_first_child().set_hexpand(False)
        self.equalizer_hint = _label("", "title-account")
        self.equalizer_hint.set_hexpand(True)
        self.equalizer_hint.set_ellipsize(Pango.EllipsizeMode.END)
        title.append(self.equalizer_hint)
        self.equalizer_toggle = Gtk.ToggleButton(label="+")
        self.equalizer_toggle.add_css_class("title-button")
        self.equalizer_toggle.add_css_class("equalizer-toggle")
        self.equalizer_toggle.set_active(False)
        self.equalizer_toggle.set_tooltip_text("Развернуть эквалайзер")
        self.equalizer_toggle.update_property([Gtk.AccessibleProperty.LABEL], ["Развернуть эквалайзер"])
        self.equalizer_toggle.connect("toggled", self._on_equalizer_toggled)
        title.append(self.equalizer_toggle)
        self._preset_names = list(EQUALIZER_PRESETS)
        self.preset_selector = Gtk.DropDown.new_from_strings(self._preset_names + ["Пользовательский"])
        self.preset_selector.add_css_class("preset-selector")
        self.preset_selector.set_size_request(138, -1)
        self.preset_selector.set_tooltip_text("Пресет эквалайзера")
        self.preset_selector.update_property([Gtk.AccessibleProperty.LABEL], ["Пресет эквалайзера"])
        self.preset_selector.set_selected(self._preset_names.index("Ровный"))
        self.preset_selector.connect("notify::selected", self._on_preset_selected)
        title.append(self.preset_selector)
        self.equalizer_reset = Gtk.Button(label="Сброс")
        self.equalizer_reset.add_css_class("title-button")
        self.equalizer_reset.set_tooltip_text("Вернуть все полосы эквалайзера к 0 дБ")
        self.equalizer_reset.connect("clicked", self._reset_equalizer)
        title.append(self.equalizer_reset)
        equalizer.append(title)

        eq_body = _box(Gtk.Orientation.HORIZONTAL, 6, "equalizer-body")
        self.equalizer_body = eq_body
        eq_body.set_visible(False)
        gauge = _box(Gtk.Orientation.VERTICAL, 0, "gain-gauge")
        gauge.append(_label("+12", "gain-label", xalign=1))
        upper_gap = Gtk.Box()
        upper_gap.set_size_request(-1, 11)
        gauge.append(upper_gap)
        gauge.append(_label("0 дБ", "gain-label", xalign=1))
        lower_gap = Gtk.Box()
        lower_gap.set_size_request(-1, 11)
        gauge.append(lower_gap)
        gauge.append(_label("−12", "gain-label", xalign=1))
        gauge.append(_label("", "frequency-label"))
        eq_body.append(gauge)
        bands = _box(Gtk.Orientation.HORIZONTAL, 2, "bands")
        bands.set_hexpand(True)
        bands.set_homogeneous(True)
        self.equalizer_sliders = []
        for index, frequency in enumerate(("31", "62", "125", "250", "500", "1K", "2K", "4K", "8K", "16K")):
            column = _box(Gtk.Orientation.VERTICAL, 1, "band")
            slider = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -12.0, 12.0, 0.5)
            slider.set_size_request(22, 58)
            slider.set_vexpand(False)
            slider.set_inverted(True)
            slider.set_draw_value(False)
            slider.set_value(0.0)
            slider.set_halign(Gtk.Align.CENTER)
            description = f"Полоса эквалайзера {frequency} Гц, от минус 12 до плюс 12 дБ"
            slider.set_tooltip_text(description)
            slider.update_property([Gtk.AccessibleProperty.LABEL], [description])
            slider.connect("value-changed", self._on_equalizer_band_changed, index)
            self.equalizer_sliders.append(slider)
            column.append(slider)
            column.append(_label(frequency, "frequency-label", xalign=0.5))
            bands.append(column)
        eq_body.append(bands)
        self.equalizer_revealer = Gtk.Revealer()
        self.equalizer_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.equalizer_revealer.set_transition_duration(180)
        self.equalizer_revealer.set_child(eq_body)
        self.equalizer_revealer.connect("notify::child-revealed", self._on_equalizer_revealed)
        equalizer.append(self.equalizer_revealer)
        self.set_equalizer_available(False)
        return equalizer

    def set_equalizer_expanded(self, expanded: bool) -> None:
        """Restore an explicit choice so responsive layout never overrides it."""
        self._equalizer_user_choice = bool(expanded)
        self.equalizer_toggle.set_active(bool(expanded))

    def _on_equalizer_toggled(self, toggle: Gtk.ToggleButton) -> None:
        expanded = toggle.get_active()
        if not self._auto_equalizer_change:
            self._equalizer_user_choice = expanded
        if expanded:
            self.equalizer_body.set_visible(True)
        self.equalizer_revealer.set_reveal_child(expanded)
        if not expanded and not self.equalizer_revealer.get_child_revealed():
            self.equalizer_body.set_visible(False)
        label = "Свернуть эквалайзер" if expanded else "Развернуть эквалайзер"
        toggle.set_label("−" if expanded else "+")
        toggle.set_tooltip_text(label)
        toggle.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def _on_equalizer_revealed(self, revealer: Gtk.Revealer, _param) -> None:
        if not revealer.get_reveal_child() and not revealer.get_child_revealed():
            self.equalizer_body.set_visible(False)

    def _reset_equalizer(self, _button: Gtk.Button) -> None:
        for slider in self.equalizer_sliders:
            slider.set_value(0.0)

    def _on_equalizer_band_changed(self, scale: Gtk.Scale, index: int) -> None:
        if self._syncing_equalizer:
            return
        self._call("set_equalizer", index, scale.get_value())
        self._sync_selected_preset()

    def _on_preset_selected(self, selector: Gtk.DropDown, _parameter) -> None:
        if self._syncing_preset:
            return
        index = selector.get_selected()
        if index < len(self._preset_names):
            self._call("equalizer_preset", self._preset_names[index])

    def _sync_selected_preset(self) -> None:
        values = tuple(slider.get_value() for slider in self.equalizer_sliders)
        selected = len(self._preset_names)
        for index, name in enumerate(self._preset_names):
            if all(abs(actual - expected) < 1e-6 for actual, expected in zip(values, EQUALIZER_PRESETS[name], strict=True)):
                selected = index
                break
        self._syncing_preset = True
        try:
            self.preset_selector.set_selected(selected)
        finally:
            self._syncing_preset = False

    def set_equalizer_values(self, values) -> None:
        """Reflect ten backend gains without feeding changes back to it."""
        try:
            gains = tuple(float(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise ValueError("Ожидаются десять значений эквалайзера") from exc
        if len(gains) != 10 or any(not math.isfinite(gain) or not -12 <= gain <= 12 for gain in gains):
            raise ValueError("Ожидаются десять значений эквалайзера от −12 до +12 дБ")
        self._syncing_equalizer = True
        try:
            for slider, gain in zip(self.equalizer_sliders, gains, strict=True):
                slider.set_value(gain)
        finally:
            self._syncing_equalizer = False
        self._sync_selected_preset()

    def set_equalizer_available(self, available: bool) -> None:
        message = "" if available else "Эквалайзер недоступен: установите gstreamer1-plugins-good"
        self.equalizer_hint.set_label(message)
        self.equalizer_hint.set_tooltip_text(message or None)
        self.equalizer_reset.set_sensitive(available)
        self.preset_selector.set_sensitive(available)
        for slider in self.equalizer_sliders:
            slider.set_sensitive(available)
            if not available:
                slider.set_tooltip_text(message)
            else:
                slider.set_tooltip_text("Изменить усиление полосы эквалайзера")

    def _build_visualizer(self) -> Gtk.Box:
        panel = _box(Gtk.Orientation.VERTICAL, 3, "chrome-panel", "visualizer-panel")
        panel.set_vexpand(True)
        title = self._panel_title("СЕЙЧАС ИГРАЕТ")
        self.visual_track_title = _label("", "title-account")
        self.visual_track_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.visual_track_title.set_max_width_chars(50)
        title.append(self.visual_track_title)
        from impuls_player.visualizations import VISUALIZATION_MODES
        self.visual_track_title.set_hexpand(True)
        self.visualization_selector = Gtk.DropDown.new_from_strings([title for _, title in VISUALIZATION_MODES])
        self.visualization_selector.add_css_class("preset-selector")
        self.visualization_selector.set_tooltip_text("Стиль визуализации")
        self.visualization_selector.update_property([Gtk.AccessibleProperty.LABEL], ["Стиль визуализации"])
        self.visualization_selector.connect("notify::selected", self._on_visualization_selected)
        title.append(self.visualization_selector)
        panel.append(title)
        overlay = Gtk.Overlay()
        overlay.add_css_class("visualizer-frame")
        overlay.set_vexpand(True)
        self.visual_area = Gtk.DrawingArea()
        self.visual_area.set_content_width(300)
        self.visual_area.set_content_height(120)
        self.visual_area.set_hexpand(True)
        self.visual_area.set_vexpand(True)
        self.visual_area.set_draw_func(self._draw_spectrum)
        overlay.set_child(self.visual_area)
        self.spectrum_hint = _label("", "spectrum-hint", xalign=0.5)
        self.spectrum_hint.set_halign(Gtk.Align.CENTER)
        self.spectrum_hint.set_valign(Gtk.Align.CENTER)
        self.spectrum_hint.set_wrap(True)
        self.spectrum_hint.set_can_target(False)
        overlay.add_overlay(self.spectrum_hint)
        panel.append(overlay)
        self.set_spectrum_available(False)
        return panel

    def _on_visualization_selected(self, selector, _param) -> None:
        from impuls_player.visualizations import VISUALIZATION_MODES
        selected = selector.get_selected()
        if selected < len(VISUALIZATION_MODES):
            self._visualization_mode = VISUALIZATION_MODES[selected][0]
            self.visual_area.queue_draw()

    def set_spectrum_available(self, available: bool) -> None:
        """Set plugin availability from the GTK/GLib main thread."""
        self._spectrum_available = bool(available)
        if not available:
            self._spectrum_levels = (0.0,) * 32
            if self._spectrum_source_id:
                GLib.source_remove(self._spectrum_source_id)
                self._spectrum_source_id = 0
            self.spectrum_hint.set_label("Визуализация недоступна: установите gstreamer1-plugins-good")
            self.spectrum_hint.set_visible(True)
            self.visual_area.queue_draw()
        else:
            self.spectrum_hint.set_label("Ожидание аудиосигнала")
            self.spectrum_hint.set_visible(not any(self._spectrum_levels))

    def set_spectrum(self, levels: tuple[float, ...]) -> None:
        """Accept 32 normalized spectrum bands, safely coalescing worker updates.

        Prefer connecting Gst.Bus.add_signal_watch() on the GTK/GLib main loop;
        if a producer calls this from another thread, only its latest pending
        snapshot is marshalled to that loop. Redraws are capped at 20 FPS.
        """
        try:
            values = tuple(float(value) for value in levels)
        except (TypeError, ValueError) as exc:
            raise ValueError("Ожидаются 32 уровня спектра") from exc
        if len(values) != 32 or any(not math.isfinite(value) for value in values):
            raise ValueError("Ожидаются 32 конечных уровня спектра")
        values = tuple(max(0.0, min(1.0, value)) for value in values)
        if threading.get_ident() != self._ui_thread:
            with self._spectrum_lock:
                self._pending_spectrum = values
                if self._spectrum_idle_pending:
                    return
                self._spectrum_idle_pending = True
            GLib.idle_add(self._drain_pending_spectrum)
            return
        self._apply_spectrum(values)

    def _drain_pending_spectrum(self) -> bool:
        with self._spectrum_lock:
            values, self._pending_spectrum = self._pending_spectrum, None
            self._spectrum_idle_pending = False
        if values is not None and not self._destroyed:
            self._apply_spectrum(values)
        return GLib.SOURCE_REMOVE

    def _apply_spectrum(self, values: tuple[float, ...]) -> None:
        if self._destroyed or not self._spectrum_available:
            return
        was_active = any(self._spectrum_levels)
        self._spectrum_levels = values
        self._spectrum_last_update = time.monotonic()
        active = any(values)
        self.spectrum_hint.set_visible(not active)
        if not active:
            if self._spectrum_source_id:
                GLib.source_remove(self._spectrum_source_id)
                self._spectrum_source_id = 0
            if was_active:
                self.visual_area.queue_draw()
        elif not self._spectrum_source_id:
            self._spectrum_source_id = GLib.timeout_add(50, self._redraw_spectrum)

    def _redraw_spectrum(self) -> bool:
        if time.monotonic() - self._spectrum_last_update > 0.7:
            self._spectrum_levels = (0.0,) * 32
            self.spectrum_hint.set_visible(True)
            self.visual_area.queue_draw()
            self._spectrum_source_id = 0
            return GLib.SOURCE_REMOVE
        if self.visual_area.get_mapped():
            self._visualization_phase += 0.035
            self.visual_area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw_spectrum(self, _area: Gtk.DrawingArea, context: cairo.Context, width: int, height: int) -> None:
        from impuls_player.visualizations import draw_visualization
        draw_visualization(context, width, height, self._spectrum_levels,
                           mode=self._visualization_mode, phase=self._visualization_phase)

    def _build_library(self) -> Gtk.Box:
        library = _box(Gtk.Orientation.VERTICAL, 3, "chrome-panel", "library-panel")
        library.set_vexpand(True)
        title = self._panel_title("МЕДИАТЕКА  •")
        self.heading_label = _label("Вся музыка здесь", "panel-title-text")
        self.heading_label.set_hexpand(True)
        self.heading_label.set_ellipsize(Pango.EllipsizeMode.END)
        title.append(self.heading_label)
        self.refresh_button = _icon_button("view-refresh-symbolic", "Обновить список · Ctrl+R", "refresh-button")
        self.refresh_button.connect("clicked", lambda _: self._call("refresh_view"))
        title.append(self.refresh_button)
        library.append(title)
        toolbar = Gtk.FlowBox()
        toolbar.add_css_class("library-toolbar")
        toolbar.set_selection_mode(Gtk.SelectionMode.NONE)
        toolbar.set_min_children_per_line(1)
        toolbar.set_max_children_per_line(4)
        toolbar.set_column_spacing(4)
        toolbar.set_row_spacing(4)
        toolbar.set_valign(Gtk.Align.START)
        self.wave_button = self._nav_button("Моя волна", "wave")
        self.liked_button = self._nav_button("Любимые треки", "liked_tracks")
        self.playlists_button = self._nav_button("Плейлисты", "playlists")
        toolbar.append(self.wave_button)
        toolbar.append(self.liked_button)
        toolbar.append(self.playlists_button)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.add_css_class("music-search")
        self.search_entry.set_placeholder_text("Поиск музыки · Enter")
        self.search_entry.set_size_request(160, -1)
        self.search_entry.connect("activate", self._on_search)
        toolbar.append(self.search_entry)
        library.append(toolbar)

        list_actions = _box(Gtk.Orientation.HORIZONTAL, 6, "list-actions")
        self.play_all_button = Gtk.Button(label="▶  Слушать всё")
        self.play_all_button.add_css_class("nav-button")
        self.play_all_button.set_sensitive(False)
        self.play_all_button.set_tooltip_text("Воспроизвести текущий список с начала")
        self.play_all_button.connect("clicked", lambda _: self._call("play_all", False))
        list_actions.append(self.play_all_button)
        self.shuffle_all_button = Gtk.Button(label="⇄  Перемешать")
        self.shuffle_all_button.add_css_class("nav-button")
        self.shuffle_all_button.set_sensitive(False)
        self.shuffle_all_button.set_tooltip_text("Воспроизвести текущий список в случайном порядке")
        self.shuffle_all_button.connect("clicked", lambda _: self._call("play_all", True))
        list_actions.append(self.shuffle_all_button)
        self.list_summary_label = _label("", "status-text", xalign=1)
        self.list_summary_label.set_hexpand(True)
        self.list_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.list_summary_label.set_width_chars(1)
        self._update_list_summary()
        list_actions.append(self.list_summary_label)
        library.append(list_actions)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.add_css_class("list-well")
        library.append(self.stack)
        self.tracks_list = self._new_tracks_list()
        track_page = _box(Gtk.Orientation.VERTICAL, 0)
        column_header = _box(Gtk.Orientation.HORIZONTAL, 8, "column-header")
        column_number = _column_label("№", 32, "column-label", xalign=0.5)
        column_header.append(column_number)
        column_title = _label("НАЗВАНИЕ / ИСПОЛНИТЕЛЬ", "column-label")
        column_title.set_hexpand(True)
        column_header.append(column_title)
        column_album = _column_label("АЛЬБОМ", 160, "column-label")
        self.column_album = column_album
        column_header.append(column_album)
        column_time = _column_label("ВРЕМЯ", 42, "column-label", xalign=1)
        column_header.append(column_time)
        column_end = _label("", "column-label")
        column_end.set_size_request(70, -1)
        column_header.append(column_end)
        track_page.append(column_header)
        self.track_scroll = Gtk.ScrolledWindow()
        self.track_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.track_scroll.set_vexpand(True)
        self.track_scroll.set_child(self.tracks_list)
        adjustment = self.track_scroll.get_vadjustment()
        adjustment.connect("value-changed", self._on_track_scroll)
        adjustment.connect("changed", self._on_track_scroll)
        self.track_scroll.connect("map", lambda _: self._schedule_auto_more())
        track_page.append(self.track_scroll)
        self.stack.add_named(track_page, "tracks")
        self.playlists_list = self._new_playlists_list()
        self.playlist_scroll = Gtk.ScrolledWindow()
        self.playlist_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.playlist_scroll.set_child(self.playlists_list)
        self.stack.add_named(self.playlist_scroll, "playlists")
        self.stack.set_visible_child_name("tracks")
        self.more_button = Gtk.Button(label="Показать ещё")
        self.more_button.add_css_class("nav-button")
        self.more_button.set_halign(Gtk.Align.CENTER)
        self.more_button.set_tooltip_text("Загрузить следующую страницу")
        self.more_button.set_visible(False)
        self.more_button.connect("clicked", self._on_more_clicked)
        library.append(self.more_button)

        return library

    def _new_tracks_list(self) -> Gtk.ListBox:
        tracks = Gtk.ListBox()
        tracks.set_selection_mode(Gtk.SelectionMode.NONE)
        tracks.add_css_class("music-list")
        tracks.connect("row-activated", self._on_track_activated)
        self.track_empty = _label("Войдите, чтобы найти музыку или открыть свою медиатеку.", "empty-state", xalign=0.5)
        self.track_empty.set_wrap(True)
        tracks.set_placeholder(self.track_empty)
        return tracks

    def _new_playlists_list(self) -> Gtk.ListBox:
        playlists = Gtk.ListBox()
        playlists.set_selection_mode(Gtk.SelectionMode.NONE)
        playlists.add_css_class("music-list")
        playlists.connect("row-activated", self._on_playlist_activated)
        playlists.set_placeholder(_label("Здесь пока нет плейлистов.", "empty-state", xalign=0.5))
        return playlists

    def _build_statusbar(self) -> Gtk.Box:
        status_bar = _box(Gtk.Orientation.HORIZONTAL, 6, "statusbar")
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        status_bar.append(self.spinner)
        self.status_label = _label("Готово", "status-text")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_hexpand(True)
        status_bar.append(self.status_label)
        self.sleep_timer_label = _label("", "status-text", "sleep-timer")
        self.sleep_timer_label.set_visible(False)
        self.sleep_timer_label.set_tooltip_text("До автоматической остановки")
        status_bar.append(self.sleep_timer_label)
        status_bar.append(_label("IMPULS", "status-indicator"))
        return status_bar

    def set_sleep_timer_remaining(self, seconds: int | None) -> None:
        active = seconds is not None and seconds > 0
        label = f"Сон через {_format_time(int(seconds) * 1000)}" if active else "Таймер сна"
        self.sleep_timer_label.set_label(label if active else "")
        self.sleep_timer_label.set_visible(active)
        self.sleep_timer_menu_label.set_label(label)
        self.cancel_sleep_button.set_sensitive(active)

    def _favorite(self, liked: bool, popover: Gtk.Popover) -> None:
        popover.popdown()
        if self._current_track is not None:
            self._call("set_liked", self._current_track, liked)

    def set_shuffle_state(self, enabled: bool) -> None:
        self.shuffle_button.handler_block(self._shuffle_handler)
        try:
            self.shuffle_button.set_active(bool(enabled))
        finally:
            self.shuffle_button.handler_unblock(self._shuffle_handler)

    def set_repeat_state(self, enabled: bool) -> None:
        self.repeat_button.handler_block(self._repeat_handler)
        try:
            self.repeat_button.set_active(bool(enabled))
        finally:
            self.repeat_button.handler_unblock(self._repeat_handler)

    def _focus_is_editable(self) -> bool:
        widget = self.get_focus()
        while widget is not None and widget is not self:
            if isinstance(widget, (Gtk.Editable, Gtk.TextView)):
                return True
            widget = widget.get_parent()
        return False

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        extra = state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK)
        if control and not extra and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.set_mini_mode(False)
            self.view_stack.set_visible_child_name("library")
            self.search_entry.grab_focus()
            return True
        if not extra and keyval == Gdk.KEY_F1:
            self._show_help()
            return True
        if not extra and (keyval == Gdk.KEY_F5 or control and keyval in (Gdk.KEY_r, Gdk.KEY_R)):
            self._call("refresh_view")
            return True
        if control and not extra and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self._call("quit")
            return True
        if self._focus_is_editable():
            return False
        if control and not extra and state & Gdk.ModifierType.SHIFT_MASK and keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self.set_mini_mode(not self._mini_mode)
            return True
        if control and not extra and keyval in (Gdk.KEY_j, Gdk.KEY_J):
            self.jump_to_current()
            return True
        if control and not extra and keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self._add_moment()
            return True
        if state == Gdk.ModifierType.ALT_MASK and keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            if self._duration_ms <= 0:
                return False
            delta = -5000 if keyval == Gdk.KEY_Left else 5000
            self._seek_to(min(1.0, max(0.0, (self._position_ms + delta) / self._duration_ms)))
            return True
        if control and not extra and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            delta = 0.05 if keyval == Gdk.KEY_Up else -0.05
            self.volume_scale.set_value(min(1.0, max(0.0, self.volume_scale.get_value() + delta)))
            return True
        if control and not extra and keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self._toggle_mute()
            return True
        if control and not extra and keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self._call("previous_track" if keyval == Gdk.KEY_Left else "next_track")
            return True
        if not control and not extra and keyval == Gdk.KEY_space:
            focus = self.get_focus()
            if isinstance(focus, (Gtk.Button, Gtk.Range, Gtk.DropDown, Gtk.MenuButton)):
                return False
            self._call("toggle_playback")
            return True
        return False

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if query:
            self.view_stack.set_visible_child_name("library")
            self._set_active_navigation("search")
            self._call("search", query)

    def _on_track_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._render_kind != "tracks" or self.stack.get_visible_child_name() != "tracks":
            return
        occurrence = self._track_rows.get(row)
        if occurrence is not None:
            self._request_track(*occurrence, self._render_generation)

    def _on_playlist_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._render_kind != "playlists" or self.stack.get_visible_child_name() != "playlists":
            return
        playlist = self._playlist_rows.get(row)
        if playlist is not None:
            self.view_stack.set_visible_child_name("library")
            self._call("open_playlist", playlist)

    def _on_seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        self._seek_to(min(1.0, max(0.0, value)))
        return False

    def _seek_to(self, fraction: float) -> None:
        position = self._call("seek", fraction)
        if type(position) is int and position >= 0:
            self._position_ms = position
            self._refresh_clock()
            if self._duration_ms > 0:
                self.seek_scale.set_value(min(1.0, position / self._duration_ms))

    def _on_account_clicked(self, _button: Gtk.Button) -> None:
        if self._account is not None:
            self._call("logout")
        else:
            self._open_login()

    def _modal(self, title: str, width: int = 440) -> tuple[Gtk.Window, Gtk.Box]:
        window = Gtk.Window(title=title, transient_for=self, modal=True, destroy_with_parent=True, default_width=width)
        header = Gtk.HeaderBar()
        header.add_css_class("window-header")
        header.set_decoration_layout(":close")
        header.set_title_widget(_label(title, "window-title", xalign=0.5))
        window.set_titlebar(header)
        content = _box(Gtk.Orientation.VERTICAL, 14, "dialog-content")
        window.set_child(content)
        return window, content

    def _open_login(self) -> None:
        if self._login_window is not None:
            self._login_window.present()
            return
        window, content = self._modal("Вход в Яндекс Музыку")
        self._login_window = window
        window.connect("close-request", self._close_login)
        content.append(_label("Войти в аккаунт", "dialog-title"))
        intro = _label("Получите одноразовый код и подтвердите вход на странице Яндекса.", "dialog-copy")
        intro.set_wrap(True)
        content.append(intro)
        remember = Gtk.CheckButton(label="Запомнить вход в системном хранилище")
        remember.set_tooltip_text("Токен будет сохранён только в системном хранилище секретов")
        content.append(remember)
        device_button = Gtk.Button(label="Войти по коду")
        device_button.add_css_class("primary-button")
        def device_clicked(_button: Gtk.Button) -> None:
            should_remember = remember.get_active()
            self._close_login_window()
            self._call("device_login", should_remember)
        device_button.connect("clicked", device_clicked)
        content.append(device_button)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        content.append(_label("Или введите токен вручную", "dialog-subtitle"))
        token_entry = Gtk.PasswordEntry()
        token_entry.set_property("placeholder-text", "Токен Яндекс Музыки")
        token_entry.set_show_peek_icon(False)
        content.append(token_entry)
        token_button = Gtk.Button(label="Войти с токеном")
        def token_clicked(_widget) -> None:
            token = token_entry.get_text().strip()
            if not token:
                token_entry.grab_focus()
                return
            should_remember = remember.get_active()
            token_entry.set_text("")
            self._close_login_window()
            self._call("login", token, should_remember)
        token_button.connect("clicked", token_clicked)
        token_entry.connect("activate", token_clicked)
        content.append(token_button)
        window.present()

    def _close_login(self, _window: Gtk.Window) -> bool:
        self._login_window = None
        return False

    def _close_login_window(self) -> None:
        if self._login_window is not None:
            window, self._login_window = self._login_window, None
            window.destroy()

    def show_device_code(self, code: str, url: str) -> None:
        if not _is_safe_login_url(url):
            self.show_error("Получена небезопасная ссылка для входа. Попробуйте ещё раз.")
            return
        if self._device_window is not None:
            self._device_window.destroy()
        window, content = self._modal("Подтвердите вход")
        self._device_window = window
        window.connect("close-request", self._close_device)
        content.append(_label("Подтвердите вход", "dialog-title"))
        text = _label("Откройте страницу Яндекса и введите код:", "dialog-copy")
        text.set_wrap(True)
        content.append(text)
        code_label = _label(str(code), "device-code", xalign=0.5)
        code_label.set_selectable(True)
        content.append(code_label)
        link = Gtk.LinkButton.new_with_label(url, url)
        link.set_tooltip_text("Открыть страницу Яндекса в браузере")
        content.append(link)
        note = _label("Ожидаем подтверждение. Это окно можно закрыть.", "dialog-copy")
        note.set_wrap(True)
        content.append(note)
        close = Gtk.Button(label="Закрыть")
        close.connect("clicked", lambda _: self._close_device_window())
        content.append(close)
        window.present()

    def _close_device(self, _window: Gtk.Window) -> bool:
        self._device_window = None
        return False

    def _close_device_window(self) -> None:
        if self._device_window is not None:
            window, self._device_window = self._device_window, None
            window.destroy()

    def set_status(self, message: str) -> None:
        self.status_label.set_label(str(message))
        self.status_label.set_tooltip_text(str(message))

    def set_busy(self, busy: bool) -> None:
        self.spinner.set_visible(bool(busy))
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def set_account(self, name: str | None) -> None:
        self._account = name
        self.account_label.set_label(name if name else "Гость")
        self.account_button.set_label("Выйти" if name else "Войти")
        if name:
            self._close_device_window()

    def _cancel_render(self) -> None:
        self._render_generation += 1
        self._render_queue.clear()
        if self._render_source_id:
            GLib.source_remove(self._render_source_id)
            self._render_source_id = 0

    def _retire_list(self, old_list: Gtk.ListBox) -> None:
        # Keep the detached list alive while destroying its rows in small idle
        # slices too; unparenting thousands of widgets must not freeze GTK.
        if old_list.get_row_at_index(0) is not None:
            self._retired_lists.append(old_list)
            if not self._dispose_source_id:
                self._dispose_source_id = GLib.idle_add(self._dispose_rows)

    def _dispose_rows(self) -> bool:
        if self._destroyed:
            self._dispose_source_id = 0
            return GLib.SOURCE_REMOVE
        deadline = time.monotonic() + 0.004
        count = 0
        while self._retired_lists and count < 24 and (count == 0 or time.monotonic() < deadline):
            old = self._retired_lists[0]
            row = old.get_row_at_index(0)
            if row is None:
                self._retired_lists.popleft()
            else:
                old.remove(row)
                count += 1
        if not self._retired_lists:
            self._dispose_source_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _replace_tracks_list(self) -> None:
        old = self.tracks_list
        self._retire_list(old)
        self.tracks_list = self._new_tracks_list()
        self.track_scroll.set_child(self.tracks_list)
        self._track_rows = {}
        self._rows_by_track_id = {}
        self._album_labels = []
        self._highlighted_row = None
        self._selected_track_index = None
        self._rendered_track_count = 0
        self._loaded_duration_ms = 0
        self._unknown_duration_count = 0

    def _replace_playlists_list(self) -> None:
        old = self.playlists_list
        self._retire_list(old)
        self.playlists_list = self._new_playlists_list()
        self.playlist_scroll.set_child(self.playlists_list)
        self._playlist_rows = {}

    def _schedule_render(self) -> None:
        if self._render_queue and not self._render_source_id and not self._destroyed:
            self._render_source_id = GLib.idle_add(self._render_rows)

    def _render_rows(self) -> bool:
        if self._destroyed:
            self._render_source_id = 0
            return GLib.SOURCE_REMOVE
        deadline = time.monotonic() + 0.004
        count = 0
        while self._render_queue and count < 24 and (count == 0 or time.monotonic() < deadline):
            index, item = self._render_queue.popleft()
            if self._render_kind == "tracks":
                self._append_track_row(index, item)
            else:
                self._append_playlist_row(item)
            count += 1
        if not self._render_queue:
            self._render_source_id = 0
            self._schedule_auto_more()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def show_loading(self, heading: str) -> None:
        """Show a new view immediately while the controller fetches its data."""
        self.view_stack.set_visible_child_name("library")
        self.set_tracks([], heading)
        self.track_empty.set_label("Загрузка…")
        self.set_status("Загрузка…")

    def show_load_error(self, message: str = "Не удалось загрузить данные. Повторите попытку.") -> None:
        """Replace the loading placeholder without discarding existing rows."""
        self.track_empty.set_label(message)
        self.set_status(message)

    def set_tracks(self, tracks: list, heading: str) -> None:
        self._cancel_render()
        self._replace_tracks_list()
        self._render_kind = "tracks"
        self._auto_more_empty_page = False
        self._auto_more_layout_retries = 0
        self.heading_label.set_label(heading)
        self.stack.set_visible_child_name("tracks")
        self.track_empty.set_label("Треки не найдены.")
        self.set_pagination(False)
        self.append_tracks(tracks)

    def append_tracks(self, tracks: list) -> None:
        """Add a page without blocking the main loop; indices span all pages."""
        if self._destroyed or self._render_kind != "tracks":
            return
        page = tuple(tracks)
        if not page and self._pagination_loading:
            self._auto_more_empty_page = True
        elif page:
            self._auto_more_empty_page = False
            self._auto_more_layout_retries = 0
        start = self._rendered_track_count
        self._rendered_track_count += len(page)
        for track in page:
            duration = track.duration_ms or 0
            if duration > 0:
                self._loaded_duration_ms += duration
            else:
                self._unknown_duration_count += 1
        self._update_list_summary()
        self.play_all_button.set_sensitive(self._rendered_track_count > 0)
        self.shuffle_all_button.set_sensitive(self._rendered_track_count > 0)
        self._render_queue.extend((start + offset, item) for offset, item in enumerate(page))
        self._schedule_render()

    def _update_list_summary(self) -> None:
        duration = _format_time(self._loaded_duration_ms)
        suffix = "+" if self._unknown_duration_count else ""
        summary = f"Загружено: {self._rendered_track_count} · {duration}{suffix}"
        self.list_summary_label.set_label(summary)
        self.list_summary_label.set_visible(True)
        tooltip = f"{summary}\nСуммарная длительность загруженных треков; следующие страницы не учтены."
        if self._unknown_duration_count:
            tooltip += f"\n+ — длительность части треков неизвестна ({self._unknown_duration_count}); показана только известная сумма."
        self.list_summary_label.set_tooltip_text(tooltip)

    def _append_track_row(self, index: int, track) -> None:
        row = Gtk.ListBoxRow()
        if index % 2:
            row.add_css_class("alternate")
        line = _box(Gtk.Orientation.HORIZONTAL, 8, "track-row")
        number = _column_label(f"{index + 1:02}", 32, "track-number", xalign=0.5)
        number.set_tooltip_text(str(index + 1))
        line.append(number)
        artists = ", ".join(track.artists) or "Неизвестный исполнитель"
        details = _box(Gtk.Orientation.VERTICAL, 2, "track-details")
        details.set_hexpand(True)
        title = _label(str(track.title), "track-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_tooltip_text(str(track.title))
        details.append(title)
        artist = _label(artists, "track-subtitle")
        artist.set_ellipsize(Pango.EllipsizeMode.END)
        artist.set_tooltip_text(artists)
        details.append(artist)
        line.append(details)
        album = _column_label(str(track.album or ""), 160, "track-album")
        album.set_tooltip_text(str(track.album or ""))
        album.set_visible(not self._narrow_columns)
        self._album_labels.append(album)
        line.append(album)
        duration = _column_label(_format_time(track.duration_ms), 42, "track-duration", xalign=1)
        duration.set_tooltip_text(_format_time(track.duration_ms))
        line.append(duration)
        play = _icon_button("media-playback-start-symbolic", f"Воспроизвести: {track.title}", "row-play")
        play.set_valign(Gtk.Align.CENTER)
        generation = self._render_generation
        play.connect("clicked", lambda _button, item=track, position=index, current=generation:
                     self._request_track(item, position, current))
        row_actions = _box(Gtk.Orientation.HORIZONTAL, 8)
        row_actions.set_size_request(70, -1)
        row_actions.append(play)
        row_actions.append(self._track_menu(track, artists))
        line.append(row_actions)
        row.set_child(line)
        self._track_rows[row] = (track, index)
        self._rows_by_track_id.setdefault(str(track.id), []).append(row)
        self.tracks_list.append(row)
        if (self._current_track is not None and str(self._current_track.id) == str(track.id)
                and (self._highlighted_row is None or index == self._selected_track_index)):
            self._refresh_current_row()

    def _request_track(self, track, index: int, generation: int) -> None:
        if self._destroyed or generation != self._render_generation:
            return
        self._selected_track_index = index
        self._call("play_track", track, index)

    def _track_menu(self, track, artists: str) -> Gtk.MenuButton:
        menu = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu.add_css_class("track-menu")
        menu.set_tooltip_text(f"Действия с треком: {track.title}")
        menu.update_property([Gtk.AccessibleProperty.LABEL], [f"Действия с треком: {track.title}"])
        popover = Gtk.Popover()
        body = _box(Gtk.Orientation.VERTICAL, 5, "popover-actions")
        copy = Gtk.Button(label="Копировать название и исполнителя")
        def copy_clicked(_button) -> None:
            self.get_clipboard().set(f"{artists} — {track.title}")
            self.set_status("Название и исполнитель скопированы")
            popover.popdown()
        copy.connect("clicked", copy_clicked)
        body.append(copy)
        url = _track_url(str(track.id))
        copy_link = Gtk.Button(label="Копировать ссылку на трек")
        copy_link.set_sensitive(url is not None)
        def copy_link_clicked(_button) -> None:
            if url is not None:
                self.get_clipboard().set(url)
                self.set_status("Ссылка на трек скопирована")
            popover.popdown()
        copy_link.connect("clicked", copy_link_clicked)
        body.append(copy_link)
        open_link = Gtk.Button(label="Открыть в Яндекс Музыке")
        open_link.set_sensitive(url is not None)
        def open_clicked(_button) -> None:
            popover.popdown()
            if url is not None:
                Gtk.show_uri(self, url, Gdk.CURRENT_TIME)
        open_link.connect("clicked", open_clicked)
        body.append(open_link)
        popover.set_child(body)
        menu.set_popover(popover)
        return menu

    def set_playlists(self, playlists: list) -> None:
        self._cancel_render()
        self._replace_playlists_list()
        self._render_kind = "playlists"
        self.list_summary_label.set_label("")
        self.list_summary_label.set_visible(False)
        self.list_summary_label.set_tooltip_text(None)
        self.play_all_button.set_sensitive(False)
        self.shuffle_all_button.set_sensitive(False)
        self.heading_label.set_label("Плейлисты")
        self.stack.set_visible_child_name("playlists")
        self.set_pagination(False)
        self._render_queue.extend(enumerate(playlists))
        self._schedule_render()

    def _append_playlist_row(self, playlist) -> None:
        row = Gtk.ListBoxRow()
        line = _box(Gtk.Orientation.HORIZONTAL, 8, "track-row")
        badge = _label("♫", "playlist-badge", xalign=0.5)
        line.append(badge)
        title = _label(str(playlist.title), "track-title")
        title.set_tooltip_text(str(playlist.title))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_hexpand(True)
        line.append(title)
        line.append(_label(f"Треков: {playlist.track_count}", "track-subtitle"))
        line.append(_label("›", "playlist-arrow", xalign=0.5))
        row.set_child(line)
        self._playlist_rows[row] = playlist
        self.playlists_list.append(row)

    def set_pagination(self, has_more: bool, loading: bool = False, error: bool = False) -> None:
        self._pagination_has_more = bool(has_more)
        self._pagination_loading = bool(loading)
        self._pagination_error = bool(error)
        self.more_button.set_visible(self._pagination_has_more or self._pagination_loading)
        self.more_button.set_sensitive(self._pagination_has_more and not self._pagination_loading)
        label = "Загрузка…" if self._pagination_loading else ("Повторить загрузку" if self._pagination_error else "Показать ещё")
        self.more_button.set_label(label)
        if self._pagination_has_more and not self._pagination_loading and not self._pagination_error:
            self._schedule_auto_more()
        elif self._auto_more_source_id:
            GLib.source_remove(self._auto_more_source_id)
            self._auto_more_source_id = 0

    def _on_track_scroll(self, _adjustment: Gtk.Adjustment) -> None:
        self._schedule_auto_more()

    def _schedule_auto_more(self) -> None:
        if (self._destroyed or self._auto_more_source_id or not self._pagination_has_more
                or self._pagination_loading or self._pagination_error or self._auto_more_empty_page):
            return
        self._auto_more_source_id = GLib.idle_add(self._check_auto_more)

    def _check_auto_more(self) -> bool:
        self._auto_more_source_id = 0
        if (self._destroyed or not self._pagination_has_more or self._pagination_loading or self._pagination_error
                or self._auto_more_empty_page or self._render_queue
                or self.view_stack.get_visible_child_name() != "library"
                or self.stack.get_visible_child_name() != "tracks" or not self.track_scroll.get_mapped()):
            return GLib.SOURCE_REMOVE
        adjustment = self.track_scroll.get_vadjustment()
        page_size = adjustment.get_page_size()
        last_row = self.tracks_list.get_row_at_index(self._rendered_track_count - 1)
        if page_size <= 0 or (last_row is not None and last_row.get_height() <= 0):
            # Row insertion and adjustment allocation happen in different GTK
            # phases. Do not mistake the previous viewport for the new list.
            if self._auto_more_layout_retries < 20:
                self._auto_more_layout_retries += 1
                self._auto_more_source_id = GLib.timeout_add(50, self._check_auto_more)
            return GLib.SOURCE_REMOVE
        self._auto_more_layout_retries = 0
        if page_size > 0 and adjustment.get_upper() - adjustment.get_value() - page_size <= 300:
            self._request_more(background=True)
        return GLib.SOURCE_REMOVE

    def _on_more_clicked(self, _button: Gtk.Button) -> None:
        self._request_more(background=False)

    def _request_more(self, background: bool) -> None:
        if not self._pagination_has_more or self._pagination_loading:
            return
        self.set_pagination(True, loading=True)
        if background:
            self._call("load_more", True)
        else:
            self._call("load_more")

    def _refresh_current_row(self) -> None:
        if self._highlighted_row is not None:
            self._highlighted_row.remove_css_class("current-track")
            self._highlighted_row = None
        if self._current_track is None:
            return
        candidates = self._rows_by_track_id.get(str(self._current_track.id), ())
        preferred = None
        if self._selected_track_index is not None:
            preferred = next((row for row in candidates
                              if self._track_rows[row][1] == self._selected_track_index), None)
        if preferred is None:
            preferred = next((row for row in candidates if self._track_rows[row][0] is self._current_track), None)
        if preferred is None and candidates:
            preferred = candidates[0]
        if preferred is not None:
            preferred.add_css_class("current-track")
            self._highlighted_row = preferred

    def _on_destroy(self, _window) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        if self._geometry_tick_id:
            self.remove_tick_callback(self._geometry_tick_id)
            self._geometry_tick_id = 0
        if self._jump_tick_id:
            self.remove_tick_callback(self._jump_tick_id)
            self._jump_tick_id = 0
        self._close_help_window()
        self._cancel_render()
        if self._dispose_source_id:
            GLib.source_remove(self._dispose_source_id)
            self._dispose_source_id = 0
        if self._auto_more_source_id:
            GLib.source_remove(self._auto_more_source_id)
            self._auto_more_source_id = 0
        if self._spectrum_source_id:
            GLib.source_remove(self._spectrum_source_id)
            self._spectrum_source_id = 0
        self._retired_lists.clear()
        display = Gdk.Display.get_default()
        if display is not None and hasattr(self, "_css_provider"):
            Gtk.StyleContext.remove_provider_for_display(display, self._css_provider)

    def _on_close_request(self, _window) -> bool:
        if self._call("close_window") is True:
            return True
        self._on_destroy(self)
        return False

    def destroy(self) -> None:
        # Gtk.Window.destroy() may retain an unrealized object until finalization;
        # stop queued work immediately even in that case.
        self._on_destroy(self)
        super().destroy()

    def set_now_playing(self, track, index: int | None = None) -> None:
        self.set_stream_quality(None, None)
        self._current_track = track
        self.moment_add_button.set_sensitive(False)
        self._render_moments()
        if index is not None:
            self._selected_track_index = index
        elif track is None:
            self._selected_track_index = None
        self.favorite_button.set_sensitive(track is not None)
        if track is None:
            self.now_title.set_label("Ничего не играет")
            self.now_artist.set_label("Выберите трек")
            self.visual_track_title.set_label("")
            self.now_title.set_tooltip_text(None)
            self.now_artist.set_tooltip_text(None)
            self.visual_track_title.set_tooltip_text(None)
        else:
            self.now_title.set_label(str(track.title))
            self.now_title.set_tooltip_text(str(track.title))
            artist = ", ".join(track.artists) or "Неизвестный исполнитель"
            self.now_artist.set_label(artist)
            self.now_artist.set_tooltip_text(artist)
            self.visual_track_title.set_label(f"{artist} — {track.title}")
            self.visual_track_title.set_tooltip_text(f"{artist} — {track.title}")
        self._refresh_current_row()

    def set_stream_quality(self, codec: str | None, bitrate: int | None) -> None:
        """Display the actual format of the playing stream, never its preference."""
        if not codec:
            self.stream_quality_label.set_visible(False)
            self.stream_quality_label.set_label("")
            return
        formats = {"mp3": "MP3", "aac": "AAC", "aac-mp4": "AAC",
                   "he-aac": "HE-AAC", "he-aac-mp4": "HE-AAC",
                   "flac": "FLAC", "flac-mp4": "FLAC"}
        name = formats.get(str(codec).strip().lower(), "Формат неизвестен")
        if isinstance(bitrate, int) and not isinstance(bitrate, bool) and bitrate > 0:
            name += f" · {bitrate} кбит/с"
        self.stream_quality_label.set_label(name)
        self.stream_quality_label.set_visible(True)

    def set_volume(self, fraction: float) -> None:
        value = min(1.0, max(0.0, fraction))
        if abs(self.volume_scale.get_value() - value) > 0.0001:
            self.volume_scale.set_value(value)

    def set_playback(self, playing: bool, position_ms: int = 0, duration_ms: int = 0) -> None:
        self._position_ms = max(0, position_ms)
        self._duration_ms = max(0, duration_ms)
        self.moment_add_button.set_sensitive(self._current_track is not None and self._duration_ms > 0)
        self.play_button.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        tooltip = "Пауза" if playing else "Воспроизвести"
        self.play_button.set_tooltip_text(tooltip)
        self.play_button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        self._refresh_clock()
        self.duration_label.set_label(_format_time(duration_ms))
        self.seek_scale.set_sensitive(duration_ms > 0)
        self.seek_scale.set_value(min(1.0, max(0.0, position_ms / duration_ms)) if duration_ms > 0 else 0)

    def _refresh_clock(self) -> None:
        if self.time_toggle.get_active():
            text = "−" + _format_time(max(0, self._duration_ms - self._position_ms)) if self._duration_ms else "−−:−−"
        else:
            text = _format_time(self._position_ms)
        self.position_label.set_label(text)
        self.position_label.update_property([Gtk.AccessibleProperty.LABEL],
                                           ["Оставшееся время" if self.time_toggle.get_active() else "Прошедшее время"])

    def show_error(self, message: str) -> None:
        if self._error_window is not None:
            self._error_window.destroy()
        window, content = self._modal("Не удалось выполнить действие")
        self._error_window = window
        window.connect("close-request", self._close_error)
        content.append(_label("Что-то пошло не так", "dialog-title"))
        error = _label(str(message), "dialog-copy")
        error.set_wrap(True)
        error.set_selectable(True)
        content.append(error)
        close = Gtk.Button(label="Понятно")
        close.connect("clicked", lambda _: self._close_error_window())
        content.append(close)
        window.present()

    def _close_error(self, _window: Gtk.Window) -> bool:
        self._error_window = None
        return False

    def _close_error_window(self) -> None:
        if self._error_window is not None:
            window, self._error_window = self._error_window, None
            window.destroy()
