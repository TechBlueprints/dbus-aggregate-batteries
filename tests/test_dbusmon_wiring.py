"""Tests of how dbusmon wires the driver to velib's DbusMonitor, and which signals it listens to.

The first suite runs against the recording stub (see driver_harness), so
constructing DbusMon touches no D-Bus at all and the constructor arguments can
be inspected. The dbusmon module itself is the real one: it is what is under
test here.

The suites after it run the real ext/velib_python/dbusmonitor.py on a fake bus
(driver_harness.FakeBusDaemon) that routes signals the way dbus-daemon and
dbus-python do, because what costs on a Cerbo is the BusItem signal receivers
the monitor registers: every signal a receiver matches wakes this process.
"""

import logging
import unittest

import driver_harness
from driver_harness import BUSITEM_INTERFACE, Battery, FakeBusDaemon, FakeDbusMon, pump, real_monitor


class DbusMonWiringTestCase(unittest.TestCase):
    def setUp(self):
        self.dbusmon = driver_harness.load_dbusmon()

    def test_callback_is_passed_as_value_changed_callback(self):
        def callback(service, path, options, changes, device_instance):  # pragma: no cover - never invoked
            return None

        monitor = self.dbusmon.DbusMon(value_changed_callback=callback).dbusmon
        self.assertIs(monitor.kwargs["valueChangedCallback"], callback)

    def test_aggregate_service_is_still_ignored(self):
        monitor = self.dbusmon.DbusMon(value_changed_callback=lambda *args: None).dbusmon
        self.assertEqual(monitor.kwargs["ignoreServices"], ["com.victronenergy.battery.aggregate"])

    def test_ignored_service_is_the_name_the_driver_publishes_under(self):
        """ignoreServices is the primary defence against our own publishes feeding back; it must name the right service."""
        driver = driver_harness.driver
        monitor = self.dbusmon.DbusMon().dbusmon
        self.assertIn(driver.AGGREGATE_SERVICE_NAME, monitor.kwargs["ignoreServices"])

    def test_callback_defaults_to_none(self):
        monitor = self.dbusmon.DbusMon().dbusmon
        self.assertIsNone(monitor.kwargs["valueChangedCallback"])

    def test_monitor_list_is_passed_positionally_and_covers_batteries(self):
        monitor = self.dbusmon.DbusMon().dbusmon
        self.assertIn("com.victronenergy.battery", monitor.monitorlist)
        self.assertIn("/Dc/0/Voltage", monitor.monitorlist["com.victronenergy.battery"])

    def test_driver_registers_its_reactive_callback_with_dbusmon(self):
        """_startMonitor must wire _on_input_changed through DbusMon into DbusMonitor."""
        driver = driver_harness.driver
        service = driver_harness.new_service()
        service._startMonitor()

        callback = driver_harness.RecordingDbusMonitor.last_instance.kwargs["valueChangedCallback"]
        self.assertEqual(callback.__func__, driver.DbusAggBatService._on_input_changed)
        self.assertIs(callback.__self__, service)


# ---------------------------------------------------------------------------
# The real DbusMonitor: which senders it listens to
# ---------------------------------------------------------------------------

PACK_A = "com.victronenergy.battery.ttyS5"
PACK_B = "com.victronenergy.battery.ttyS6"
LYNX = "com.victronenergy.battery.lynx_i2c"
"""in the battery class, but not one of the packs this configuration aggregates"""
AGGREGATE = "com.victronenergy.battery.aggregate"
VEBUS = "com.victronenergy.vebus.ttyS4"
SOLAR = "com.victronenergy.solarcharger.ttyS7"
SYSTEM = "com.victronenergy.system"
SETTINGS = "com.victronenergy.settings"
TEMPERATURE = "com.victronenergy.temperature.ruuvi_c7d3"
"""not in any class the monitor list covers"""

VOLTAGE = "/Dc/0/Voltage"


