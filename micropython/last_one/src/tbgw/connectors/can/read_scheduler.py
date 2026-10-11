"""tbgw.connectors.can.read_scheduler
=======================================

Pure state machine (no I/O, no driver, no threads -- an injected
`now_ms` clock is the only time source) that decides:

  * which (device, selector) polls are due, since xxx-style buses
    only report events on change -- a device whose state hasn't
    changed since boot would otherwise never populate, and the GUI
    would show blanks forever;
  * which node currently has an outstanding read, since xxx's read
    reply carries no register number at all (DLC=1, value only) -- a
    second read to the same node before the first reply (or a timeout)
    arrives would be unattributable;
  * when an outstanding read has timed out and should be retried or
    given up on.

"device" here means whatever key the caller uses to identify a bus
node -- this project uses the CAN arbitration "deviceKey" (the node's
address), matching frame_codec.DecodedFrame.device_key, so a
selector-less reply can be attributed back to its scheduler slot
without any name lookup.
"""


class ReadScheduler:
    def __init__(self, poll_config, read_timeout_ms=300, read_retries=1,
                 stagger_step_ms=50, logger=None):
        """poll_config: {device_key: [{"selector": int, "intervalMs": int}, ...], ...}

        Initial due-times are staggered across devices/selectors (each
        subsequent one pushed `stagger_step_ms` further out) so a board
        with several polled datapoints doesn't burst them all in the
        same tick right after boot.
        """
        self._read_timeout_ms = read_timeout_ms
        self._read_retries = read_retries
        self._logger = logger

        # (device_key, selector) -> configured interval
        self._intervals = {}
        # (device_key, selector) -> ms timestamp of the last known-fresh
        # value (from an event, a reply, or -- initially -- a staggered
        # "due soon" placeholder in the past)
        self._last_fresh_ms = {}

        stagger = 0
        for device_key, entries in poll_config.items():
            for entry in entries:
                key = (device_key, entry["selector"])
                self._intervals[key] = entry["intervalMs"]
                self._last_fresh_ms[key] = -entry["intervalMs"] + stagger
                stagger += stagger_step_ms

        # device_key -> {"selector", "sent_at_ms", "retries_left"}
        self._pending = {}

    def due(self, now_ms):
        """Returns [(device_key, selector), ...] whose configured
        interval has elapsed since the last fresh value AND whose
        device has no outstanding read (at most one read per node may
        be in flight, since a reply cannot be attributed to a selector
        any other way).  A single call also returns at most one selector
        per device: callers send the returned reads afterwards, so merely
        checking ``_pending`` here is not enough to prevent several reads
        for the same selector-less-reply device being queued at once.
        """
        result = []
        scheduled_devices = set()
        for (device_key, selector), interval_ms in self._intervals.items():
            if device_key in self._pending or device_key in scheduled_devices:
                continue
            last = self._last_fresh_ms[(device_key, selector)]
            if now_ms - last >= interval_ms:
                result.append((device_key, selector))
                scheduled_devices.add(device_key)
        return result

    def on_read_sent(self, device_key, selector, now_ms, retries_left=None):
        """Call after actually transmitting a read command. `retries_left`
        defaults to the configured `read_retries` for a fresh read; pass
        the decremented count when resending after a timeout (see
        `expired()`).
        """
        if retries_left is None:
            retries_left = self._read_retries
        self._pending[device_key] = {
            "selector": selector, "sent_at_ms": now_ms, "retries_left": retries_left,
        }

    def on_value(self, device_key, selector, now_ms):
        """Call for every fresh value seen for (device_key, selector),
        whether it arrived as an unsolicited event or as a read reply
        (a selector-less reply must first be resolved to a selector via
        `pending_for()` -- see can_connector.py). Clears the pending
        read on this device if it was waiting on this selector.
        """
        self._last_fresh_ms[(device_key, selector)] = now_ms
        pending = self._pending.get(device_key)
        if pending is not None and pending["selector"] == selector:
            del self._pending[device_key]

    def pending_for(self, device_key):
        """Returns the selector this device has an outstanding read for,
        or None. Used to attribute a selector-less reply (DLC=1, per
        frame_codec) back to what it must be answering.
        """
        pending = self._pending.get(device_key)
        return pending["selector"] if pending is not None else None

    def cancel_pending(self, device_key):
        """Forget an in-flight read before a priority write to this node.

        xxx read replies do not include a selector.  Letting such a reply
        race a write can therefore associate an old response with the wrong
        operation.  The write path calls this from the one CAN-owning thread.
        """
        self._pending.pop(device_key, None)

    def request_refresh(self, device_key, selector, now_ms):
        """Make a configured selector due as soon as the caller permits it."""
        key = (device_key, selector)
        interval_ms = self._intervals.get(key)
        if interval_ms is None:
            return False
        self.cancel_pending(device_key)
        self._last_fresh_ms[key] = now_ms - interval_ms
        return True

    def expired(self, now_ms):
        """Returns [(device_key, selector, retries_left), ...] for every
        outstanding read that has exceeded `read_timeout_ms`. Clears
        each returned slot (the caller decides what happens next):
        `retries_left > 0` means the caller should resend and call
        `on_read_sent(device_key, selector, now_ms, retries_left - 1)`;
        `retries_left == 0` means give up -- the next scheduled poll (or
        a future event) will pick this datapoint up again on its own.
        """
        result = []
        for device_key, info in list(self._pending.items()):
            if now_ms - info["sent_at_ms"] < self._read_timeout_ms:
                continue
            result.append((device_key, info["selector"], info["retries_left"]))
            del self._pending[device_key]
            if info["retries_left"] <= 0 and self._logger is not None:
                self._logger.warning(
                    "Read timed out with no retries left: device=0x%X selector=%s",
                    device_key, info["selector"],
                )
        return result
