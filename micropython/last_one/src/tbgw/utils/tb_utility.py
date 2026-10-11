"""tbgw.utils.tb_utility
==========================

Grab-bag of small helpers shared by the CAN and MQTT connectors (Phases 2/3)
that replace CPython-only functionality used by upstream thingsboard-gateway:

  * `generate_id()`       -- replaces `uuid.uuid4()` (module absent in MicroPython)
  * `pack_value()` / `unpack_value()`
                           -- replace the manual struct-based byte<->value
                              conversion upstream's CAN connector does, per
                              the `type`/`signed`/`encoding`/byte-order rules
                              documented at thingsboard.io/docs/iot-gateway/config/can/
  * `render_template()`   -- replaces the subset of upstream's
                              deviceNameExpression / dataExpression
                              "${key}" substitution that doesn't need a full
                              JSONPath or regex engine
  * `topic_matches()`     -- MQTT-style '+'/'#' wildcard topic matching,
                              used instead of `re` (ure's `findall`/lookaround
                              gap, see architecture.md) for the very common
                              case of "does this topic match this filter"
  * `get_nested()`        -- dotted-path lookup into a parsed JSON dict,
                              a light stand-in for the JSONPath expressions
                              upstream supports in `deviceNameJsonExpression`
"""

try:
    import ustruct as struct
except ImportError:  # pragma: no cover - host CPython fallback
    import struct

# struct.error's exact identity differs between ustruct (MicroPython) and
# struct (CPython): CPython's struct module exposes `.error`, but
# MicroPython's ustruct (confirmed on real ESP32-S3 firmware, MicroPython
# 1.27.0) has no `.error` attribute at all -- pack()/unpack() failures
# there raise a plain ValueError. Normalize to whichever is actually
# available so callers only need to catch one exception type.
try:
    _StructError = struct.error
except AttributeError:
    _StructError = ValueError

try:
    import os
except ImportError:  # pragma: no cover
    os = None

import time


# ---------------------------------------------------------------------------
# ID generation (uuid module is absent in MicroPython)
# ---------------------------------------------------------------------------

_HEX_DIGITS = "0123456789abcdef"


