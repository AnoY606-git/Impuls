"""StatusNotifierItem and DBusMenu contracts, without a desktop or account."""
import os
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import Gio, GLib

from impuls_player.tray import Tray


class Controller:
    def __init__(self):
        self.calls = []

    def previous_track(self): self.calls.append('previous')
    def toggle_playback(self): self.calls.append('toggle')
    def next_track(self): self.calls.append('next')


class Reply:
    def __init__(self): self.value = self.error = None
    def return_value(self, value): self.value = value
    def return_dbus_error(self, name, message): self.error = (name, message)


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.shown = []
        self.quits = []
        self.availability = []
        with patch('impuls_player.tray.Gio.bus_get_sync', side_effect=GLib.Error('no bus')):
            self.tray = Tray(self.controller, lambda: self.shown.append(True),
                             lambda: self.quits.append(True), self.availability.append)
        self.addCleanup(self.tray.close)

    def call(self, method, signature='()', values=(), interface='com.canonical.dbusmenu'):
        reply = Reply()
        self.tray._on_method_call(None, None, None, interface, method,
                                  GLib.Variant(signature, values), reply)
        self.assertIsNone(reply.error)
        return reply.value

    def rows(self, depth=-1, names=()):
        result = self.call('GetLayout', '(iias)', (0, depth, names))
        self.assertEqual(result.get_type_string(), '(u(ia{sv}av))')
        return {row[0]: row[1] for row in result.unpack()[1][2]}

    def click(self, item):
        self.call('Event', '(isvu)', (item, 'clicked', GLib.Variant('s', ''), 0))

    def test_unavailable_bus_is_safe_and_playback_controls_are_disabled(self):
        self.assertFalse(self.tray.available)
        rows = self.rows()
        self.assertFalse(rows[5]['enabled'])
        self.click(5)
        self.click(4)
        self.click(6)
        self.assertEqual(self.controller.calls, [])
        self.click(1)
        self.click(8)
        self.assertEqual(self.shown, [True])
        self.assertEqual(self.quits, [True])

    def test_menu_follows_track_and_playback_and_dispatches_clicks(self):
        track = SimpleNamespace(title='A_song', artists=('Исполнитель',))
        self.tray.set_playback(track, True)
        rows = self.rows()
        self.assertIn('A__song', rows[2]['label'])
        self.assertIn('Исполнитель', rows[2]['label'])
        self.assertTrue(rows[5]['enabled'])
        self.assertEqual(rows[5]['label'], 'Пауза')
        for item in (4, 5, 6): self.click(item)
        self.assertEqual(self.controller.calls, ['previous', 'toggle', 'next'])
        self.tray.set_playback(track, False)
        self.assertEqual(self.rows()[5]['label'], 'Воспроизвести')

    def test_layout_depth_and_property_filters_and_grouped_events(self):
        self.assertEqual(self.rows(0), {})
        self.assertEqual(set(self.rows(1, ('label',))[5]), {'label'})
        result = self.call('GetGroupProperties', '(aias)', ([1, 999], ['label']))
        self.assertEqual(result.get_type_string(), '(a(ia{sv}))')
        self.assertEqual(result.unpack(), ([(1, {'label': 'Показать окно'})],))
        events = [(1, 'hovered', GLib.Variant('s', ''), 0),
                  (1, 'clicked', GLib.Variant('s', ''), 0),
                  (999, 'clicked', GLib.Variant('s', ''), 0)]
        self.assertEqual(self.call('EventGroup', '(a(isvu))', (events,)).unpack(), ([999],))
        self.assertEqual(self.shown, [True])

    def test_empty_group_ids_return_every_item_with_requested_properties(self):
        all_props = self.call('GetGroupProperties', '(aias)', ([], [])).unpack()[0]
        self.assertEqual([item for item, _ in all_props], list(range(9)))
        self.assertEqual(dict(all_props)[0]['children-display'], 'submenu')
        self.assertEqual(dict(all_props)[5]['enabled'], False)
        filtered = self.call('GetGroupProperties', '(aias)', ([], ['label'])).unpack()[0]
        self.assertEqual([item for item, _ in filtered], list(range(9)))
        self.assertEqual(dict(filtered)[5], {'label': 'Воспроизвести'})
        self.assertEqual(dict(filtered)[3], {})

    def test_item_activation_restores_window_and_secondary_activation_controls_playback(self):
        self.call('Activate', '(ii)', (0, 0), 'org.kde.StatusNotifierItem')
        self.assertEqual(self.shown, [True])
        self.call('SecondaryActivate', '(ii)', (0, 0), 'org.kde.StatusNotifierItem')
        self.assertEqual(self.controller.calls, [])
        self.tray.set_playback(SimpleNamespace(title='Song', artists=()), False)
        self.call('SecondaryActivate', '(ii)', (0, 0), 'org.kde.StatusNotifierItem')
        self.assertEqual(self.controller.calls, ['toggle'])

    def test_state_changes_emit_menu_and_tooltip_updates_once(self):
        class Connection:
            def __init__(self): self.signals = []
            def emit_signal(self, *args): self.signals.append(args[3])
        connection = Connection()
        self.tray._connection = connection
        track = SimpleNamespace(title='Song', artists=())
        self.tray.set_playback(track, True)
        self.assertIn('ItemsPropertiesUpdated', connection.signals)
        self.assertIn('NewToolTip', connection.signals)
        count = len(connection.signals)
        self.tray.set_playback(track, True)
        self.assertEqual(len(connection.signals), count)
        self.tray._connection = None

    def test_close_clears_availability_and_is_idempotent(self):
        self.tray._set_available(True)
        self.tray.close()
        self.tray.close()
        self.assertFalse(self.tray.available)
        self.assertEqual(self.availability, [True, False])