def pack(device_instance, voltage=13.3):
    return {"/DeviceInstance": device_instance, "/ProductName": "SerialBattery(JBD)", VOLTAGE: voltage, "/Soc": 80.0}


class RealMonitorTestCase(unittest.TestCase):
    """A bus shaped like prod's: two tracked packs, a battery-class bystander, our own service, systemcalc."""

    def setUp(self):
        self.daemon = FakeBusDaemon()
        self.daemon.start(PACK_A, pack(1))
        self.daemon.start(PACK_B, pack(2))
        self.daemon.start(LYNX, pack(3))
        self.daemon.start(AGGREGATE, pack(100))
        self.daemon.start(VEBUS, {"/DeviceInstance": 276, "/Connected": 1, "/Dc/0/Current": -2.0, "/ProductName": "MultiPlus"})
        self.daemon.start(SOLAR, {"/DeviceInstance": 278, "/Dc/0/Current": 4.0, "/ProductName": "SmartSolar"})
        self.daemon.start(SYSTEM, {"/DeviceInstance": 0, "/SystemState/LowSoc": 0})
        self.daemon.start(SETTINGS, {"/Settings/CGwacs/OvervoltageFeedIn": 1})
        self.daemon.start(TEMPERATURE, {"/DeviceInstance": 20, "/Temperature": 21.0})

        self.changes = []
        self.dbus_mon, self.glib = real_monitor(self, self.daemon, value_changed_callback=self._record)
        self.monitor = self.dbus_mon.dbusmon
        pump(self.glib)

    def _record(self, service, path, options, changes, device_instance):
        self.changes.append((service, path, changes["Value"]))

    # ----- helpers -----

    def receivers_by_name(self, name):
        """The BusItem receivers registered for ``name`` itself."""
        return [match for match in self.daemon.matches(BUSITEM_INTERFACE) if match.sender == name]

    def assertFollowed(self, name):
        receivers = self.receivers_by_name(name)
        self.assertEqual({"PropertiesChanged", "ItemsChanged"}, {match.member for match in receivers}, receivers)
        delivered = self.daemon.delivered_busitem_signals(name)
        self.assertTrue(delivered, "a signal from %s reaches no receiver" % name)
        self.assertEqual(set(receivers), set(delivered), "%s is reached by receivers that are not its own" % name)

    def assertNotListenedTo(self, name):
        self.assertEqual([], self.daemon.delivered_busitem_signals(name), "%s still wakes the monitor" % name)

    def follow_packs(self):
        self.monitor.follow_only([PACK_A, PACK_B])

    def change(self, name, value):
        self.changes = []
        self.daemon.properties_changed(name, VOLTAGE, value)
        pump(self.glib)
        return [change for change in self.changes if change[0] == name]


class NoReceiverWithoutSenderTestCase(RealMonitorTestCase):
    def test_no_busitem_receiver_without_a_sender_at_startup(self):
        unnamed = [match.rule for match in self.daemon.matches(BUSITEM_INTERFACE) if match.sender is None]
        self.assertEqual([], unnamed)

    def test_no_busitem_receiver_without_a_sender_once_narrowed(self):
        self.follow_packs()
        unnamed = [match.rule for match in self.daemon.matches(BUSITEM_INTERFACE) if match.sender is None]
        self.assertEqual([], unnamed)

    def test_name_owner_changed_receiver_is_kept(self):
        """Appearing and disappearing services are noticed through this org.freedesktop.DBus signal."""
        self.follow_packs()
        rules = [match.rule for match in self.daemon.matches() if match.member == "NameOwnerChanged"]
        self.assertIn("type='signal',member='NameOwnerChanged',arg0namespace='com.victronenergy'", rules)


