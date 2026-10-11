"""tbgw.core.device_state_store
=================================

A thread-safe cache of each device's latest known state (merged
attributes + telemetry), updated by `TBGatewayService.send_to_storage()`
on every uplink event -- the exact same call every connector (CAN, MQTT)
already makes to queue an event for the ThingsBoard uplink. This means
the GUI (Phase 4, Core 0) sees state updates through the identical
pipeline ThingsBoard does, with no separate wiring in either connector.

See docs/architecture.md, "The 'no optimistic GUI updates' rule": the
GUI reads this store to render device state, and NEVER writes to it
directly -- writes only happen as a side effect of a real uplink event,
whether that event originated from a physical state change, a
GUI-issued local command, or a ThingsBoard RPC.
"""

try:
    import _thread
    _lock = _thread.allocate_lock()
except ImportError:  # pragma: no cover - host CPython fallback
    import threading
    _lock = threading.Lock()

try:
    import time
except ImportError:  # pragma: no cover
    time = None


def _now_ms():
    if time is None:
        return 0
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


class DeviceStateStore:
    def __init__(self):
        self._states = {}  # device_name -> {"values": dict, "updated_at_ms": int}

    def update(self, device_name, values):
        """Merges `values` (a dict of attribute/telemetry key-value pairs)
        into the device's current state. Does not replace the whole
        state -- a telemetry-only event doesn't erase previously known
        attributes, and vice versa.
        """
        if not values:
            return
        with _lock:
            entry = self._states.setdefault(device_name, {"values": {}, "updated_at_ms": 0})
            entry["values"].update(values)
            entry["updated_at_ms"] = _now_ms()

    def update_uplink_values(self, device_name, attributes=None, telemetry=None):
        """Merge a converted uplink event while retaining each value's kind."""
        attributes = attributes or {}
        telemetry = telemetry or {}
        if not attributes and not telemetry:
            return
        with _lock:
            entry = self._states.setdefault(
                device_name,
                {"values": {}, "attributes": {}, "telemetry": {}, "updated_at_ms": 0},
            )
            # Entries created by the original update() API remain valid.
            entry.setdefault("attributes", {})
            entry.setdefault("telemetry", {})
            entry["attributes"].update(attributes)
            entry["telemetry"].update(telemetry)
            entry["values"].update(attributes)
            entry["values"].update(telemetry)
            entry["updated_at_ms"] = _now_ms()

    def get(self, device_name):
        """Returns a shallow COPY of the device's current state dict, or
        None if nothing has ever been reported for this device. Callers
        (the GUI) must not mutate the dict returned by reference back
        into a store update -- always go through update().
        """
        with _lock:
            entry = self._states.get(device_name)
            return dict(entry["values"]) if entry else None

    def get_updated_at(self, device_name):
        with _lock:
            entry = self._states.get(device_name)
            return entry["updated_at_ms"] if entry else None

    def snapshot(self):
        """Returns {device_name: {values...}} for every known device --
        used by the GUI to do a full refresh (e.g. on screen navigation).
        """
        with _lock:
            return {name: dict(entry["values"]) for name, entry in self._states.items()}

    def snapshot_uplink_events(self, device_names=None):
        """Return replay-ready ``deviceName``/attribute/telemetry events."""
        allowed = set(device_names) if device_names is not None else None
        with _lock:
            events = []
            for name, entry in self._states.items():
                if allowed is not None and name not in allowed:
                    continue
                attributes = dict(entry.get("attributes", {}))
                telemetry = dict(entry.get("telemetry", {}))
                if attributes or telemetry:
                    events.append({
                        "deviceName": name,
                        "attributes": attributes,
                        "telemetry": telemetry,
                    })
            return events

    def clear(self):
        with _lock:
            self._states = {}
