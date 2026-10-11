"""tbgw.core.device_registry
==============================

Maps device_name -> the Connector instance that owns it (populated as
connectors report device connects via TBGatewayService.connect_device()).
Used by rpc_dispatcher.py to route inbound RPC/attribute-update traffic
to the right connector, and available for the GUI (Phase 4) to list
currently-known devices.

Thread safety: entries are written from TBGatewayService's device-event
asyncio task (see gateway_service.py's docstring for why device
connect/disconnect events are queued rather than applied directly from
whichever thread reported them) and read from the same asyncio thread by
rpc_dispatcher.py -- so in the current design this registry is only ever
touched from the single Core 1 gateway thread. The lock is kept anyway
since `list_devices()` is intended to also be callable from the GUI
thread (Phase 4, Core 0) for a live device list, which WOULD be a
genuine cross-thread read.
"""

try:
    import _thread
    _lock = _thread.allocate_lock()
except ImportError:  # pragma: no cover - host CPython fallback
    import threading
    _lock = threading.Lock()


class DeviceRegistry:
    def __init__(self, logger=None):
        self._logger = logger
        self._devices = {}  # device_name -> {"deviceType": str, "connector": Connector}

    def register(self, device_name, device_type, connector):
        with _lock:
            already_present = device_name in self._devices
            self._devices[device_name] = {"deviceType": device_type, "connector": connector}
        if self._logger is not None and not already_present:
            self._logger.info(
                "Device registered: %s (type=%s, connector=%s)",
                device_name, device_type, getattr(connector, "name", connector),
            )

    def unregister(self, device_name):
        with _lock:
            existed = self._devices.pop(device_name, None) is not None
        if self._logger is not None and existed:
            self._logger.info("Device unregistered: %s", device_name)

    def get_connector(self, device_name):
        with _lock:
            entry = self._devices.get(device_name)
            return entry["connector"] if entry else None

    def get_device_type(self, device_name):
        with _lock:
            entry = self._devices.get(device_name)
            return entry["deviceType"] if entry else None

    def is_registered(self, device_name):
        with _lock:
            return device_name in self._devices

    def list_devices(self):
        """Returns a list of {"name": ..., "type": ..., "connector": <connector name>}
        dicts, safe to call from any thread (e.g. the GUI, Phase 4).
        """
        with _lock:
            return [
                {"name": name, "type": entry["deviceType"], "connector": getattr(entry["connector"], "name", None)}
                for name, entry in self._devices.items()
            ]

    def count(self):
        with _lock:
            return len(self._devices)
