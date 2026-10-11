"""tbgw.utils.bitfield
========================

Bit-field extraction/insertion for CAN/MQTT byte payloads, per the
register/selector schema in docs/can_connector_design.md. Payload bytes
are treated as one big-endian integer; a field's "bit"/"width" address a
window within that integer, LSB-first (bit 0 = least-significant bit of
the last byte).

Pure and hardware-free: this module knows nothing about frames, devices,
or buses -- it only turns (bytes, field-spec) into a Python value and
back.
"""


class BitfieldError(Exception):
    pass


def extract(payload, field):
    """Reads one field out of `payload` (bytes-like).

    `field` is a dict: required "bit" (int) and "width" (int, bits);
    optional "signed" (bool, default False, two's-complement over
    `width` bits), "scale" (default 1), "offset" (default 0, applied
    after scaling), "enum" (dict mapping str(int) -> label -- mutually
    exclusive with scale/offset), "type": "bool" (renders 0/1 as
    False/True instead of a scaled number).

    Returns the decoded Python value (bool / int / float / str-via-enum).
    Raises BitfieldError if bit+width doesn't fit within `payload`.
    """
    bit = field["bit"]
    width = field["width"]
    total_bits = len(payload) * 8
    if bit < 0 or width <= 0 or bit + width > total_bits:
        raise BitfieldError(
            "field bit=%d width=%d does not fit in a %d-bit payload"
            % (bit, width, total_bits)
        )

    raw = int.from_bytes(bytes(payload), "big")
    mask = (1 << width) - 1
    value = (raw >> bit) & mask

    if field.get("signed") and (value >> (width - 1)) & 1:
        value -= 1 << width

    enum = field.get("enum")
    if enum is not None:
        key = str(value)
        return enum[key] if key in enum else value

    if field.get("type") == "bool":
        return bool(value)

    scale = field.get("scale", 1)
    offset = field.get("offset", 0)
    if scale != 1 or offset != 0:
        value = value * scale + offset
    return value


def insert(byte_value, field, value):
    """Inverse of extract() for a SINGLE byte (0-255): returns a new byte
    value with `field`'s bits set from `value`, leaving every other bit
    of `byte_value` unchanged.

    Reserved for the read-modify-write downlink path
    (docs/can_connector_design.md section 6, "readModifyWrite") -- not
    exercised by any config this port ships yet, since every write this
    project targets fills a whole register, but kept small and correct
    so a future config can turn it on without a schema change. Only
    supports fields fully within one byte (bit+width <= 8); raises
    BitfieldError otherwise.
    """
    bit = field["bit"]
    width = field["width"]
    if bit + width > 8:
        raise BitfieldError(
            "insert() only supports fields within a single byte (bit=%d width=%d)"
            % (bit, width)
        )

    enum = field.get("enum")
    if enum is not None:
        reverse = {label: int(key) for key, label in enum.items()}
        if value in reverse:
            raw = reverse[value]
        else:
            raw = int(value)
    elif field.get("type") == "bool":
        raw = 1 if value else 0
    else:
        scale = field.get("scale", 1)
        offset = field.get("offset", 0)
        raw = int(round((value - offset) / scale)) if (scale != 1 or offset != 0) else int(value)

    mask = (1 << width) - 1
    if raw < 0 or raw > mask:
        raise BitfieldError("value %r out of range for a %d-bit field" % (value, width))

    cleared = byte_value & (~(mask << bit) & 0xFF)
    return cleared | ((raw & mask) << bit)
