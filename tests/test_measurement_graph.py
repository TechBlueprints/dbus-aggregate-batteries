#!/usr/bin/env python3

"""
Unit tests for the measurement-graph declaration published by the aggregate
battery service.

Two D-Bus paths are under test:

* ``/Measurement/Kind`` - the constant string ``derived``, telling summing
  consumers that this service re-publishes values rolled up from other
  services and must not be counted alongside them.
* ``/Measurement/TracksServices`` - created empty and filled once the battery
  search completes, with the discovered battery service names joined by commas
  in sorted order and without spaces. Consumers parse this string, so the exact
  format is part of the contract.

The battery search can finish at three different places, and each of them has
to publish the constituent list, otherwise a system that took that route would
be left advertising an empty declaration:

* ``_find_batteries``  - current comes from the BMSes, no Victron devices are
  searched at all.
* ``_find_multis``     - a MultiPlus/Quattro is used but no MPPTs are configured.
* ``_find_mppts``      - MPPTs are configured and the expected number was found.

``dbus``, ``vedbus``, ``dbusmon`` and ``gi``/``GLib`` are not importable off the
Venus device, and ``settings`` exits the interpreter when no ``config.ini`` is
present, so all of them are stubbed in ``sys.modules`` before the driver is
loaded. The driver file name contains dashes and therefore cannot be imported
by name; it is loaded through ``importlib.util.spec_from_file_location``.

The service object is never built through ``__init__`` (that one opens a real
bus). Tests use ``object.__new__`` and set only the attributes the methods
under test actually read.
"""

import ast
import importlib.util
import logging
import os
import sys
import types
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_DRIVER_PATH = os.path.join(_REPO_ROOT, "dbus-aggregate-batteries.py")
_DRIVER_MODULE_NAME = "dbus_aggregate_batteries_driver"

KIND_PATH = "/Measurement/Kind"
TRACKS_PATH = "/Measurement/TracksServices"


# ---------------------------------------------------------------------------
# Stubs for the modules that only exist on a Venus OS device
# ---------------------------------------------------------------------------


class FakeGLib:
    """Stand-in for ``gi.repository.GLib`` recording scheduled callbacks."""

    timeouts = []

    @classmethod
    def reset(cls):
        cls.timeouts = []

    @classmethod
    def timeout_add_seconds(cls, interval, callback, *args):
        cls.timeouts.append((interval, callback))
        return len(cls.timeouts)

    @classmethod
    def idle_add(cls, callback, *args):
        cls.timeouts.append(("idle", callback))
        return len(cls.timeouts)


