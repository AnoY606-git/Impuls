"""GTK presentation for Impuls. This module never performs network requests."""
from __future__ import annotations

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


def _format_time(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds or 0)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


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
        self._track_rows: dict[Gtk.ListBoxRow, tuple[object, int]] = {}
        self._playlist_rows: dict[Gtk.ListBoxRow, object] = {}
        self._login_window: Gtk.Window | None = None
        self._device_window: Gtk.Window | None = None
        self._error_window: Gtk.Window | None = None
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
        self.set_default_size(860, 720)
        self.set_size_request(480, 360)
        self._install_css()
        self._build()

    def _install_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = provider

    def _call(self, name: str, *args) -> None:
        action = self.actions.get(name)
        if action is not None:
            action(*args)

    def _build(self) -> None:
        root = _box(Gtk.Orientation.VERTICAL, 5, "app-shell")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(root)
        self.set_child(scroller)
        self.view_stack = Gtk.Stack()
        self.view_stack.set_vexpand(True)
        root.append(self._build_deck())
        root.append(self._build_equalizer())
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
        button.connect("clicked", lambda _: self._call(action))
        return button

    def _build_deck(self) -> Gtk.Box:
        deck = _box(Gtk.Orientation.VERTICAL, 4, "chrome-panel", "deck")
        title = self._panel_title("IMPULS  •  ПРОИГРЫВАТЕЛЬ")
        title.get_first_child().set_hexpand(False)
        self.view_switcher = Gtk.StackSwitcher(stack=self.view_stack)
        self.view_switcher.add_css_class("view-switcher")
        self.view_switcher.set_hexpand(True)
        self.view_switcher.set_halign(Gtk.Align.CENTER)
        title.append(self.view_switcher)
        self.account_label = _label("Гость", "title-account")
        self.account_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.account_label.set_max_width_chars(24)
        title.append(self.account_label)
        self.account_button = Gtk.Button(label="Войти")
        self.account_button.add_css_class("title-button")
        self.account_button.connect("clicked", self._on_account_clicked)
        title.append(self.account_button)
        deck.append(title)

        display = _box(Gtk.Orientation.HORIZONTAL, 12, "led-display")
        clock = _box(Gtk.Orientation.VERTICAL, 0, "clock-display")
        clock.set_valign(Gtk.Align.CENTER)
        clock.append(_label("ВРЕМЯ", "display-caption"))
        self.position_label = _label("0:00", "display-time")
        self.position_label.set_width_chars(5)
        clock.append(self.position_label)
        display.append(clock)
        display_details = _box(Gtk.Orientation.VERTICAL, 3, "display-details")
        display_details.set_hexpand(True)
        display_details.set_valign(Gtk.Align.CENTER)
        display_details.append(_label("ВОСПРОИЗВОДИТСЯ", "display-caption"))
        self.now_title = _label("Ничего не играет", "now-title")
        self.now_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.now_artist = _label("Выберите трек", "now-artist")
        self.now_artist.set_ellipsize(Pango.EllipsizeMode.END)
        display_details.append(self.now_title)
        display_details.append(self.now_artist)
        display.append(display_details)
        self.duration_label = _label("0:00", "display-duration", xalign=1)
        self.duration_label.set_width_chars(5)
        self.duration_label.set_valign(Gtk.Align.END)
        display.append(self.duration_label)
        deck.append(display)

        seek_row = _box(Gtk.Orientation.HORIZONTAL, 8, "seek-row")
        seek_row.append(_label("ПОЗИЦИЯ", "metal-caption"))
        self.seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self.seek_scale.set_draw_value(False)
        self.seek_scale.set_hexpand(True)
        self.seek_scale.set_sensitive(False)
        self.seek_scale.set_tooltip_text("Позиция воспроизведения")
        self.seek_scale.update_property([Gtk.AccessibleProperty.LABEL], ["Позиция воспроизведения"])
        self.seek_scale.connect("change-value", self._on_seek)
        seek_row.append(self.seek_scale)
        deck.append(seek_row)

        controls = _box(Gtk.Orientation.HORIZONTAL, 6, "deck-controls")
        previous = _icon_button("media-skip-backward-symbolic", "Предыдущий трек", "transport-button")
        previous.connect("clicked", lambda _: self._call("previous_track"))
        controls.append(previous)
        self.play_button = _icon_button("media-playback-start-symbolic", "Воспроизвести", "transport-button", "play-button")
        self.play_button.connect("clicked", lambda _: self._call("toggle_playback"))
        controls.append(self.play_button)
        next_button = _icon_button("media-skip-forward-symbolic", "Следующий трек", "transport-button")
        next_button.connect("clicked", lambda _: self._call("next_track"))
        controls.append(next_button)
        self.shuffle_button = Gtk.ToggleButton(label="⇄")
        self.shuffle_button.add_css_class("transport-toggle")
        self.shuffle_button.set_tooltip_text("Случайный порядок обычной медиатеки. «Моя волна» сохраняет порядок рекомендаций.")
        self.shuffle_button.update_property([Gtk.AccessibleProperty.LABEL], ["Случайный порядок"])
        self._shuffle_handler = self.shuffle_button.connect("toggled", lambda button: self._call("set_shuffle", button.get_active()))
        controls.append(self.shuffle_button)
        self.repeat_button = Gtk.ToggleButton(label="↻")
        self.repeat_button.add_css_class("transport-toggle")
        self.repeat_button.set_tooltip_text("Повтор обычной очереди. «Моя волна» автоматически загружает следующую партию.")
        self.repeat_button.update_property([Gtk.AccessibleProperty.LABEL], ["Повтор очереди"])
        self._repeat_handler = self.repeat_button.connect("toggled", lambda button: self._call("set_repeat", button.get_active()))
        controls.append(self.repeat_button)
        controls.append(Gtk.Box(hexpand=True))
        self.favorite_button = Gtk.MenuButton(label="♥")
        self.favorite_button.set_tooltip_text("Действия с любимыми треками")
        self.favorite_button.add_css_class("favorite-button")
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
        controls.append(self.favorite_button)
        controls.append(_label("ГРОМКОСТЬ", "metal-caption"))
        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.01)
        self.volume_scale.set_size_request(105, -1)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_value(0.75)
        self.volume_scale.set_tooltip_text("Громкость")
        self.volume_scale.update_property([Gtk.AccessibleProperty.LABEL], ["Громкость"])
        self.volume_scale.connect("value-changed", lambda scale: self._call("set_volume", scale.get_value()))
        controls.append(self.volume_scale)
        deck.append(controls)
        return deck

    def _build_equalizer(self) -> Gtk.Box:
        equalizer = _box(Gtk.Orientation.VERTICAL, 2, "chrome-panel", "equalizer")
        title = self._panel_title("ЭКВАЛАЙЗЕР")
        title.get_first_child().set_hexpand(False)
        self.equalizer_hint = _label("", "title-account")
        self.equalizer_hint.set_hexpand(True)
        self.equalizer_hint.set_ellipsize(Pango.EllipsizeMode.END)
        title.append(self.equalizer_hint)
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
        equalizer.append(eq_body)
        self.set_equalizer_available(False)
        return equalizer

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
        if values is not None:
            self._apply_spectrum(values)
        return GLib.SOURCE_REMOVE

    def _apply_spectrum(self, values: tuple[float, ...]) -> None:
        if not self._spectrum_available:
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
            self.visual_area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw_spectrum(self, _area: Gtk.DrawingArea, context: cairo.Context, width: int, height: int) -> None:
        background = cairo.LinearGradient(0, 0, width, height)
        background.add_color_stop_rgb(0, 0.018, 0.045, 0.115)
        background.add_color_stop_rgb(0.55, 0.035, 0.09, 0.19)
        background.add_color_stop_rgb(1, 0.06, 0.06, 0.16)
        context.set_source(background)
        context.paint()
        levels = self._spectrum_levels
        if not any(levels) or width <= 1 or height <= 1:
            return
        palettes = []
        for reverse in (False, True):
            gradient = cairo.LinearGradient(width if reverse else 0, 0, 0 if reverse else width, height)
            gradient.add_color_stop_rgba(0.0, 0.05, 0.53, 1.0, 0.32)
            gradient.add_color_stop_rgba(0.32, 0.08, 0.94, 0.91, 0.72)
            gradient.add_color_stop_rgba(0.62, 0.40, 1.0, 0.53, 0.80)
            gradient.add_color_stop_rgba(1.0, 0.72, 0.31, 1.0, 0.38)
            palettes.append(gradient)
        for family in range(2):
            direction = -1 if family else 1
            for layer in range(9):
                # Geometry changes with measured bands only, never with a synthetic clock.
                for point in range(97):
                    x = point / 96
                    band = x * 31
                    low = min(30, int(band))
                    mix = band - low
                    magnitude = levels[low] * (1 - mix) + levels[low + 1] * mix
                    envelope = math.sin(math.pi * x) ** 0.55
                    wave = math.sin(x * math.tau * 1.25 + layer * 0.42 + family * 0.7)
                    crossing = direction * (x - 0.5) * 0.9
                    spread = (layer - 4) * 0.035
                    y = 0.5 + (crossing + wave * 0.31 + spread) * magnitude * envelope
                    if point == 0:
                        context.move_to(0, height * y)
                    else:
                        context.line_to(width * x, height * y)
                path = context.copy_path()
                context.set_line_width(6)
                context.set_source_rgba(0.12, 0.72 if family else 0.47, 0.92, 0.055)
                context.stroke()
                context.append_path(path)
                context.set_line_width(1.4)
                context.set_source(palettes[family])
                context.stroke()

    def _build_library(self) -> Gtk.Box:
        library = _box(Gtk.Orientation.VERTICAL, 3, "chrome-panel", "library-panel")
        library.set_vexpand(True)
        title = self._panel_title("МЕДИАТЕКА  •")
        self.heading_label = _label("Вся музыка здесь", "panel-title-text")
        self.heading_label.set_hexpand(True)
        self.heading_label.set_ellipsize(Pango.EllipsizeMode.END)
        title.append(self.heading_label)
        library.append(title)
        toolbar = _box(Gtk.Orientation.HORIZONTAL, 4, "library-toolbar")
        self.wave_button = self._nav_button("Моя волна", "wave")
        self.liked_button = self._nav_button("Любимые треки", "liked_tracks")
        self.playlists_button = self._nav_button("Плейлисты", "playlists")
        toolbar.append(self.wave_button)
        toolbar.append(self.liked_button)
        toolbar.append(self.playlists_button)
        toolbar.append(Gtk.Box(hexpand=True))
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Поиск музыки · Enter")
        self.search_entry.set_size_request(180, -1)
        self.search_entry.connect("activate", self._on_search)
        toolbar.append(self.search_entry)
        library.append(toolbar)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.add_css_class("list-well")
        library.append(self.stack)
        self.tracks_list = Gtk.ListBox()
        self.tracks_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.tracks_list.add_css_class("music-list")
        self.tracks_list.connect("row-activated", self._on_track_activated)
        self.track_empty = _label("Войдите, чтобы найти музыку или открыть свою медиатеку.", "empty-state", xalign=0.5)
        self.track_empty.set_wrap(True)
        self.tracks_list.set_placeholder(self.track_empty)
        track_page = _box(Gtk.Orientation.VERTICAL, 0)
        column_header = _box(Gtk.Orientation.HORIZONTAL, 8, "column-header")
        column_number = _label("№", "column-label", xalign=0.5)
        column_number.set_width_chars(4)
        column_header.append(column_number)
        column_title = _label("НАЗВАНИЕ / ИСПОЛНИТЕЛЬ", "column-label")
        column_title.set_hexpand(True)
        column_header.append(column_title)
        column_album = _label("АЛЬБОМ", "column-label")
        column_album.set_width_chars(16)
        column_album.set_max_width_chars(24)
        column_header.append(column_album)
        column_time = _label("ВРЕМЯ", "column-label", xalign=1)
        column_time.set_width_chars(5)
        column_header.append(column_time)
        column_end = _label("", "column-label")
        column_end.set_size_request(26, -1)
        column_header.append(column_end)
        track_page.append(column_header)
        track_scroll = Gtk.ScrolledWindow()
        track_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        track_scroll.set_vexpand(True)
        track_scroll.set_child(self.tracks_list)
        track_page.append(track_scroll)
        self.stack.add_named(track_page, "tracks")
        self.playlists_list = Gtk.ListBox()
        self.playlists_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.playlists_list.add_css_class("music-list")
        self.playlists_list.connect("row-activated", self._on_playlist_activated)
        playlist_empty = _label("Здесь пока нет плейлистов.", "empty-state", xalign=0.5)
        self.playlists_list.set_placeholder(playlist_empty)
        playlist_scroll = Gtk.ScrolledWindow()
        playlist_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        playlist_scroll.set_child(self.playlists_list)
        self.stack.add_named(playlist_scroll, "playlists")
        self.stack.set_visible_child_name("tracks")

        return library

    def _build_statusbar(self) -> Gtk.Box:
        status_bar = _box(Gtk.Orientation.HORIZONTAL, 6, "statusbar")
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        status_bar.append(self.spinner)
        self.status_label = _label("Готово", "status-text")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_hexpand(True)
        status_bar.append(self.status_label)
        status_bar.append(_label("IMPULS", "status-indicator"))
        return status_bar

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
            self.view_stack.set_visible_child_name("library")
            self.search_entry.grab_focus()
            return True
        if self._focus_is_editable():
            return False
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
            self._call("search", query)

    def _on_track_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        occurrence = self._track_rows.get(row)
        if occurrence is not None:
            self._call("play_track", *occurrence)

    def _on_playlist_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        playlist = self._playlist_rows.get(row)
        if playlist is not None:
            self._call("open_playlist", playlist)

    def _on_seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        self._call("seek", min(1.0, max(0.0, value)))
        return False

    def _on_account_clicked(self, _button: Gtk.Button) -> None:
        if self._account is not None:
            self._call("logout")
        else:
            self._open_login()

    def _modal(self, title: str, width: int = 440) -> tuple[Gtk.Window, Gtk.Box]:
        window = Gtk.Window(title=title, transient_for=self, modal=True, destroy_with_parent=True, default_width=width)
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

    @staticmethod
    def _clear_list(listbox: Gtk.ListBox) -> None:
        while (row := listbox.get_row_at_index(0)) is not None:
            listbox.remove(row)

    def set_tracks(self, tracks: list, heading: str) -> None:
        self.heading_label.set_label(heading)
        self.stack.set_visible_child_name("tracks")
        self._track_rows.clear()
        self._clear_list(self.tracks_list)
        self.track_empty.set_label("Треки не найдены.")
        for index, track in enumerate(tracks):
            row = Gtk.ListBoxRow()
            if index % 2:
                row.add_css_class("alternate")
            line = _box(Gtk.Orientation.HORIZONTAL, 8, "track-row")
            number = _label(f"{index + 1:02}", "track-number", xalign=0.5)
            number.set_width_chars(4)
            line.append(number)
            artists = ", ".join(track.artists) or "Неизвестный исполнитель"
            title = _label(f"{artists} — {track.title}", "track-title")
            title.set_hexpand(True)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            line.append(title)
            album = _label(str(track.album or ""), "track-album")
            album.set_ellipsize(Pango.EllipsizeMode.END)
            album.set_width_chars(16)
            album.set_max_width_chars(24)
            line.append(album)
            duration = _label(_format_time(track.duration_ms), "track-duration", xalign=1)
            duration.set_width_chars(5)
            line.append(duration)
            play = _icon_button("media-playback-start-symbolic", f"Воспроизвести: {track.title}", "row-play")
            play.set_valign(Gtk.Align.CENTER)
            play.connect("clicked", lambda _button, item=track, position=index: self._call("play_track", item, position))
            line.append(play)
            row.set_child(line)
            self._track_rows[row] = (track, index)
            self.tracks_list.append(row)

    def set_playlists(self, playlists: list) -> None:
        self.heading_label.set_label("Плейлисты")
        self.stack.set_visible_child_name("playlists")
        self._playlist_rows.clear()
        self._clear_list(self.playlists_list)
        for playlist in playlists:
            row = Gtk.ListBoxRow()
            line = _box(Gtk.Orientation.HORIZONTAL, 8, "track-row")
            badge = _label("♫", "playlist-badge", xalign=0.5)
            line.append(badge)
            title = _label(str(playlist.title), "track-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_hexpand(True)
            line.append(title)
            line.append(_label(f"Треков: {playlist.track_count}", "track-subtitle"))
            arrow = _label("›", "playlist-arrow", xalign=0.5)
            line.append(arrow)
            row.set_child(line)
            self._playlist_rows[row] = playlist
            self.playlists_list.append(row)

    def set_now_playing(self, track) -> None:
        self._current_track = track
        self.favorite_button.set_sensitive(track is not None)
        if track is None:
            self.now_title.set_label("Ничего не играет")
            self.now_artist.set_label("Выберите трек")
            self.visual_track_title.set_label("")
        else:
            self.now_title.set_label(str(track.title))
            artist = ", ".join(track.artists) or "Неизвестный исполнитель"
            self.now_artist.set_label(artist)
            self.visual_track_title.set_label(f"{artist} — {track.title}")

    def set_volume(self, fraction: float) -> None:
        value = min(1.0, max(0.0, fraction))
        if abs(self.volume_scale.get_value() - value) > 0.0001:
            self.volume_scale.set_value(value)

    def set_playback(self, playing: bool, position_ms: int = 0, duration_ms: int = 0) -> None:
        self.play_button.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        tooltip = "Пауза" if playing else "Воспроизвести"
        self.play_button.set_tooltip_text(tooltip)
        self.play_button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        self.position_label.set_label(_format_time(position_ms))
        self.duration_label.set_label(_format_time(duration_ms))
        self.seek_scale.set_sensitive(duration_ms > 0)
        self.seek_scale.set_value(min(1.0, max(0.0, position_ms / duration_ms)) if duration_ms > 0 else 0)

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
