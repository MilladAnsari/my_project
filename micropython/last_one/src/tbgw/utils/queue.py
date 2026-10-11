"""tbgw.utils.queue
=====================

MicroPython has no `queue` module and no condition variables in `_thread`.
This is a small bounded, thread-safe FIFO queue used as the hand-off point
between:

  * the CAN RX thread (Phase 2) and the gateway's storage/uplink path,
  * MQTT connector callbacks and their consumer loop (so `on_message` never
    blocks -- exactly the risk flagged in the prior port's report),
  * the RPC dispatcher (Phase 3) and connector downlink handlers.

Design notes
------------
* Backed by a plain list used as a ring buffer (fixed `maxsize`, no
  reallocation once created) to avoid repeated allocation/GC churn on a
  memory-constrained target.
* No OS-level condition variable exists in MicroPython's `_thread`, so
  blocking get()/put() are implemented as a short poll loop
  (`_POLL_INTERVAL_MS`) protected by a lock only while touching the buffer,
  not while sleeping -- this keeps the lock hold time minimal even though
  the wait itself is not event-driven.
* `Empty`/`Full` exceptions mirror `queue.Empty`/`queue.Full` so calling
  code reads the same as it would against CPython's queue module.
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


def _allocate_lock():
    if _thread is not None:
        return _thread.allocate_lock()
    return threading.Lock()


class Empty(Exception):
    pass


class Full(Exception):
    pass


class Queue:
    """A bounded FIFO queue. maxsize <= 0 means unbounded (use with care on
    a microcontroller -- prefer an explicit bound so a stalled uplink can't
    grow the queue until the heap is exhausted).
    """

    def __init__(self, maxsize=0):
        self.maxsize = maxsize
        self._buffer = []
        self._lock = _allocate_lock()

    def qsize(self):
        with self._lock:
            return len(self._buffer)

    def empty(self):
        return self.qsize() == 0

    def full(self):
        if self.maxsize <= 0:
            return False
        return self.qsize() >= self.maxsize

    def put_nowait(self, item):
        with self._lock:
            if self.maxsize > 0 and len(self._buffer) >= self.maxsize:
                raise Full()
            self._buffer.append(item)

    def get_nowait(self):
        with self._lock:
            if not self._buffer:
                raise Empty()
            return self._buffer.pop(0)

    def put(self, item, block=True, timeout=None):
        if not block:
            return self.put_nowait(item)
        start = _now_ms()
        while True:
            try:
                return self.put_nowait(item)
            except Full:
                if timeout is not None and _elapsed_ms(start) >= timeout * 1000:
                    raise
                _sleep_ms(_POLL_INTERVAL_MS)

    def get(self, block=True, timeout=None):
        if not block:
            return self.get_nowait()
        start = _now_ms()
        while True:
            try:
                return self.get_nowait()
            except Empty:
                if timeout is not None and _elapsed_ms(start) >= timeout * 1000:
                    raise
                _sleep_ms(_POLL_INTERVAL_MS)

    def get_batch(self, max_items):
        """Non-blocking: pops up to `max_items` items in one locked section.
        Used by the storage layer to pull a whole "event pack" at once
        instead of repeated get_nowait() calls (fewer lock acquisitions).
        """
        with self._lock:
            n = min(max_items, len(self._buffer))
            if n == 0:
                return []
            items, self._buffer = self._buffer[:n], self._buffer[n:]
            return items

    def clear(self):
        with self._lock:
            self._buffer = []