class DiscoveryTestCase(RealMonitorTestCase):
    """Until the driver says which services it uses, the device search reads a cache as live as before."""

    def test_every_monitored_service_is_followed_by_name_until_told(self):
        for name in (PACK_A, PACK_B, LYNX, VEBUS, SOLAR, SYSTEM, SETTINGS):
            with self.subTest(name=name):
                self.assertFollowed(name)

    def test_a_service_outside_the_monitor_list_is_never_listened_to(self):
        self.assertNotListenedTo(TEMPERATURE)

    def test_candidate_values_stay_live_during_discovery(self):
        self.assertEqual([(LYNX, VOLTAGE, 13.9)], self.change(LYNX, 13.9))
        self.assertEqual(13.9, self.monitor.get_value(LYNX, VOLTAGE))

    def test_a_battery_appearing_during_discovery_is_scanned_and_followed(self):
        late = "com.victronenergy.battery.ttyUSB0"
        self.daemon.start(late, pack(4, voltage=12.9))
        pump(self.glib)
        self.assertIn(late, self.monitor.get_service_list("com.victronenergy.battery"))
        self.assertEqual(12.9, self.monitor.get_value(late, VOLTAGE))
        self.assertFollowed(late)


class NarrowedTestCase(RealMonitorTestCase):
    """After follow_only(), only the named services wake the process."""

    def test_tracked_packs_have_their_receivers(self):
        self.follow_packs()
        self.assertFollowed(PACK_A)
        self.assertFollowed(PACK_B)

    def test_untracked_services_have_none(self):
        self.follow_packs()
        for name in (LYNX, VEBUS, SOLAR, SYSTEM, SETTINGS, TEMPERATURE):
            with self.subTest(name=name):
                self.assertEqual([], self.receivers_by_name(name))
                self.assertNotListenedTo(name)

    def test_value_change_from_a_tracked_pack_reaches_the_callback(self):
        self.follow_packs()
        self.assertEqual([(PACK_A, VOLTAGE, 13.5)], self.change(PACK_A, 13.5))
        self.assertEqual(13.5, self.monitor.get_value(PACK_A, VOLTAGE))

    def test_items_changed_from_a_tracked_pack_reaches_the_callback(self):
        self.follow_packs()
        self.daemon.items_changed(PACK_B, {VOLTAGE: 13.1, "/Soc": 79.0})
        pump(self.glib)
        self.assertEqual({(PACK_B, VOLTAGE, 13.1), (PACK_B, "/Soc", 79.0)}, set(self.changes))

    def test_reading_an_unfollowed_service_returns_the_default_not_a_stale_value(self):
        """A value that stopped updating is worse than a missing one: the driver's read guards handle missing."""
        self.follow_packs()
        with self.assertLogs(level=logging.WARNING) as logs:
            self.assertIsNone(self.monitor.get_value(LYNX, VOLTAGE))
            self.assertEqual("fallback", self.monitor.get_value(LYNX, VOLTAGE, "fallback"))
        self.assertEqual(1, len(logs.records), "warned more than once for the same service")
        self.assertEqual(13.3, self.monitor.get_value(PACK_A, VOLTAGE))

    def test_following_more_later_subscribes_and_refreshes(self):
        self.monitor.follow_only([PACK_A])
        self.daemon.properties_changed(PACK_B, VOLTAGE, 12.5)  # nobody listens to it now
        pump(self.glib)
        self.follow_packs()
        self.assertFollowed(PACK_B)
        pump(self.glib)
        self.assertEqual(12.5, self.monitor.get_value(PACK_B, VOLTAGE))

    def test_a_service_that_cannot_be_followed_reads_as_absent_and_is_retried(self):
        """follow_only() runs inside _update(): it must not raise, and must not leave a stale cache readable."""
        self.monitor.follow_only([PACK_A])
        owner = self.daemon.owners.pop(PACK_B)  # gone, but its NameOwnerChanged not processed yet
        with self.assertLogs(level=logging.ERROR):
            self.follow_packs()
        self.assertEqual([], self.receivers_by_name(PACK_B))
        with self.assertLogs(level=logging.WARNING):
            self.assertIsNone(self.monitor.get_value(PACK_B, VOLTAGE))
        self.daemon.owners[PACK_B] = owner
        self.follow_packs()
        self.assertFollowed(PACK_B)
        self.assertEqual(13.3, self.monitor.get_value(PACK_B, VOLTAGE))

    def test_following_the_same_set_again_changes_nothing(self):
        self.follow_packs()
        before = list(self.daemon.matches())
        self.follow_packs()
        self.assertEqual(before, list(self.daemon.matches()))


