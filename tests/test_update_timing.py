#!/usr/bin/env python3

"""
Unit tests for the parts of _update() that depend on time rather than on calls.

The update loop used to run once a second, so counting failed calls and
measuring seconds were the same thing. With reactive updates and a 10 s floor
they are not: a burst of input changes can produce several failed calls in a
second, and a silent bank produces one every 10 s. These tests pin the
behaviour to elapsed time, with the bounds the old one second loop gave:

* reads that keep failing end in an exit no later than READ_TRIALS cycles of
  UPDATE_INTERVAL_DATA after the first failure, and not earlier either;
* while a read is failing (counted or tolerated) a retry is kept armed at
  UPDATE_INTERVAL_DATA, so neither the exit nor the recovery waits for the floor;
* the Coulomb counter integrates the current that actually flowed in the
  interval, which matters once intervals can be ten seconds long;

Time is injected through a clock the test advances by hand, and GLib is a
recorder, so a "retry" runs only when the test fires it.
"""

import unittest
from unittest import mock

import driver_harness
from driver_harness import Battery, DriverTestCase, FakeDbusMon, driver

TOLERANCE = 60


class FakeClock:
    """Stands in for the driver's ``time`` module: a clock the test moves itself."""

    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)

    def advance(self, seconds):
        self.now += seconds
        return self.now


