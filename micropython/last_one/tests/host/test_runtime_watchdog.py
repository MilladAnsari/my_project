import unittest

from tbgw.core.runtime_watchdog import RuntimeHealth, WatchdogGate


def _wrapped_diff(now, then):
    """Small deterministic equivalent of time.ticks_diff() for tests."""
    period = 1000
    return ((now - then + period // 2) % period) - period // 2


class TestRuntimeHealth(unittest.TestCase):
    def test_reports_unstarted_and_old_workers_as_stale(self):
        health = RuntimeHealth(clock_ms=lambda: 0)
        health.register("gui")
        health.register("can")
        health.beat("gui", now_ms=100)

        self.assertEqual(health.stale_workers(("gui", "can"), 50, now_ms=160), ["gui", "can"])

    def test_tick_wrap_does_not_make_recent_heartbeat_stale(self):
        health = RuntimeHealth(clock_ms=lambda: 0, ticks_diff=_wrapped_diff)
        health.beat("can", now_ms=990)

        self.assertEqual(health.stale_workers(("can",), 30, now_ms=10), [])
        self.assertEqual(health.stale_workers(("can",), 10, now_ms=10), ["can"])


class TestWatchdogGate(unittest.TestCase):
    def setUp(self):
        self.now = [0]
        self.health = RuntimeHealth(clock_ms=lambda: self.now[0])
        for worker in ("gateway", "can", "gui"):
            self.health.register(worker)
        self.gate = WatchdogGate(
            self.health, ("gateway", "can", "gui"),
            stale_after_ms=50, startup_grace_ms=100,
            clock_ms=lambda: self.now[0],
        )
        self.gate.arm()

    def _beat_all(self):
        for worker in ("gateway", "can", "gui"):
            self.health.beat(worker, now_ms=self.now[0])

    def test_feeds_during_bounded_startup_grace(self):
        self.assertEqual(self.gate.should_feed(), (True, ()))
        self.now[0] = 99
        self.assertEqual(self.gate.should_feed(), (True, ()))

    def test_stale_gui_withholds_feed_after_grace(self):
        self.now[0] = 101
        should_feed, stale = self.gate.should_feed()

        self.assertFalse(should_feed)
        self.assertEqual(stale, ("gateway", "can", "gui"))

    def test_physical_can_offline_does_not_matter_when_loop_heartbeats(self):
        self._beat_all()
        self.now[0] = 40
        # No bus/liveness state is provided to the health gate. A running
        # CAN loop remains fresh even while its peer/switch is unplugged.
        self._beat_all()

        self.assertEqual(self.gate.should_feed(), (True, ()))

    def test_stale_can_loop_withholds_feed_after_initial_readiness(self):
        self._beat_all()
        self.assertEqual(self.gate.should_feed(), (True, ()))
        self.now[0] = 60
        self.health.beat("gateway", now_ms=self.now[0])
        self.health.beat("gui", now_ms=self.now[0])

        should_feed, stale = self.gate.should_feed()
        self.assertFalse(should_feed)
        self.assertEqual(stale, ("can",))