def generate_id(length=16):
    """Returns a random lowercase-hex id string of `length` characters.
    Not RFC-4122 UUID-formatted (no dashes/version bits) -- callers that
    need that specific shape should format this output themselves. Uses
    os.urandom when available (esp32 port has it); falls back to a
    time-seeded PRNG on hosts/ports that don't.
    """
    if os is not None and hasattr(os, "urandom"):
        raw = os.urandom((length + 1) // 2)
        return "".join(_HEX_DIGITS[b >> 4] + _HEX_DIGITS[b & 0x0F] for b in raw)[:length]
    # Fallback: not cryptographically random, only used if urandom is missing.
    import random
    random.seed(int(time.time() * 1000) & 0xFFFFFFFF)
    return "".join(_HEX_DIGITS[random.getrandbits(4)] for _ in range(length))


# ---------------------------------------------------------------------------
# Byte packing / unpacking for CAN payloads
# ---------------------------------------------------------------------------

# type name -> (struct format char, size in bytes) for FIXED-size types.
# "string" is handled separately (variable length, encoding-dependent).
_TYPE_FORMATS = {
    "bool": ("B", 1),
    "boolean": ("B", 1),
    "int": ("i", 4),      # ThingsBoard "long" <- CAN connector "int"
    "long": ("i", 4),
    "float": ("f", 4),
    "double": ("d", 8),
}

_SIGNED_INT_OVERRIDES = {
    1: ("b", "B"),
    2: ("h", "H"),
    4: ("i", "I"),
    8: ("q", "Q"),
}


def _byteorder_prefix(byteorder):
    # struct format prefixes: '>' big-endian, '<' little-endian
    return ">" if byteorder in ("big", ">", None) else "<"


def pack_value(value, value_type, signed=True, encoding="ascii", byteorder="big", size=None):
    """Packs a Python value into bytes following the same rules upstream's
    CAN connector documents for its `type`/`signed`/`encoding` attribute
    options. `size` is required (and used) for "string" and for integer
    widths other than the 4-byte default.

    Returns: bytes
    """
    value_type = value_type.lower()
    prefix = _byteorder_prefix(byteorder)

    if value_type == "string":
        data = str(value).encode(encoding or "ascii")
        if size is not None:
            data = data[:size].ljust(size, b"\x00")
        return data

    if value_type in ("int", "long"):
        width = size or 4
        signed_fmt, unsigned_fmt = _SIGNED_INT_OVERRIDES.get(width, ("i", "I"))
        fmt = prefix + (signed_fmt if signed else unsigned_fmt)
        try:
            return struct.pack(fmt, int(value))
        except _StructError as exc:
            raise ValueError("pack_value: %s" % exc)

    if value_type in ("bool", "boolean"):
        return struct.pack(prefix + "B", 1 if value else 0)

    if value_type == "float":
        try:
            return struct.pack(prefix + "f", float(value))
        except _StructError as exc:
            raise ValueError("pack_value: %s" % exc)

    if value_type == "double":
        try:
            return struct.pack(prefix + "d", float(value))
        except _StructError as exc:
            raise ValueError("pack_value: %s" % exc)

    raise ValueError("Unsupported value type for pack_value(): %s" % value_type)


def unpack_value(data, offset, value_type, signed=True, encoding="ascii", byteorder="big", size=None):
    """Reads a value out of `data` (bytes-like) starting at `offset`,
    following the same interpretation rules as upstream's CAN connector
    (see the module docstring). `size` is the byte length to read; for
    fixed-width numeric types it defaults to the type's natural width.

    Returns: the decoded Python value (bool / int / float / str)
    """
    value_type = value_type.lower()
    prefix = _byteorder_prefix(byteorder)

    if value_type == "string":
        length = size if size is not None else (len(data) - offset)
        raw = bytes(data[offset:offset + length])
        return raw.split(b"\x00", 1)[0].decode(encoding or "ascii")

    if value_type in ("int", "long"):
        width = size or 4
        signed_fmt, unsigned_fmt = _SIGNED_INT_OVERRIDES.get(width, ("i", "I"))
        fmt = prefix + (signed_fmt if signed else unsigned_fmt)
        try:
            return struct.unpack(fmt, bytes(data[offset:offset + width]))[0]
        except _StructError as exc:
            raise ValueError("unpack_value: %s" % exc)

    if value_type in ("bool", "boolean"):
        return bool(data[offset])

    if value_type == "float":
        try:
            return struct.unpack(prefix + "f", bytes(data[offset:offset + 4]))[0]
        except _StructError as exc:
            raise ValueError("unpack_value: %s" % exc)

    if value_type == "double":
        try:
            return struct.unpack(prefix + "d", bytes(data[offset:offset + 8]))[0]
        except _StructError as exc:
            raise ValueError("unpack_value: %s" % exc)

    raise ValueError("Unsupported value type for unpack_value(): %s" % value_type)


# ---------------------------------------------------------------------------
# Lightweight "${key}" template expansion
# ---------------------------------------------------------------------------

def render_template(expression, context):
    """Expands "${key}" / "${nested.key}" placeholders in `expression`
    against `context` (a dict, possibly nested). If the *entire* expression
    is a single placeholder (e.g. "${serialNumber}"), the resolved value is
    returned as-is (preserving its type); otherwise the result is always a
    string (placeholders are stringified and substituted in place), matching
    the way upstream's *Expression config fields behave.

    Unresolvable placeholders are left verbatim in the output rather than
    raising, so a misconfigured mapping fails loudly in the resulting
    device/attribute name instead of crashing the connector thread.
    """
    if not isinstance(expression, str):
        return expression

    if expression.startswith("${") and expression.endswith("}") and expression.count("${") == 1:
        key = expression[2:-1]
        value = get_nested(context, key)
        return expression if value is None else value

    result = []
    i = 0
    n = len(expression)
    while i < n:
        start = expression.find("${", i)
        if start == -1:
            result.append(expression[i:])
            break
        result.append(expression[i:start])
        end = expression.find("}", start)
        if end == -1:
            result.append(expression[start:])
            break
        key = expression[start + 2:end]
        value = get_nested(context, key)
        result.append(str(value) if value is not None else expression[start:end + 1])
        i = end + 1
    return "".join(result)


def get_nested(d, dotted_path, default=None):
    """dict/list-safe dotted-path lookup, e.g. get_nested(payload, "sensor.id").
    Returns `default` if any segment is missing.
    """
    node = d
    for segment in dotted_path.split("."):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return default
    return node


# ---------------------------------------------------------------------------
# MQTT-style topic wildcard matching ('+' single-level, '#' multi-level)
# ---------------------------------------------------------------------------

def topic_matches(topic, topic_filter):
    """True if `topic` (a concrete topic, e.g. "sensor/17/data") matches
    `topic_filter` (may contain '+' and '#' MQTT wildcards, e.g. "sensor/+/data"
    or "sensor/#"). This intentionally does NOT use `re`, both because
    MicroPython's `re` lacks the features (lookaround) some upstream
    filter-to-regex conversions rely on, and because MQTT topic matching
    has simple, well-defined semantics that don't need a regex engine at all.
    """
    topic_parts = topic.split("/")
    filter_parts = topic_filter.split("/")

    for i, fpart in enumerate(filter_parts):
        if fpart == "#":
            return True  # matches this level and everything after
        if i >= len(topic_parts):
            return False
        if fpart == "+":
            continue
        if fpart != topic_parts[i]:
            return False

    return len(topic_parts) == len(filter_parts)


# ---------------------------------------------------------------------------
# CAN arbitration ID parsing (shared by can_uplink_converter,
# can_downlink_converter, and can_connector -- kept here rather than
# duplicated in each so there is exactly one place that defines what a
# config-file CAN ID string means)
# ---------------------------------------------------------------------------

def parse_can_id(value):
    """Accepts an int, or a string like "0x700" (hex) or "1792" (decimal),
    as used for the "canId" field throughout config/can.json, and returns
    a plain int arbitration ID.
    """
    if isinstance(value, int):
        return value
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def resolve_device_name(source, expression, topic, payload=None):
    """Shared device-name-resolution logic for both MQTT uplink converters
    (json_uplink_converter.py, bytes_uplink_converter.py). `source` is
    "topic" or "message"; see json_uplink_converter.py's module docstring
    for the exact semantics and the documented "topic" simplification
    (exactly one "${...}" placeholder). Raises ValueError on failure so
    callers can wrap it in their own converter-specific error type.
    """
    if source == "topic":
        captured = extract_template_vars(expression, topic)
        if not captured:
            raise ValueError("Topic %r does not match deviceNameExpression template %r" % (topic, expression))
        if len(captured) != 1:
            raise ValueError(
                "deviceNameExpression %r must contain exactly one ${...} placeholder "
                "when deviceNameExpressionSource is 'topic' (found %d)" % (expression, len(captured))
            )
        return list(captured.values())[0]

    # "message" (or anything else): render against the payload (a dict for
    # JSON payloads; bytes_uplink_converter passes an empty dict since raw
    # byte payloads have no dotted-path structure to resolve against).
    return render_template(expression, payload or {})


def extract_template_vars(template, actual):
    """The inverse of render_template(): given a '/'-segmented template
    containing "${name}" placeholders (e.g. "sensor/${deviceId}/data") and
    a concrete string of the same segment shape (e.g. "sensor/17/data"),
    returns {"deviceId": "17"}. Returns None if `actual` doesn't have the
    same number of segments as `template`, or if any of the template's
    literal (non-placeholder) segments don't match `actual` exactly.

    Used by the MQTT connector's deviceNameExpressionSource="topic" mode
    (see connectors/mqtt/json_uplink_converter.py) in place of upstream's
    regex-based topic-to-device-name extraction, which relies on
    lookaround features MicroPython's `re` doesn't support.
    """
    template_parts = template.split("/")
    actual_parts = actual.split("/")
    if len(template_parts) != len(actual_parts):
        return None
    result = {}
    for tpart, apart in zip(template_parts, actual_parts):
        if tpart.startswith("${") and tpart.endswith("}"):
            result[tpart[2:-1]] = apart
        elif tpart != apart:
            return None
    return result


# ---------------------------------------------------------------------------
# Unix-epoch time (works around MicroPython's esp32-port RTC epoch gap)
# ---------------------------------------------------------------------------

_MICROPYTHON_EPOCH_OFFSET_S = 946684800  # seconds between 1970-01-01 and 2000-01-01
_epoch_is_2000 = None


def unix_time_ms():
    """Returns the current time as Unix-epoch milliseconds.

    MicroPython's esp32 port RTC defaults to (and, after
    tbgw.core.ntp.sync() calls ntptime.settime(), continues to use) a
    2000-01-01 epoch rather than 1970-01-01 -- confirmed via
    `time.gmtime(0)`'s year, checked once and cached. On CPython (host
    tests) `time.time()` is already Unix-epoch, so no correction is
    applied there. Callers should gate use of this on
    tbgw.core.ntp.is_synced() -- an un-synced clock is a real time, just
    not the right one.
    """
    global _epoch_is_2000
    if _epoch_is_2000 is None:
        _epoch_is_2000 = time.gmtime(0)[0] == 2000
    now = time.time()
    if _epoch_is_2000:
        now += _MICROPYTHON_EPOCH_OFFSET_S
    return int(now * 1000)