WATCHER_XML = '''<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
<property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
<signal name="StatusNotifierHostRegistered"/>
<signal name="StatusNotifierHostUnregistered"/>
</interface></node>'''


@unittest.skipUnless(os.environ.get('IMPULS_TEST_DBUS') == '1', 'requires isolated session bus')
class TrayBusTests(unittest.TestCase):
    def setUp(self):
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.host = True
        self.registered = []
        self.events = []
        self.controller = Controller()
        interface = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        self.registration = self.connection.register_object('/StatusNotifierWatcher', interface,
                                                           self.register_item, self.get_property, None)
        self.set_name(True)
        self.tray = Tray(self.controller, lambda: None, lambda: self.events.append('quit'),
                         self.events.append)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.tray.close()
        self.set_name(False)
        self.connection.unregister_object(self.registration)
        self.pump(lambda: True)

    def set_name(self, own):
        method = 'RequestName' if own else 'ReleaseName'
        signature = '(su)' if own else '(s)'
        values = ('org.kde.StatusNotifierWatcher', 0) if own else ('org.kde.StatusNotifierWatcher',)
        self.connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                  'org.freedesktop.DBus', method, GLib.Variant(signature, values),
                                  None, Gio.DBusCallFlags.NONE, 2000, None)

    def register_item(self, connection, sender, path, interface, method, parameters, invocation):
        self.registered.append((sender, parameters.unpack()[0]))
        invocation.return_value(GLib.Variant('()', ()))

    def get_property(self, *args):
        return GLib.Variant('b', self.host)

    def pump(self, condition):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            while context.pending(): context.iteration(False)
            if condition(): return
            time.sleep(.001)
        self.fail('timed out waiting for D-Bus state')

    def bus_call(self, path, method, *args):
        process = subprocess.Popen(['gdbus', 'call', '--session', '--dest',
                                    self.connection.get_unique_name(), '--object-path', path,
                                    '--method', method, '--', *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.pump(lambda: process.poll() is not None)
        finally:
            if process.poll() is None: process.kill()
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        return stdout

    def test_registration_export_menu_commands_and_watcher_restart(self):
        self.pump(lambda: self.tray.available)
        self.assertEqual(self.registered[0][1], '/StatusNotifierItem')
        properties = self.bus_call('/StatusNotifierItem', 'org.freedesktop.DBus.Properties.GetAll',
                                   'org.kde.StatusNotifierItem')
        self.assertIn('/Menu', properties)
        self.assertIn('io.github.AnoY606_git.Impuls', properties)
        self.tray.set_playback(SimpleNamespace(title='Tray Song', artists=('Artist',)), True)
        layout = self.bus_call('/Menu', 'com.canonical.dbusmenu.GetLayout', '0', '-1', '[]')
        self.assertIn('Tray Song', layout)
        self.bus_call('/Menu', 'com.canonical.dbusmenu.Event', '5', 'clicked', "<''>", '0')
        self.assertEqual(self.controller.calls, ['toggle'])
        self.set_name(False)
        self.pump(lambda: not self.tray.available)
        self.set_name(True)
        self.pump(lambda: self.tray.available and len(self.registered) == 2)

    def test_existing_watcher_without_host_does_not_enable_hiding(self):
        self.host = False
        self.pump(lambda: bool(self.registered))
        self.assertFalse(self.tray.available)
        self.host = True
        self.connection.emit_signal(None, '/StatusNotifierWatcher', 'org.kde.StatusNotifierWatcher',
                                    'StatusNotifierHostRegistered', GLib.Variant('()', ()))
        self.pump(lambda: self.tray.available)
        self.host = False
        self.connection.emit_signal(None, '/StatusNotifierWatcher', 'org.kde.StatusNotifierWatcher',
                                    'StatusNotifierHostUnregistered', GLib.Variant('()', ()))
        self.pump(lambda: not self.tray.available)
