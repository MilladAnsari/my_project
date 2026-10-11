"""Small, cross-thread runtime health primitives for the hardware WDT.

The ESP32 task watchdog resets the whole board when it is not fed.  It is
therefore deliberately fed only after the independent GUI, CAN and gateway
workers have all reported recent progress.  Network reachability is not a
worker heartbeat: the local smart-home controls must remain usable offline.
"""

try:
    import _thread
except ImportError:  # pragma: no cover - host CPython fallback
    _thread = None
    import threading

try:
    import time
except ImportError:  # pragma: no cover
    time = None


def _now_ms():
    if time is None:
        return 0
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(now_ms, then_ms):
    if time is not None and hasattr(time, "ticks_diff"):
        return time.ticks_diff(now_ms, then_ms)
    return now_ms - then_ms


def _allocate_lock():
    if _thread is not None:
        return _thread.allocate_lock()
    return threading.Lock()


class RuntimeHealth:
    """Thread-safe timestamps for independently scheduled runtime workers."""

    def __init__(self, clock_ms=None, ticks_diff=None):
        self._clock_ms = clock_ms or _now_ms
        self._ticks_diff = ticks_diff or _ticks_diff
        self._heartbeats = {}
        self._lock = _allocate_lock()

    def register(self, worker):
        with self._lock:
            self._heartbeats[worker] = None

    def beat(self, worker, now_ms=None):
        if now_ms is None:
            now_ms = self._clock_ms()
        with self._lock:
            self._heartbeats[worker] = now_ms

    def stale_workers(self, workers, max_age_ms, now_ms=None):
        if now_ms is None:
            now_ms = self._clock_ms()
        with self._lock:
            heartbeats = {worker: self._heartbeats.get(worker) for worker in workers}
        return [
            worker for worker, last_seen in heartbeats.items()
            if last_seen is None or self._ticks_diff(now_ms, last_seen) > max_age_ms
        ]


class WatchdogGate:
    """Allow WDT feeds only while every required worker is fresh."""

    def __init__(self, health, required_workers, stale_after_ms, startup_grace_ms,
                 clock_ms=None, ticks_diff=None):
        self._health = health
        self._required_workers = tuple(required_workers)
        self._stale_after_ms = stale_after_ms
        self._startup_grace_ms = startup_grace_ms
        self._clock_ms = clock_ms or _now_ms
        self._ticks_diff = ticks_diff or _ticks_diff
        self._armed_at_ms = None
        self._ready_once = False

    def arm(self, now_ms=None):
        self._armed_at_ms = self._clock_ms() if now_ms is None else now_ms

    def should_feed(self, now_ms=None):
        if now_ms is None:
            now_ms = self._clock_ms()
        if self._armed_at_ms is None:
            self.arm(now_ms)

        stale = self._health.stale_workers(
            self._required_workers, self._stale_after_ms, now_ms
        )
        if not stale:
            self._ready_once = True
            return True, ()
        if (not self._ready_once and
                self._ticks_diff(now_ms, self._armed_at_ms) < self._startup_grace_ms):
            return True, ()
        return False, tuple(stale)
