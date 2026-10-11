"""tbgw.storage.memory_storage
================================

RAM-resident event storage, bounded by `maxsize`. This is the only storage
backend shipped in this port (see architecture.md); it trades durability
across power loss for speed and simplicity, which is the right trade-off
for a microcontroller gateway whose upstream broker connection is expected
to be intermittent for seconds/minutes, not hours/days.

Overflow policy is configurable:
  * "drop_oldest" (default) -- silently evicts the oldest unsent event to
    make room for a new one. Appropriate for live telemetry where a stale
    reading is worse than no reading.
  * "reject_new" -- raises Full instead of evicting; appropriate for
    attribute updates / RPC replies where losing the OLDEST item silently
    would be worse than the caller finding out the new one didn't fit.
"""

try:
    import _thread
except ImportError:  # pragma: no cover - host CPython fallback
    _thread = None
    import threading

from tbgw.storage.base import EventStorage
from tbgw.utils.queue import Full


def _allocate_lock():
    if _thread is not None:
        return _thread.allocate_lock()
    return threading.Lock()


class MemoryEventStorage(EventStorage):
    def __init__(self, maxsize=1000, overflow_policy="drop_oldest", logger=None):
        if overflow_policy not in ("drop_oldest", "reject_new"):
            raise ValueError("overflow_policy must be 'drop_oldest' or 'reject_new'")
        self.maxsize = maxsize
        self.overflow_policy = overflow_policy
        self._logger = logger
        self._buffer = []
        self._pending_pack = None  # checked-out-but-not-yet-confirmed pack
        self._lock = _allocate_lock()

    def put(self, event):
        with self._lock:
            if self.maxsize > 0 and len(self._buffer) >= self.maxsize:
                if self.overflow_policy == "drop_oldest":
                    dropped = self._buffer.pop(0)
                    if self._logger is not None:
                        self._logger.warning(
                            "MemoryEventStorage full (maxsize=%s), dropped oldest event", self.maxsize
                        )
                    # If the dropped event was part of an already-checked-out
                    # pack, shrink that pack view too so indices stay valid.
                    if self._pending_pack:
                        try:
                            self._pending_pack.remove(dropped)
                        except ValueError:
                            pass
                else:
                    raise Full("MemoryEventStorage is full (maxsize=%s)" % self.maxsize)
            self._buffer.append(event)

    def get_event_pack(self, max_size):
        with self._lock:
            if self._pending_pack is not None:
                # A pack is already checked out and not yet confirmed --
                # return the same pack rather than handing out a second,
                # overlapping view of the buffer.
                return list(self._pending_pack)
            n = min(max_size, len(self._buffer))
            self._pending_pack = self._buffer[:n]
            return list(self._pending_pack)

    def event_pack_processing_done(self):
        with self._lock:
            if not self._pending_pack:
                return
            n = len(self._pending_pack)
            self._buffer = self._buffer[n:]
            self._pending_pack = None

    def discard_pending_pack(self):
        """Releases the checked-out pack WITHOUT deleting the events, so a
        failed uplink attempt can retry later via get_event_pack() again.
        """
        with self._lock:
            self._pending_pack = None

    def len(self):
        with self._lock:
            return len(self._buffer)

    def clear(self):
        with self._lock:
            self._buffer = []
            self._pending_pack = None
