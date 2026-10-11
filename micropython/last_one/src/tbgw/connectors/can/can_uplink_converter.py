"""tbgw.connectors.can.can_uplink_converter
==============================================

Converts a decoded CAN frame -- (deviceKey, selector, payload), as
produced by tbgw.connectors.can.frame_codec.FrameCodec.decode() -- into
the ThingsBoard Gateway-API telemetry/attribute shape, per the
register/selector schema documented in docs/can_connector_design.md.

This is a rewrite of the Phase 2 byte-offset converter: upstream TBGW's
model (one CAN ID -> fixed byte layout) cannot express a register bus
like xxx's, where the arbitration ID names a NODE and a selector byte
inside the payload names which datapoint the frame carries, with values
packed as bit-fields rather than byte-aligned slices. See the design doc
for the full rationale; this module implements its "uplink converter
contract" section exactly.

Output shape (unchanged from Phase 2, so gateway_service.py /
storage / uplink_client / DeviceStateStore need no changes):

    {
        "deviceName": "xxx",
        "deviceType": "thermostat",
        "attributes": {"guestMode": "custom"},
        "telemetry": {"currentTemperature": 29},
    }
"""

from tbgw.utils.bitfield import extract, BitfieldError
from tbgw.utils.tb_utility import parse_can_id


class CanUplinkConverterError(Exception):
    pass


def _parse_devices(devices_config):
    """Returns {device_key: device_dict} -- device_dict is the config
    entry with "address" replaced by its parsed int form, for O(1)
    lookup by the value frame_codec.decode() actually produces.
    """
    by_key = {}
    for device in devices_config:
        key = parse_can_id(device["address"])
        entry = dict(device)
        entry["address"] = key
        by_key[key] = entry
    return by_key


class CanUplinkConverter:
    """Built once per CAN connector from the full can.json config (the
    "devices" and "datapointSets" sections). Pure and hardware-free --
    convert() takes plain Python values and does no I/O, so it is fully
    host-testable without a driver, gateway, or board.
    """

    def __init__(self, devices_config, datapoint_sets_config, logger=None):
        self._devices_by_key = _parse_devices(devices_config)
        self._devices_by_name = {d["deviceName"]: d for d in self._devices_by_key.values()}
        self._datapoint_sets = datapoint_sets_config
        self._logger = logger
        # (device_key, selector, field_key) already logged once -- avoids
        # spamming the log every time a malformed/unmapped value repeats.
        self._warned = set()

    def device_by_key(self, device_key):
        """Returns the device config dict for `device_key`, or None.
        Exposed so the connector can look up a device's profile/name for
        things unrelated to conversion (e.g. registering it with the
        gateway at open()).
        """
        return self._devices_by_key.get(device_key)

    def device_by_name(self, device_name):
        """Returns the device config dict for `device_name`, or None --
        used by the connector to resolve an RPC/attribute-update target
        (which arrives by name) back to its bus address.
        """
        return self._devices_by_name.get(device_name)

    def known_device_keys(self):
        return list(self._devices_by_key.keys())

    def all_devices(self):
        """Every configured device dict, in declaration order -- used by
        the connector to register all of them with the gateway at
        open(), and to build the read-scheduler's poll table.
        """
        return list(self._devices_by_key.values())

    def convert(self, device_key, selector, payload):
        """Returns the ConvertedData dict described in this module's
        docstring, or None if `device_key`/`selector` isn't one this
        config maps -- which is a normal, silent outcome on a shared
        bus (other nodes, or registers this config doesn't care about),
        not an error.

        Never raises: a malformed payload (too short for a mapped
        field) or an out-of-range value is logged once per
        (device, selector, key) and that one field is skipped, so one
        bad frame can never take down the whole CAN thread.
        """
        device = self._devices_by_key.get(device_key)
        if device is None:
            return None

        entries = self._datapoint_sets.get(device["datapoints"], [])
        matching_entry = None
        for entry in entries:
            if entry["selector"] == selector:
                matching_entry = entry
                break
        if matching_entry is None:
            return None

        attributes = {}
        telemetry = {}
        for field in matching_entry["fields"]:
            try:
                value = extract(payload, field)
            except BitfieldError as exc:
                warn_key = (device_key, selector, field["key"])
                if warn_key not in self._warned:
                    self._warned.add(warn_key)
                    if self._logger is not None:
                        self._logger.error(
                            "Uplink decode failed for %s selector=%s field=%s: %s",
                            device["deviceName"], selector, field["key"], exc,
                        )
                continue
            bucket = attributes if field["kind"] == "attribute" else telemetry
            bucket[field["key"]] = value

        return {
            "deviceName": device["deviceName"],
            "deviceType": device["deviceProfile"],
            "attributes": attributes,
            "telemetry": telemetry,
        }