class TimingTestCase(DriverTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patcher = mock.patch.object(driver, "tt", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.glib = driver_harness.patch_glib(self)

        self.a = Battery("BatteryA", voltage=52.0, current=0.0, power=0.0)
        self.b = Battery("BatteryB", voltage=54.0, current=0.0, power=0.0)

    def make_bank(self, **overrides):
        overrides.setdefault("MISSING_DATA_TOLERANCE", TOLERANCE)
        overrides.setdefault("UPDATE_INTERVAL_DATA", 1)
        overrides.setdefault("READ_TRIALS", 10)
        overrides.setdefault("BATTERY_EFFICIENCY", 1.0)
        service = self.make_service([self.a, self.b], **overrides)
        service._timeOld = self.clock.now
        return service

    def serve(self, service, present, absent=()):
        values = {}
        for battery in present:
            values.update(battery.values())
        for battery in absent:
            values.update({key: None for key in battery.values()})
        service._dbusMon = FakeDbusMon(values)

    def break_capacity(self, service):
        """A None that is not one of the guarded values: a plain, counted read failure."""
        values = {}
        for battery in (self.a, self.b):
            values.update(battery.values())
        values[(self.b.service, "/InstalledCapacity")] = None
        service._dbusMon = FakeDbusMon(values)

    def retries(self):
        """The retry timers armed so far, as (interval in ms, callback)."""
        return [(interval, callback) for interval, callback, _ in self.glib.timeout_calls if getattr(callback, "__name__", "") == "_retry_update"]

    def fire_retry(self):
        """Advance the clock by the armed retry's interval and run it, as the main loop would."""
        pending = self.retries()
        self.assertEqual(1, len(pending), "expected exactly one armed retry, got %r" % pending)
        interval_ms, callback = pending[0]
        self.glib.timeout_calls.clear()
        self.clock.advance(interval_ms / 1000.0)
        return callback()


class RetryWhileFailingTest(TimingTestCase):
    """While _update() does not complete, the next attempt is due in UPDATE_INTERVAL_DATA, not at the floor."""

    def test_a_counted_read_failure_arms_a_retry_at_the_data_interval(self):
        service = self.make_bank()
        self.break_capacity(service)
        with mock.patch.object(self.glib, "timeout_add", wraps=self.glib.timeout_add) as timeout_add:
            with self.assertLogs(level="ERROR"):
                service._update()
        self.assertEqual(1, timeout_add.call_count, "the retry must use the millisecond timer, not the whole-second one")
        self.assertEqual([1000], [interval for interval, _ in self.retries()])

    def test_a_tolerated_gap_arms_a_retry_at_the_data_interval(self):
        service = self.make_bank()
        self.serve(service, [self.a], [self.b])
        with self.assertLogs(level="WARNING"):
            self.assertTrue(service._update())
        self.assertEqual([1000], [interval for interval, _ in self.retries()])

    def test_the_retry_follows_update_interval_data(self):
        service = self.make_bank(UPDATE_INTERVAL_DATA=5)
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            service._update()
        self.assertEqual([5000], [interval for interval, _ in self.retries()])

    def test_only_one_retry_is_armed_however_often_it_fails(self):
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            for _ in range(5):
                self.clock.advance(0.1)
                service._update()
        self.assertEqual(1, len(self.retries()))

    def test_a_healthy_update_arms_nothing(self):
        service = self.make_bank()
        self.assertTrue(service._update())
        self.assertEqual([], self.retries())
        self.assertEqual([], self.glib.idle_calls)

    def test_the_retry_is_one_shot_and_re_arms_while_still_failing(self):
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            service._update()
            self.assertIs(False, self.fire_retry(), "the retry source must remove itself")
        self.assertEqual(1, len(self.retries()), "still failing: the next retry must be armed")

    def test_the_retry_does_not_run_on_top_of_a_queued_reactive_update(self):
        """The queued reactive update reads the same cache; running both would only double the work."""
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            service._update()
        calls = []
        service._update = lambda: calls.append(1) or True
        service._updating = True
        self.fire_retry()
        self.assertEqual([], calls)
        self.assertTrue(service._updating, "the queued reactive update still owns the guard")


class ReadFailureExitTimeTest(TimingTestCase):
    """Continuous counted failures end the process after the time ten one-second trials took, not after ten calls."""

    def _fail_until_exit(self, service, max_steps=100):
        """Drive the retry chain until the driver exits; return seconds from the first failure to the exit."""
        start = self.clock.now
        with self.assertLogs(level="ERROR"):
            service._update()
            with self.assertRaises(SystemExit):
                for _ in range(max_steps):
                    self.fire_retry()
        return self.clock.now - start

    def test_failures_on_the_retry_chain_exit_when_the_old_loop_did(self):
        service = self.make_bank()
        self.break_capacity(service)
        # the old 1 s loop: trial 1 at t0 ... trial 10 at t0 + 9 s, exit there
        self.assertAlmostEqual(9.0, self._fail_until_exit(service))
        self.assertEqual([driver.settings.TIME_BEFORE_RESTART], self.clock.slept)

    def test_the_exit_scales_with_update_interval_data(self):
        service = self.make_bank(UPDATE_INTERVAL_DATA=3, READ_TRIALS=4)
        self.break_capacity(service)
        self.assertAlmostEqual(9.0, self._fail_until_exit(service))

    def test_read_trials_one_still_exits_on_the_first_failure(self):
        service = self.make_bank(READ_TRIALS=1)
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(SystemExit):
                service._update()

    def test_a_burst_of_failures_does_not_exit_early(self):
        """Reactive triggers arriving ten to the second must not turn ten seconds of grace into one."""
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            for _ in range(50):
                service._update()
                self.clock.advance(0.1)
        # 50 failed calls in 5 s: still within the time ten trials used to take

    def test_a_burst_of_failures_still_exits_on_time(self):
        service = self.make_bank()
        self.break_capacity(service)
        start = self.clock.now
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(SystemExit):
                for _ in range(200):
                    service._update()
                    self.clock.advance(0.1)
        self.assertLessEqual(self.clock.now - start, 9.0 + 0.1 + 1e-6)

    def test_two_sparse_failures_do_not_exit(self):
        """Elapsed time alone is not enough: two failures ten seconds apart were never an exit."""
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            service._update()
            self.clock.advance(30)
            service._update()
        self.assertEqual(3, service._readTrials)

    def test_a_healthy_read_resets_the_clock(self):
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            for _ in range(8):
                service._update()
                self.clock.advance(1)
        self.serve(service, [self.a, self.b])
        self.assertTrue(service._update())
        self.break_capacity(service)
        self.clock.advance(1)
        # a fresh streak: it is again nine seconds from here
        self.assertAlmostEqual(9.0, self._fail_until_exit(service))

    def test_the_clock_stepping_back_does_not_hold_the_exit_off(self):
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR"):
            for _ in range(3):
                service._update()
                self.clock.advance(1)
            self.clock.advance(-3600)
            start = self.clock.now
            with self.assertRaises(SystemExit):
                for _ in range(100):
                    self.fire_retry()
        # the first failure after the step starts a fresh streak, one retry after start
        self.assertLessEqual(self.clock.now - start, 1.0 + 9.0 + 1e-6)

    def test_the_log_says_how_far_along_it_is(self):
        service = self.make_bank()
        self.break_capacity(service)
        with self.assertLogs(level="ERROR") as captured:
            service._update()
            self.fire_retry()
            self.fire_retry()
        trials = [message for message in captured.output if "Read trial nr." in message]
        self.assertEqual(3, len(trials))
        self.assertIn("Read trial nr. 3 of 10", trials[-1])
        self.assertIn("failing for 2 s", trials[-1])
        self.assertIn("exit after 9 s", trials[-1])


class MissingDataBoundsTest(TimingTestCase):
    """A tolerated gap is evaluated every UPDATE_INTERVAL_DATA, so its bounds are those of the one second loop."""

    def test_the_gap_hands_over_and_exits_when_the_old_loop_did(self):
        service = self.make_bank()
        self.assertTrue(service._update())
        self.serve(service, [self.a], [self.b])
        start = self.clock.now
        with self.assertLogs(level="WARNING"):
            service._update()
            with self.assertRaises(SystemExit):
                for _ in range(1000):
                    self.fire_retry()
        # old loop: tolerated up to TOLERANCE, the first cycle past it is trial 1,
        # nine more one second cycles to trial 10
        self.assertAlmostEqual(TOLERANCE + 1 + 9, self.clock.now - start)

    def test_publishing_resumes_on_the_next_retry_after_the_data_returns(self):
        """velib refills a returning service's cache without a change callback: the retry is what notices."""
        service = self.make_bank()
        self.assertTrue(service._update())
        self.serve(service, [self.a], [self.b])
        with self.assertLogs(level="WARNING"):
            service._update()
            for _ in range(20):
                self.fire_retry()

        self.b.voltage = 54.4
        self.serve(service, [self.a, self.b])
        before = self.clock.now
        with self.assertLogs(level="WARNING") as captured:
            self.fire_retry()
        self.assertLessEqual(self.clock.now - before, 1.0)
        self.assertTrue(any("complete again" in message for message in captured.output))
        self.assertAlmostEqual(53.2, service._dbusservice.published["/Dc/0/Voltage"])
        self.assertEqual([], self.retries(), "healthy again: no further retry")


class CoulombCounterTest(TimingTestCase):
    """The counter integrates the current that flowed in the interval, i.e. the one read at the previous update."""

    def test_a_step_after_a_quiet_interval_is_not_integrated_backwards(self):
        service = self.make_bank()
        self.assertTrue(service._update())
        charge = service._ownCharge

        # ten quiet seconds at 0 A (no input changed, so no update ran), then a
        # 100 A load starts and the change triggers an update
        self.clock.advance(10)
        self.a.current = self.b.current = 50.0
        self.serve(service, [self.a, self.b])
        self.assertTrue(service._update())
        self.assertAlmostEqual(charge, service._ownCharge, places=9, msg="the 100 A did not flow during the 10 s before it started")

        # the next interval is the one in which the 100 A did flow
        self.clock.advance(10)
        self.assertTrue(service._update())
        self.assertAlmostEqual(charge + 100.0 * 10 / 3600, service._ownCharge, places=9)

    def test_constant_current_is_integrated_as_before(self):
        self.a.current = self.b.current = -5.0
        service = self.make_bank()
        charge = service._ownCharge
        for _ in range(3):
            self.clock.advance(10)
            self.assertTrue(service._update())
        self.assertAlmostEqual(charge - 10.0 * 30 / 3600, service._ownCharge, places=9)


if __name__ == "__main__":
    unittest.main()
