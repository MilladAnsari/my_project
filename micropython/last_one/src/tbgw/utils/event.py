"""tbgw.utils.event
=====================

MicroPython's `_thread` has no `Event` class (unlike CPython's `threading`).
This is a minimal poll-based stand-in used for simple cross-thread signaling
that doesn't belong on the data-carrying Queue -- e.g. "gateway connected to
ThingsBoard", "shutdown requested", "CAN bus recovered from bus-off".
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

_POLL_INTERVAL_MS = 10


def _sleep_ms(ms):
    if time is None:
        return
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


def _now_ms():
    if time is None:
        return 0
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def _elapsed_ms(start_ms):
    if time is None:
        return 0
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(_now_ms(), start_ms)
    return _now_ms() - start_ms


class Event:
    def __init__(self):
        self._lock = _thread.allocate_lock() if _thread is not None else threading.Lock()
        self._flag = False

    def set(self):
        with self._lock:
            self._flag = True

    def clear(self):
        with self._lock:
            self._flag = False

    def is_set(self):
        with self._lock:
            return self._flag

    def wait(self, timeout=None):
        """Returns True if the event was set before the timeout (seconds),
        False if the timeout elapsed first. timeout=None waits forever.
        """
        start = _now_ms()
        while True:
            if self.is_set():
                return True
            if timeout is not None and _elapsed_ms(start) >= timeout * 1000:
                return False
            _sleep_ms(_POLL_INTERVAL_MS)
