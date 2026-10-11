"""tbgw.gui.device_catalog
============================

Loads and validates `config/home_devices.json` (see docs/architecture.md,
"Device catalog"): homes -> devices -> type/subtype/bus/nodeAddress. Every
`deviceName` here must match the corresponding `deviceName` in
`can.json`/`mqtt.json`'s `dataMapping`/`requestsMapping`, since
`DeviceRegistry`/`DeviceStateStore` lookups are keyed by that name.

No LVGL/hardware dependency -- this module is pure data modeling and is
fully host-testable, same as everything under `tbgw/core/`.
"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - host CPython fallback
    import json


class DeviceCatalogError(Exception):
    pass


VALID_TYPES = ("light", "thermostat", "blind")
VALID_BUSES = ("can", "mqtt")


class Device:
    def __init__(self, device_name, display_name, type_, subtype, bus, node_address, home_name):
        self.device_name = device_name
        self.display_name = display_name
        self.type = type_
        self.subtype = subtype
        self.bus = bus
        self.node_address = node_address
        self.home_name = home_name

    def __repr__(self):
        return "Device(%r, type=%r, bus=%r)" % (self.device_name, self.type, self.bus)


class Home:
    def __init__(self, name, devices):
        self.name = name
        self.devices = devices


class DeviceCatalog:
    def __init__(self, homes):
        self.homes = homes
        self._device_index = {}
        for home in homes:
            for device in home.devices:
                self._device_index[device.device_name] = device

    def get_home(self, name):
        for home in self.homes:
            if home.name == name:
                return home
        return None

    def get_device(self, device_name):
        return self._device_index.get(device_name)

    def all_devices(self):
        return list(self._device_index.values())

    def home_names(self):
        return [home.name for home in self.homes]


# -- validation ---------------------------------------------------------------

def _validate_light_subtype(subtype, device_name):
    poles = subtype.get("poles")
    if not isinstance(poles, int) or poles < 1:
        raise DeviceCatalogError("light device %r: subtype.poles must be a positive integer" % device_name)
    pole_labels = subtype.get("poleLabels")
    if pole_labels is not None and len(pole_labels) != poles:
        raise DeviceCatalogError(
            "light device %r: poleLabels length (%d) does not match poles (%d)"
            % (device_name, len(pole_labels), poles)
        )


def _validate_thermostat_subtype(subtype, device_name):
    min_setpoint = subtype.get("minSetpoint", 0)
    max_setpoint = subtype.get("maxSetpoint", 100)
    if min_setpoint >= max_setpoint:
        raise DeviceCatalogError(
            "thermostat device %r: minSetpoint (%s) must be < maxSetpoint (%s)"
            % (device_name, min_setpoint, max_setpoint)
        )


def _validate_blind_subtype(subtype, device_name):
    pass  # no required fields currently


_TYPE_VALIDATORS = {
    "light": _validate_light_subtype,
    "thermostat": _validate_thermostat_subtype,
    "blind": _validate_blind_subtype,
}


def _build_device(entry, home_name):
    device_name = entry.get("deviceName")
    if not device_name:
        raise DeviceCatalogError("a device entry in home %r is missing 'deviceName'" % home_name)

    device_type = entry.get("type")
    if device_type not in VALID_TYPES:
        raise DeviceCatalogError(
            "device %r has unknown type %r (must be one of %s)" % (device_name, device_type, VALID_TYPES)
        )

    bus = entry.get("bus")
    if bus not in VALID_BUSES:
        raise DeviceCatalogError(
            "device %r has unknown bus %r (must be one of %s)" % (device_name, bus, VALID_BUSES)
        )

    subtype = entry.get("subtype", {})
    _TYPE_VALIDATORS[device_type](subtype, device_name)

    return Device(
        device_name=device_name,
        display_name=entry.get("displayName", device_name),
        type_=device_type,
        subtype=subtype,
        bus=bus,
        node_address=entry.get("nodeAddress"),
        home_name=home_name,
    )


def parse_device_catalog(data):
    """data: an already-parsed JSON dict (see config/home_devices.json
    for the shape). Returns a DeviceCatalog. Raises DeviceCatalogError on
    any structural or validation problem. Split out from
    load_device_catalog() so tests can exercise parsing/validation logic
    without touching the filesystem.
    """
    homes = []
    seen_device_names = set()
    for home_entry in data.get("homes", []):
        home_name = home_entry.get("assetName")
        if not home_name:
            raise DeviceCatalogError("a home entry is missing 'assetName'")

        devices = []
        for device_entry in home_entry.get("devices", []):
            device = _build_device(device_entry, home_name)
            if device.device_name in seen_device_names:
                raise DeviceCatalogError(
                    "duplicate deviceName %r across catalog (device names must be globally unique, "
                    "not just unique within a home)" % device.device_name
                )
            seen_device_names.add(device.device_name)
            devices.append(device)

        homes.append(Home(home_name, devices))

    return DeviceCatalog(homes)


def load_device_catalog(path="config/home_devices.json"):
    with open(path, "r") as f:
        data = json.load(f)
    return parse_device_catalog(data)
