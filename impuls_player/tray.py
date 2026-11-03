"""GTK-independent StatusNotifierItem and DBusMenu on the session bus."""
from gi.repository import Gio, GLib


APP_ID = 'io.github.AnoY606_git.Impuls'
ITEM = 'org.kde.StatusNotifierItem'
MENU = 'com.canonical.dbusmenu'
WATCHER = 'org.kde.StatusNotifierWatcher'
ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/Menu'
WATCHER_PATH = '/StatusNotifierWatcher'

XML = '''<node>
<interface name="org.kde.StatusNotifierItem">
 <property name="Category" type="s" access="read"/>
 <property name="Id" type="s" access="read"/>
 <property name="Title" type="s" access="read"/>
 <property name="Status" type="s" access="read"/>
 <property name="WindowId" type="i" access="read"/>
 <property name="IconName" type="s" access="read"/>
 <property name="IconThemePath" type="s" access="read"/>
 <property name="IconPixmap" type="a(iiay)" access="read"/>
 <property name="OverlayIconName" type="s" access="read"/>
 <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
 <property name="AttentionIconName" type="s" access="read"/>
 <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
 <property name="AttentionMovieName" type="s" access="read"/>
 <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
 <property name="ItemIsMenu" type="b" access="read"/>
 <property name="Menu" type="o" access="read"/>
 <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
 <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
 <signal name="NewStatus"><arg type="s"/></signal>
</interface>
<interface name="com.canonical.dbusmenu">
 <property name="Version" type="u" access="read"/>
 <property name="TextDirection" type="s" access="read"/>
 <property name="Status" type="s" access="read"/>
 <property name="IconThemePath" type="as" access="read"/>
 <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
  <arg type="as" direction="in"/><arg type="u" direction="out"/>
  <arg type="(ia{sv}av)" direction="out"/></method>
 <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
  <arg type="a(ia{sv})" direction="out"/></method>
 <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
  <arg type="v" direction="out"/></method>
 <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
  <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
 <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
 <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
 <method name="AboutToShowGroup"><arg type="ai" direction="in"/>
  <arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
 <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
 <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
</interface></node>'''


def _text(value):
    return str(value or '').replace('\x00', '').encode('utf-8', 'replace').decode('utf-8')