class OwnerFollowingTestCase(RealMonitorTestCase):
    """A pack whose driver restarts gets a new unique name; the aggregate must follow it."""

    def test_restart_receivers_follow_the_new_owner(self):
        self.follow_packs()
        old_owner = self.daemon.owners[PACK_A]
        new_owner = self.daemon.restart(PACK_A)
        pump(self.glib)
        self.assertNotEqual(old_owner, new_owner)
        self.assertFollowed(PACK_A)
        self.assertEqual(new_owner, self.monitor.servicesByName[PACK_A].id)
        self.assertEqual([(PACK_A, VOLTAGE, 13.6)], self.change(PACK_A, 13.6))
        self.assertEqual(13.6, self.monitor.get_value(PACK_A, VOLTAGE))

    def test_restart_subscribes_before_the_rescan(self):
        """A change between the rescan's GetItems and the subscription would be lost until the next one."""
        self.follow_packs()
        connection = self.monitor.dbusConn
        start = len(connection.events)
        self.daemon.restart(PACK_A)
        pump(self.glib)
        events = connection.events[start:]
        subscribed = events.index(("add", "type='signal',sender='%s',interface='%s',member='PropertiesChanged'" % (PACK_A, BUSITEM_INTERFACE)))
        rescanned = events.index(("call", PACK_A, "/", "GetItems"))
        self.assertLess(subscribed, rescanned, events)

    def test_restarts_do_not_pile_up_receivers(self):
        self.follow_packs()
        for _ in range(3):
            self.daemon.restart(PACK_A)
            pump(self.glib)
        self.assertEqual(2, len(self.receivers_by_name(PACK_A)))


class AppearingAndDisappearingTestCase(RealMonitorTestCase):
    def test_a_tracked_pack_going_away_is_removed_with_its_receivers(self):
        self.follow_packs()
        self.daemon.stop(PACK_A)
        pump(self.glib)
        self.assertNotIn(PACK_A, self.monitor.get_service_list("com.victronenergy.battery"))
        self.assertIsNone(self.monitor.get_value(PACK_A, VOLTAGE))
        self.assertEqual([], self.receivers_by_name(PACK_A))

    def test_a_tracked_pack_coming_back_is_followed_again(self):
        self.follow_packs()
        self.daemon.stop(PACK_A)
        pump(self.glib)
        self.daemon.start(PACK_A, pack(1, voltage=13.2))
        pump(self.glib)
        self.assertIn(PACK_A, self.monitor.get_service_list("com.victronenergy.battery"))
        self.assertEqual(13.2, self.monitor.get_value(PACK_A, VOLTAGE))
        self.assertFollowed(PACK_A)
        self.assertEqual([(PACK_A, VOLTAGE, 13.0)], self.change(PACK_A, 13.0))

    def test_a_new_battery_after_narrowing_is_noticed_as_before_but_not_listened_to(self):
        self.follow_packs()
        late = "com.victronenergy.battery.ttyUSB0"
        self.daemon.start(late, pack(4))
        pump(self.glib)
        # noticed exactly as before: scanned into the monitor's service list
        self.assertIn(late, self.monitor.get_service_list("com.victronenergy.battery"))
        # but the driver does not aggregate it, so its signals no longer wake us
        self.assertNotListenedTo(late)
        self.assertEqual([], self.change(late, 12.0))

    def test_an_untracked_battery_going_away_is_removed(self):
        self.follow_packs()
        self.daemon.stop(LYNX)
        pump(self.glib)
        self.assertNotIn(LYNX, self.monitor.get_service_list("com.victronenergy.battery"))


