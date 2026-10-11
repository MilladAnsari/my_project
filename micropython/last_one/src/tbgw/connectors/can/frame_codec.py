"""tbgw.connectors.can.frame_codec
=====================================

Turns a raw CAN frame's (arbitration_id, data) into the bus-agnostic
tuple (deviceKey, frameKind, selector, payload) that the rest of the
connector operates on, and back again for transmission. The mapping is
entirely described by a connector config's "frameProfile" section (see
docs/can_connector_design.md) -- this module has no knowledge of any
specific protocol, xxx included. A connector for a different,
offset-addressed bus (e.g. upstream ThingsBoard-Gateway's own model)
is just a different frameProfile: deviceKey = the whole arbitration ID,
no "selector"/"frameKind" section, payload = every data byte.

Pure and hardware-free: decode()/encode() do no I/O and touch no
driver/device state.
"""

from tbgw.utils.tb_utility import parse_can_id


class FrameCodecError(Exception):
    pass


class DecodedFrame:
    __slots__ = ("device_key", "frame_kind", "selector", "payload")

    def __init__(self, device_key, frame_kind, selector, payload):
        self.device_key = device_key
        self.frame_kind = frame_kind
        self.selector = selector
        self.payload = payload

    def __repr__(self):
        return "DecodedFrame(device_key=0x%X, frame_kind=%r, selector=%r, payload=%r)" % (
            self.device_key, self.frame_kind, self.selector, self.payload
        )


def _as_int(value, default=None):
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return parse_can_id(value)


class FrameCodec:
    """Constructed once per CAN connector from config["frameProfile"]."""

    def __init__(self, frame_profile):
        dk = frame_profile["deviceKey"]
        self._dk_mask = _as_int(dk.get("mask"), 0xFFFFFFFF)
        self._dk_shift = dk.get("shift", 0)

        fk = frame_profile.get("frameKind")
        if fk is not None:
            self._fk_mask = _as_int(fk.get("mask"), 0xFFFFFFFF)
            self._fk_shift = fk.get("shift", 0)
            self._fk_values = fk.get("values", {})
        else:
            self._fk_mask = None
            self._fk_values = None

        sel = frame_profile.get("selector")
        if sel is not None:
            self._sel_byte = sel["byte"]
            self._sel_mask = _as_int(sel.get("mask"), 0xFF)
            self._sel_absent_dlc = sel.get("absentWhenDlcAtMost", -1)
        else:
            self._sel_byte = None

        payload = frame_profile.get("payload")
        if payload is not None:
            self._payload_byte = payload["byte"]
            self._payload_len = payload.get("length", 1)
        else:
            self._payload_byte = None

        self._encode_spec = frame_profile.get("encode", {})

    def decode(self, arbitration_id, data):
        """Returns a DecodedFrame, or None if this profile's frameKind
        section is present but the frame's kind bits don't map to a
        known value. A None result means "not a frame this protocol
        recognizes" -- callers must skip it silently, not treat it as
        an error, since a shared bus can legitimately carry other
        traffic.
        """
        device_key = (arbitration_id & self._dk_mask) >> self._dk_shift

        frame_kind = None
        if self._fk_mask is not None:
            raw = (arbitration_id & self._fk_mask) >> self._fk_shift
            frame_kind = self._fk_values.get(str(raw))
            if frame_kind is None:
                return None

        selector = None
        if self._sel_byte is not None:
            if len(data) > self._sel_absent_dlc and len(data) > self._sel_byte:
                selector = data[self._sel_byte] & self._sel_mask

        if self._payload_byte is not None:
            start = self._payload_byte
            end = start + self._payload_len
            payload = bytes(data[start:end])
        else:
            payload = bytes(data)

        return DecodedFrame(device_key, frame_kind, selector, payload)

    def encode(self, device_key, selector, payload, op):
        """op: "read" or "write". Returns (arbitration_id: int, data: bytes).

        `payload` may be empty (e.g. a read command, which carries no
        real value) -- the "payload" byte producer then falls back to
        `encode.readPayloadFill` (default 0).
        """
        id_spec = self._encode_spec.get("id", {"from": "deviceKey"})
        if id_spec.get("from") != "deviceKey":
            raise FrameCodecError(
                "encode.id.from must be 'deviceKey' (only source currently supported)"
            )
        arbitration_id = device_key

        out = bytearray()
        for spec in self._encode_spec.get("data", []):
            source = spec.get("from")
            if source == "payload":
                out.append(payload[0] if payload else self._encode_spec.get("readPayloadFill", 0))
            elif source == "selector":
                value = selector
                or_spec = spec.get("or")
                if or_spec is not None and op in or_spec:
                    value = value | _as_int(or_spec[op])
                out.append(value)
            else:
                raise FrameCodecError("unsupported encode.data source: %r" % source)

        return arbitration_id, bytes(out)