class Tray:
    """Optional tray service. Availability means a registered, running host.

    Desktop hosts render the menu themselves. No GTK3/AppIndicator dependency
    is loaded into the GTK4 process; a missing host leaves normal close intact.
    """
    def __init__(self, controller, on_show, on_quit, on_available):
        self._controller = controller
        self._on_show, self._on_quit = on_show, on_quit
        self._on_available = on_available
        self.available = False
        self._connection = None
        self._registrations = []
        self._watch_id = 0
        self._closed_id = 0
        self._proxy = None
        self._proxy_handlers = []
        self._registered = False
        self._host = False
        self._generation = 0
        self._revision = 1
        self._track_label = 'Нет воспроизведения'
        self._has_track = False
        self._playing = False
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for interface in Gio.DBusNodeInfo.new_for_xml(XML).interfaces:
                path = ITEM_PATH if interface.name == ITEM else MENU_PATH
                registration = self._connection.register_object(
                    path, interface, self._on_method_call, self._on_get_property, None)
                if not registration:
                    raise RuntimeError('Could not register tray object')
                self._registrations.append(registration)
            self._closed_id = self._connection.connect('closed', self._bus_closed)
            self._watch_id = Gio.bus_watch_name_on_connection(
                self._connection, WATCHER, Gio.BusNameWatcherFlags.NONE,
                self._watcher_appeared, self._watcher_vanished)
        except (GLib.Error, RuntimeError):
            self.close()

    def _set_available(self, value):
        value = bool(value)
        if value != self.available:
            self.available = value
            self._on_available(value)

    def _clear_proxy(self):
        self._generation += 1
        if self._proxy is not None:
            for handler in self._proxy_handlers:
                self._proxy.disconnect(handler)
        self._proxy_handlers.clear()
        self._proxy = None
        self._registered = self._host = False
        self._set_available(False)

    def _watcher_appeared(self, connection, name, owner):
        self._clear_proxy()
        generation = self._generation

        def ready(source, result):
            try:
                proxy = Gio.DBusProxy.new_finish(result)
            except GLib.Error:
                return
            if generation != self._generation or self._connection is None:
                return
            self._proxy = proxy
            self._proxy_handlers = [proxy.connect('g-properties-changed', self._properties_changed),
                                    proxy.connect('g-signal', self._watcher_signal)]
            value = proxy.get_cached_property('IsStatusNotifierHostRegistered')
            self._host = value is not None and bool(value.unpack())
            proxy.call('RegisterStatusNotifierItem', GLib.Variant('(s)', (ITEM_PATH,)),
                       Gio.DBusCallFlags.NONE, 3000, None, registered)

        def registered(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error:
                return
            if generation == self._generation and proxy == self._proxy:
                self._registered = True
                self._set_available(self._host)

        Gio.DBusProxy.new(connection, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                         None, owner, WATCHER_PATH, WATCHER, None, ready)

    def _watcher_vanished(self, *_args):
        self._clear_proxy()

    def _properties_changed(self, proxy, changed, invalidated):
        if proxy != self._proxy:
            return
        values = changed.unpack()
        if 'IsStatusNotifierHostRegistered' in values:
            self._host = bool(values['IsStatusNotifierHostRegistered'])
        elif 'IsStatusNotifierHostRegistered' in invalidated:
            self._host = False
        self._set_available(self._registered and self._host)

    def _watcher_signal(self, proxy, sender, signal, parameters):
        if proxy == self._proxy and signal in ('StatusNotifierHostRegistered', 'StatusNotifierHostUnregistered'):
            self._host = signal == 'StatusNotifierHostRegistered'
            self._set_available(self._registered and self._host)

    def _bus_closed(self, *_args):
        self._clear_proxy()

    def close(self):
        """Release exported objects and watchers; safe after partial startup."""
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        self._clear_proxy()
        if self._connection is not None:
            if self._closed_id:
                self._connection.disconnect(self._closed_id)
            for registration in self._registrations:
                self._connection.unregister_object(registration)
        self._closed_id = 0
        self._registrations.clear()
        self._connection = None

    def set_playback(self, track, playing):
        title = _text(getattr(track, 'title', ''))
        artists = ', '.join(_text(value) for value in getattr(track, 'artists', ()))
        label = f'{artists} — {title}' if artists else title
        state = (label if track is not None else 'Нет воспроизведения', track is not None,
                 bool(playing) if track is not None else False)
        if state == (self._track_label, self._has_track, self._playing):
            return
        previous = self._rows()
        self._track_label, self._has_track, self._playing = state
        rows = self._rows()
        updates = [(item, props) for item, props in rows.items() if props != previous.get(item)]
        self._revision = self._revision % (2**32 - 1) + 1
        self._emit(MENU_PATH, MENU, 'ItemsPropertiesUpdated', '(a(ia{sv})a(ias))', (updates, []))
        self._emit(MENU_PATH, MENU, 'LayoutUpdated', '(ui)', (self._revision, 0))
        self._emit(ITEM_PATH, ITEM, 'NewToolTip', '()', ())

    def _emit(self, path, interface, name, signature, values):
        if self._connection is not None:
            try:
                self._connection.emit_signal(None, path, interface, name, GLib.Variant(signature, values))
            except GLib.Error:
                self._set_available(False)

    def _rows(self):
        def row(label, enabled=True, icon=None):
            props = {'label': GLib.Variant('s', label), 'enabled': GLib.Variant('b', enabled),
                     'visible': GLib.Variant('b', True)}
            if icon:
                props['icon-name'] = GLib.Variant('s', icon)
            return props
        separator = {'type': GLib.Variant('s', 'separator')}
        return {
            0: {'children-display': GLib.Variant('s', 'submenu')},
            1: row('Показать окно', icon='view-restore-symbolic'),
            2: row(self._track_label.replace('_', '__'), False), 3: separator,
            4: row('Предыдущий трек', self._has_track, 'media-skip-backward-symbolic'),
            5: row('Пауза' if self._playing else 'Воспроизвести', self._has_track,
                   'media-playback-pause-symbolic' if self._playing else 'media-playback-start-symbolic'),
            6: row('Следующий трек', self._has_track, 'media-skip-forward-symbolic'),
            7: separator, 8: row('Выход', icon='application-exit-symbolic'),
        }

    def _layout(self, parent, depth, names):
        rows = self._rows()
        if parent not in rows:
            raise ValueError('Unknown menu item')
        props = {name: value for name, value in rows[parent].items() if not names or name in names}
        children = []
        if parent == 0 and depth != 0:
            children = [GLib.Variant('(ia{sv}av)', self._layout(item, 0, names)) for item in rows if item]
        return parent, props, children

    def _event(self, item, event):
        rows = self._rows()
        if item not in rows:
            return False
        enabled = rows[item].get('enabled')
        if event == 'clicked' and enabled is not None and enabled.unpack():
            callbacks = {1: self._on_show, 4: self._controller.previous_track,
                         5: self._controller.toggle_playback, 6: self._controller.next_track,
                         8: self._on_quit}
            callback = callbacks.get(item)
            if callback:
                callback()
        return True

    def _on_get_property(self, connection, sender, path, interface, name):
        if interface == ITEM:
            values = {'Category': ('s', 'ApplicationStatus'), 'Id': ('s', APP_ID),
                      'Title': ('s', 'Impuls'), 'Status': ('s', 'Active'), 'WindowId': ('i', 0),
                      'IconName': ('s', APP_ID), 'IconThemePath': ('s', ''),
                      'IconPixmap': ('a(iiay)', []), 'OverlayIconName': ('s', ''),
                      'OverlayIconPixmap': ('a(iiay)', []), 'AttentionIconName': ('s', ''),
                      'AttentionIconPixmap': ('a(iiay)', []), 'AttentionMovieName': ('s', ''),
                      'ToolTip': ('(sa(iiay)ss)', (APP_ID, [], 'Impuls', self._track_label)),
                      'ItemIsMenu': ('b', False), 'Menu': ('o', MENU_PATH)}
        elif interface == MENU:
            values = {'Version': ('u', 3), 'TextDirection': ('s', 'ltr'),
                      'Status': ('s', 'normal'), 'IconThemePath': ('as', [])}
        else:
            return None
        value = values.get(name)
        return GLib.Variant(*value) if value is not None else None

    def _on_method_call(self, connection, sender, path, interface, method, parameters, invocation):
        try:
            values = parameters.unpack()
            reply = GLib.Variant('()', ())
            if interface == ITEM:
                if method == 'Activate':
                    self._on_show()
                elif method == 'SecondaryActivate':
                    self._event(5, 'clicked')
                elif method == 'ContextMenu':
                    self._emit(MENU_PATH, MENU, 'ItemActivationRequested', '(iu)', (0, 0))
                elif method != 'Scroll':
                    raise NotImplementedError
            elif interface == MENU:
                rows = self._rows()
                if method == 'GetLayout':
                    reply = GLib.Variant('(u(ia{sv}av))', (self._revision, self._layout(*values)))
                elif method == 'GetGroupProperties':
                    ids, names = values
                    ids = ids or rows
                    reply = GLib.Variant('(a(ia{sv}))', ([(item, {key: value for key, value in rows[item].items()
                                                                if not names or key in names})
                                                         for item in ids if item in rows],))
                elif method == 'GetProperty':
                    reply = GLib.Variant('(v)', (rows[values[0]][values[1]],))
                elif method == 'Event':
                    self._event(values[0], values[1])
                elif method == 'EventGroup':
                    reply = GLib.Variant('(ai)', ([event[0] for event in values[0]
                                                  if not self._event(event[0], event[1])],))
                elif method == 'AboutToShow':
                    reply = GLib.Variant('(b)', (False,))
                elif method == 'AboutToShowGroup':
                    reply = GLib.Variant('(aiai)', ([], [item for item in values[0] if item not in rows]))
                else:
                    raise NotImplementedError
            else:
                raise NotImplementedError
            invocation.return_value(reply)
        except NotImplementedError:
            invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod', 'Unknown method')
        except (ValueError, TypeError, KeyError, IndexError):
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs', 'Invalid menu arguments')
        except Exception:
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', 'Player action failed')