class OwnServiceTestCase(RealMonitorTestCase):
    """Our own publishes must never come back as a recompute."""

    def test_aggregate_has_no_receiver_at_startup(self):
        self.assertEqual([], self.receivers_by_name(AGGREGATE))
        self.assertNotListenedTo(AGGREGATE)

    def test_aggregate_has_no_receiver_even_if_named(self):
        self.monitor.follow_only([PACK_A, PACK_B, AGGREGATE])
        self.assertEqual([], self.receivers_by_name(AGGREGATE))
        self.assertNotListenedTo(AGGREGATE)

    def test_aggregate_publishes_do_not_reach_the_callback(self):
        self.daemon.properties_changed(AGGREGATE, VOLTAGE, 26.7)
        pump(self.glib)
        self.assertEqual([], [change for change in self.changes if change[0] == AGGREGATE])


# ---------------------------------------------------------------------------
# The driver: it must follow every service it reads from the cache
# ---------------------------------------------------------------------------

MULTI = VEBUS
SHUNT = "com.victronenergy.battery.ttyS9"
DCLOAD_SHUNT = "com.victronenergy.dcload.ttyS10"


class DriverFollowsWhatItReadsTestCase(driver_harness.DriverTestCase):
    """_update() tells the monitor which services to follow; each one it reads must be among them."""

    def run_update(self, service, extra_values=None):
        values = dict(service._dbusMon.dbusmon._values)
        values.update(extra_values or {})
        service._dbusMon = FakeDbusMon(values)
        service._update()
        monitor = service._dbusMon.dbusmon
        self.assertIsNotNone(monitor.followed, "the driver never told the monitor what to follow")
        self.assertLessEqual(monitor.read_services, monitor.followed, "read from the cache without following it")
        return monitor

    def test_prod_config_follows_only_the_packs(self):
        batteries = [Battery("A"), Battery("B")]
        monitor = self.run_update(self.make_service(batteries))
        self.assertEqual({battery.service for battery in batteries}, monitor.followed)

    def test_current_from_victron_follows_multi_mppts_and_shunts(self):
        batteries = [Battery("A"), Battery("B")]
        service = self.make_service(batteries, CURRENT_FROM_VICTRON=True, NR_OF_MPPTS=1)
        service._multi = MULTI
        service._mppts_list = [SOLAR]
        service._smartShunt_list = [SHUNT, DCLOAD_SHUNT]
        service._num_battery_shunts = 1
        service._multi_connected = True
        monitor = self.run_update(
            service,
            {
                (MULTI, "/Connected"): 1,
                (MULTI, "/Dc/0/Current"): -2.0,
                (SOLAR, "/Dc/0/Current"): 4.0,
                (SHUNT, "/Dc/0/Current"): 1.0,
                (DCLOAD_SHUNT, "/Dc/0/Current"): 0.5,
            },
        )
        self.assertTrue({MULTI, SOLAR, SHUNT, DCLOAD_SHUNT} <= monitor.read_services, monitor.read_services)

    def test_own_charge_parameters_follows_settings(self):
        """Dynamic CVL reads /Settings/CGwacs/OvervoltageFeedIn from the cache when it starts."""
        batteries = [Battery("A", max_cell_voltage=3.7), Battery("B")]
        service = self.make_service(batteries, OWN_CHARGE_PARAMETERS=True)
        monitor = self.run_update(service, {(SETTINGS, "/Settings/CGwacs/OvervoltageFeedIn"): 1})
        self.assertIn(SETTINGS, monitor.read_services)

    def test_own_soc_follows_system(self):
        batteries = [Battery("A"), Battery("B")]
        service = self.make_service(batteries, OWN_SOC=True)
        monitor = self.run_update(service, {(SYSTEM, "/SystemState/LowSoc"): 0})
        self.assertIn(SYSTEM, monitor.read_services)


if __name__ == "__main__":
    unittest.main()