class Unusable:
    """Placeholder for symbols the driver imports but tests must never call."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("real D-Bus objects must not be constructed in unit tests")


_DEFAULT_SETTINGS = {
    "BATTERY_SERVICE_NAME": "com.victronenergy.battery",
    "DCLOAD_SERVICE_NAME": "com.victronenergy.dcload",
    "BATTERY_PRODUCT_NAME_PATH": "/ProductName",
    "BATTERY_PRODUCT_NAME": "SerialBattery",
    "BATTERY_INSTANCE_NAME_PATH": "/CustomName",
    "SMARTSHUNT_INSTANCE_NAME_PATH": "/CustomName",
    "SMARTSHUNT_NAME_KEYWORD": "SmartShunt",
    "USE_SMARTSHUNTS": False,
    "SEND_CELL_VOLTAGES": 0,
    "NR_OF_CELLS_PER_BATTERY": 16,
    "CAN_batteries": False,
    "NR_OF_BATTERIES": 2,
    "CURRENT_FROM_VICTRON": False,
    "MULTI_KEYWORD": "com.victronenergy.vebus",
    "NR_OF_MPPTS": 0,
    "MPPT_KEYWORD": "com.victronenergy.solarcharger",
    "SEARCH_TRIALS": 10,
    "UPDATE_INTERVAL_DATA": 1000,
    "UPDATE_INTERVAL_FIND_DEVICES": 3,
    "TIME_BEFORE_RESTART": 0,
    "OWN_CHARGE_PARAMETERS": False,
}


def _install_stubs():
    """Register the unavailable modules in ``sys.modules``; return the settings stub."""
    gi_module = types.ModuleType("gi")
    repository_module = types.ModuleType("gi.repository")
    repository_module.GLib = FakeGLib
    gi_module.repository = repository_module
    sys.modules.setdefault("gi", gi_module)
    sys.modules.setdefault("gi.repository", repository_module)

    dbus_module = types.ModuleType("dbus")
    dbus_module.SystemBus = Unusable
    dbus_module.SessionBus = Unusable
    sys.modules.setdefault("dbus", dbus_module)

    vedbus_module = types.ModuleType("vedbus")
    vedbus_module.VeDbusService = Unusable
    vedbus_module.VeDbusItemImport = Unusable
    sys.modules.setdefault("vedbus", vedbus_module)

    dbusmon_module = types.ModuleType("dbusmon")
    dbusmon_module.DbusMon = Unusable
    sys.modules.setdefault("dbusmon", dbusmon_module)

    sys.modules.setdefault("settings", types.ModuleType("settings"))
    settings_module = sys.modules["settings"]
    _reset_settings(settings_module)
    return settings_module


def _reset_settings(settings_module):
    for name, value in _DEFAULT_SETTINGS.items():
        setattr(settings_module, name, value)


def _load_driver():
    """Load the dash-named driver file as an importable module object."""
    if _DRIVER_MODULE_NAME in sys.modules:
        return sys.modules[_DRIVER_MODULE_NAME]
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    spec = importlib.util.spec_from_file_location(_DRIVER_MODULE_NAME, _DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DRIVER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


SETTINGS = _install_stubs()
driver = _load_driver()


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeDbusService:
    """Records every ``add_path`` and every ``__setitem__`` performed on it."""

    def __init__(self):
        self.values = {}
        self.add_path_calls = []
        self.set_calls = []

    def add_path(self, path, value, **kwargs):
        self.add_path_calls.append((path, value, kwargs))
        self.values[path] = value

    def __setitem__(self, path, value):
        self.set_calls.append((path, value))
        self.values[path] = value

    def __getitem__(self, path):
        return self.values[path]

    def writes_to(self, path):
        """Every value written to ``path`` through ``__setitem__``, in order."""
        return [value for written_path, value in self.set_calls if written_path == path]


class FakeBus:
    def __init__(self, names):
        self._names = list(names)

    def list_names(self):
        return list(self._names)


class FakeDbusMonitor:
    def __init__(self, values):
        self._values = dict(values)

    def get_value(self, service, path):
        return self._values.get((service, path))


class FakeDbusMon:
    def __init__(self, values):
        self.dbusmon = FakeDbusMonitor(values)


def make_service(batteries=None, bus_names=(), monitor_values=None):
    """Build a DbusAggBatService without running its D-Bus touching __init__."""
    service = object.__new__(driver.DbusAggBatService)
    service._dbusservice = FakeDbusService()
    service._dbusConn = FakeBus(bus_names)
    service._dbusMon = FakeDbusMon(monitor_values or {})
    service._batteries_dict = dict(batteries or {})
    service._smartShunt_list = []
    service._mppts_list = []
    service._num_battery_shunts = 0
    service._multi = None
    service._searchTrials = 1
    service._timeOld = 0.0
    service._ownCharge = 0.5
    service._reactive_ready = False
    service._updating = False
    return service


def battery_monitor_values(services_by_name, product_name="SerialBattery(Jkbms)", cells=16):
    """Monitor answers that make ``_find_batteries`` accept every given service."""
    values = {}
    for name, service in services_by_name.items():
        values[(service, "/ProductName")] = product_name
        values[(service, "/CustomName")] = name
        values[(service, "/System/NrOfCellsPerBattery")] = cells
    return values


# ---------------------------------------------------------------------------
# Source-level assertions about the declaration itself
# ---------------------------------------------------------------------------


def _parse_driver():
    with open(_DRIVER_PATH, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_DRIVER_PATH)


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return _NON_LITERAL


_NON_LITERAL = object()


def measurement_add_path_calls():
    """Every ``add_path`` call for a ``/Measurement/...`` path, as (path, value, kwargs)."""
    calls = []
    for node in ast.walk(_parse_driver()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_path" or not node.args:
            continue
        path = _literal(node.args[0])
        if not isinstance(path, str) or not path.startswith("/Measurement/"):
            continue
        value = _literal(node.args[1]) if len(node.args) > 1 else _NON_LITERAL
        kwargs = {keyword.arg: _literal(keyword.value) for keyword in node.keywords}
        calls.append((path, value, kwargs))
    return calls


def _subscript_key(subscript):
    node = subscript.slice
    if isinstance(node, ast.Constant):
        return node.value
    # Python 3.8 wraps the subscript key in an ast.Index node
    inner = getattr(node, "value", None)
    if isinstance(inner, ast.Constant):
        return inner.value
    return None


def measurement_subscript_writes():
    """Every ``self._dbusservice["/Measurement/..."] = ...`` assignment target path."""
    written = []
    for node in ast.walk(_parse_driver()):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = _subscript_key(target)
            if isinstance(key, str) and key.startswith("/Measurement/"):
                written.append(key)
    return written


class MeasurementDeclarationTests(unittest.TestCase):
    """The two /Measurement paths must be declared once, with the right literals."""

    def test_kind_is_declared_as_the_string_derived(self):
        kind_calls = [call for call in measurement_add_path_calls() if call[0] == KIND_PATH]
        self.assertEqual(len(kind_calls), 1, "%s must be declared exactly once" % KIND_PATH)
        _, value, _ = kind_calls[0]
        self.assertIsInstance(value, str, "consumers read %s as a string" % KIND_PATH)
        self.assertEqual(value, "derived")

    def test_tracks_services_is_declared_as_a_writeable_empty_string(self):
        tracks_calls = [call for call in measurement_add_path_calls() if call[0] == TRACKS_PATH]
        self.assertEqual(len(tracks_calls), 1, "%s must be declared exactly once" % TRACKS_PATH)
        _, value, kwargs = tracks_calls[0]
        self.assertIsInstance(value, str)
        self.assertEqual(value, "", "%s starts empty and is filled after the search" % TRACKS_PATH)
        self.assertTrue(kwargs.get("writeable"), "%s is written after creation" % TRACKS_PATH)

    def test_only_kind_and_tracks_services_are_declared(self):
        declared = sorted(call[0] for call in measurement_add_path_calls())
        self.assertEqual(declared, [KIND_PATH, TRACKS_PATH])

    def test_kind_is_never_reassigned_after_declaration(self):
        self.assertNotIn(KIND_PATH, measurement_subscript_writes(), "%s is a constant declaration" % KIND_PATH)

    def test_tracks_services_is_the_only_measurement_path_written_at_runtime(self):
        self.assertEqual(set(measurement_subscript_writes()), {TRACKS_PATH})


# ---------------------------------------------------------------------------
# The three completion paths
# ---------------------------------------------------------------------------


class CompletionPathTestCase(unittest.TestCase):
    """Shared fixture: fresh settings and a clean GLib timeout log per test."""

    # deliberately unsorted insertion order, and battery names whose sort order
    # is the reverse of the service-name sort order
    BATTERIES = {
        "Zulu": "com.victronenergy.battery.ttyUSB0",
        "Alpha": "com.victronenergy.battery.ttyUSB2",
        "Mike": "com.victronenergy.battery.ttyUSB1",
    }
    EXPECTED = "com.victronenergy.battery.ttyUSB0,com.victronenergy.battery.ttyUSB1,com.victronenergy.battery.ttyUSB2"

    def setUp(self):
        _reset_settings(SETTINGS)
        FakeGLib.reset()
        self.addCleanup(FakeGLib.reset)

    def published(self, service):
        """The value currently on TRACKS_PATH, failing loudly if it was never filled."""
        self.assertIn(TRACKS_PATH, service._dbusservice.values, "%s was never published" % TRACKS_PATH)
        value = service._dbusservice.values[TRACKS_PATH]
        self.assertNotEqual(value, "", "%s was left at its empty declaration value" % TRACKS_PATH)
        return value

    # -- the three ways the search can finish -------------------------------

    def run_batteries_only_path(self, batteries=None):
        """Finish in _find_batteries: current comes from the BMSes."""
        batteries = self.BATTERIES if batteries is None else batteries
        SETTINGS.CURRENT_FROM_VICTRON = False
        SETTINGS.NR_OF_BATTERIES = len(batteries)
        service = make_service(
            bus_names=sorted(batteries.values()),
            monitor_values=battery_monitor_values(batteries),
        )
        self.assertIs(service._find_batteries(), False, "the search must stop once it succeeded")
        return service

    def run_no_mppt_path(self, batteries=None):
        """Finish in _find_multis: a Multi is used and no MPPTs are configured."""
        batteries = self.BATTERIES if batteries is None else batteries
        SETTINGS.NR_OF_MPPTS = 0
        multi = "com.victronenergy.vebus.ttyO1"
        service = make_service(
            batteries=batteries,
            bus_names=[multi],
            monitor_values={(multi, "/ProductName"): "MultiPlus-II 48/5000"},
        )
        self.assertIs(service._find_multis(), False, "the search must stop once it succeeded")
        return service

    def run_mppt_path(self, batteries=None):
        """Finish in _find_mppts: the configured number of MPPTs was found."""
        batteries = self.BATTERIES if batteries is None else batteries
        mppts = ["com.victronenergy.solarcharger.ttyO2", "com.victronenergy.solarcharger.ttyO3"]
        SETTINGS.NR_OF_MPPTS = len(mppts)
        service = make_service(
            batteries=batteries,
            bus_names=mppts,
            monitor_values={(mppt, "/ProductName"): "SmartSolar MPPT" for mppt in mppts},
        )
        self.assertIs(service._find_mppts(), False, "the search must stop once it succeeded")
        return service

    def completion_paths(self):
        return (
            ("batteries only", self.run_batteries_only_path),
            ("no MPPT", self.run_no_mppt_path),
            ("MPPT", self.run_mppt_path),
        )


class TracksServicesPublishedOnEveryPathTests(CompletionPathTestCase):
    """None of the three completion routes may leave the declaration empty."""

    def test_batteries_only_path_publishes_the_constituents(self):
        service = self.run_batteries_only_path()
        self.assertEqual(self.published(service), self.EXPECTED)

    def test_no_mppt_path_publishes_the_constituents(self):
        service = self.run_no_mppt_path()
        self.assertEqual(self.published(service), self.EXPECTED)

    def test_mppt_path_publishes_the_constituents(self):
        service = self.run_mppt_path()
        self.assertEqual(self.published(service), self.EXPECTED)

    def test_every_path_writes_tracks_services_exactly_once(self):
        for label, run_path in self.completion_paths():
            with self.subTest(path=label):
                service = run_path()
                writes = service._dbusservice.writes_to(TRACKS_PATH)
                self.assertEqual(len(writes), 1, "%s path wrote %s %d times" % (label, TRACKS_PATH, len(writes)))
                self.assertEqual(writes[0], self.EXPECTED)

    def test_every_path_agrees_on_the_published_value(self):
        published = {label: self.published(run_path()) for label, run_path in self.completion_paths()}
        self.assertEqual(set(published.values()), {self.EXPECTED}, published)


class TracksServicesFormatTests(CompletionPathTestCase):
    """The string is parsed by consumers, so its shape is part of the contract."""

    def test_value_is_comma_separated_without_spaces(self):
        for label, run_path in self.completion_paths():
            with self.subTest(path=label):
                value = self.published(run_path())
                self.assertNotIn(" ", value, "consumers split on ',' only")
                self.assertEqual(value.split(","), sorted(self.BATTERIES.values()))

    def test_value_is_sorted_by_service_name_not_by_battery_name(self):
        # battery names sort Alpha < Mike < Zulu, which is the reverse of the
        # service-name order; sorting the wrong side of the dict would show up here
        value = self.published(self.run_no_mppt_path())
        self.assertEqual(value, self.EXPECTED)
        self.assertNotEqual(value, ",".join(self.BATTERIES[name] for name in sorted(self.BATTERIES)))

    def test_single_battery_value_has_no_separator(self):
        one = {"OnlyBattery": "com.victronenergy.battery.ttyUSB9"}
        for label, run_path in self.completion_paths():
            with self.subTest(path=label):
                value = self.published(run_path(batteries=one))
                self.assertEqual(value, "com.victronenergy.battery.ttyUSB9")

    def test_value_is_a_plain_string(self):
        value = self.published(self.run_mppt_path())
        self.assertIsInstance(value, str)


class TracksServicesFollowsDiscoveryTests(CompletionPathTestCase):
    """The names come from the discovered batteries dict, never from a fixed list."""

    OTHER_BATTERIES = {
        "Delta": "com.victronenergy.battery.socketcan_can0",
        "Charlie": "com.victronenergy.battery.socketcan_can1",
    }
    OTHER_EXPECTED = "com.victronenergy.battery.socketcan_can0,com.victronenergy.battery.socketcan_can1"

    def test_a_different_batteries_dict_yields_a_different_value(self):
        for label, run_path in self.completion_paths():
            with self.subTest(path=label):
                value = self.published(run_path(batteries=self.OTHER_BATTERIES))
                self.assertEqual(value, self.OTHER_EXPECTED)
                self.assertNotEqual(value, self.EXPECTED)

    def test_batteries_only_path_publishes_what_the_bus_scan_discovered(self):
        service = self.run_batteries_only_path()
        self.assertEqual(
            self.published(service),
            ",".join(sorted(service._batteries_dict.values())),
        )
        self.assertEqual(sorted(service._batteries_dict.values()), sorted(self.BATTERIES.values()))

    def test_services_of_non_battery_devices_are_not_tracked(self):
        SETTINGS.CURRENT_FROM_VICTRON = False
        SETTINGS.NR_OF_BATTERIES = len(self.BATTERIES)
        monitor_values = battery_monitor_values(self.BATTERIES)
        decoy = "com.victronenergy.battery.ttyUSB7"
        monitor_values[(decoy, "/ProductName")] = "Some Other BMS"
        service = make_service(
            bus_names=sorted(list(self.BATTERIES.values()) + [decoy, "com.victronenergy.system"]),
            monitor_values=monitor_values,
        )
        self.assertIs(service._find_batteries(), False)
        self.assertEqual(self.published(service), self.EXPECTED)
        self.assertNotIn(decoy, self.published(service))


class TracksServicesNotPublishedBeforeCompletionTests(CompletionPathTestCase):
    """An unfinished search must not publish a partial constituent list."""

    def test_incomplete_battery_search_publishes_nothing(self):
        SETTINGS.CURRENT_FROM_VICTRON = False
        SETTINGS.NR_OF_BATTERIES = len(self.BATTERIES) + 1
        service = make_service(
            bus_names=sorted(self.BATTERIES.values()),
            monitor_values=battery_monitor_values(self.BATTERIES),
        )
        self.assertIs(service._find_batteries(), True, "the search must retry")
        self.assertEqual(service._dbusservice.writes_to(TRACKS_PATH), [])
        self.assertFalse(service._reactive_ready)

    def test_multi_search_defers_publishing_to_the_mppt_search(self):
        SETTINGS.NR_OF_MPPTS = 2
        multi = "com.victronenergy.vebus.ttyO1"
        service = make_service(
            batteries=self.BATTERIES,
            bus_names=[multi],
            monitor_values={(multi, "/ProductName"): "MultiPlus-II 48/5000"},
        )
        self.assertIs(service._find_multis(), False)
        self.assertEqual(service._dbusservice.writes_to(TRACKS_PATH), [])
        self.assertFalse(service._reactive_ready)

    def test_incomplete_mppt_search_publishes_nothing(self):
        SETTINGS.NR_OF_MPPTS = 2
        found = ["com.victronenergy.solarcharger.ttyO2"]
        service = make_service(
            batteries=self.BATTERIES,
            bus_names=found,
            monitor_values={(found[0], "/ProductName"): "SmartSolar MPPT"},
        )
        self.assertIs(service._find_mppts(), True, "the search must retry")
        self.assertEqual(service._dbusservice.writes_to(TRACKS_PATH), [])
        self.assertFalse(service._reactive_ready)

    def test_battery_search_delegates_to_multi_search_without_publishing(self):
        SETTINGS.CURRENT_FROM_VICTRON = True
        SETTINGS.NR_OF_BATTERIES = len(self.BATTERIES)
        service = make_service(
            bus_names=sorted(self.BATTERIES.values()),
            monitor_values=battery_monitor_values(self.BATTERIES),
        )
        self.assertIs(service._find_batteries(), False)
        self.assertEqual(service._dbusservice.writes_to(TRACKS_PATH), [])
        self.assertFalse(service._reactive_ready)
        scheduled = [callback for _, callback in FakeGLib.timeouts]
        self.assertIn(service._find_multis, scheduled)


if __name__ == "__main__":
    unittest.main()
