"""tbgw.connectors.mqtt.json_uplink_converter
================================================

Converts a JSON MQTT payload into the ThingsBoard Gateway-API telemetry/
attribute shape, per a `converter` block from a config/mqtt.json
`dataMapping` entry:

    {
      "topicFilter": "sensor/+/data",
      "converter": {
        "type": "json",
        "deviceNameExpressionSource": "topic",       # "topic" | "message"
        "deviceNameExpression": "sensor/${deviceId}/data",
        "deviceTypeExpression": "default",           # optional, defaults to "default"
        "attributes":  [ {"key": ..., "type": ..., "value": "${jsonField}"}, ... ],
        "timeseries":  [ ... same shape ... ]
      }
    }

Simplifications versus upstream (documented, not accidental -- see
architecture.md's "MicroPython gaps this port works around" table):
  * `deviceNameExpressionSource: "topic"` requires `deviceNameExpression`
    to contain EXACTLY ONE "${...}" placeholder (extracted via
    tbgw.utils.tb_utility.extract_template_vars()); upstream's regex-based
    extraction is more general, but this covers the common
    "topic segment IS the device id" case without needing a real regex
    engine (MicroPython's `re` lacks the lookaround upstream sometimes
    relies on -- see architecture.md section 3.5).
  * `deviceNameExpressionSource: "message"` renders `deviceNameExpression`
    against the parsed JSON payload via render_template() (dotted-path
    "${a.b}" lookups), not a full JSONPath implementation.
"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - host CPython fallback
    import json

from tbgw.utils.tb_utility import render_template, resolve_device_name, topic_matches


class JsonMqttUplinkConverterError(Exception):
    pass


def _coerce(value, value_type):
    """Best-effort type coercion for a decoded field value against its
    configured `type`. Falls back to the raw value on failure rather than
    raising -- a type mismatch here is a config/data quality issue best
    surfaced by ThingsBoard's own validation, not a reason to drop the
    whole converted event.
    """
    try:
        if value_type in ("double", "float"):
            return float(value)
        if value_type in ("int", "long"):
            return int(value)
        if value_type == "string":
            return str(value)
        if value_type in ("bool", "boolean"):
            return bool(value)
    except (TypeError, ValueError):
        pass
    return value


class JsonMqttUplinkConverter:
    def __init__(self, mapping_entry, logger=None):
        self.logger = logger
        self.topic_filter = mapping_entry["topicFilter"]
        converter_cfg = mapping_entry.get("converter", {})
        self.device_name_source = converter_cfg.get("deviceNameExpressionSource", "topic")
        self.device_name_expression = converter_cfg.get("deviceNameExpression")
        self.device_type_expression = converter_cfg.get("deviceTypeExpression", "default")
        self._attribute_fields = converter_cfg.get("attributes", [])
        self._timeseries_fields = converter_cfg.get("timeseries", [])

        if not self.device_name_expression:
            raise JsonMqttUplinkConverterError(
                "dataMapping entry for topicFilter=%r is missing converter.deviceNameExpression"
                % self.topic_filter
            )

    def matches(self, topic):
        return topic_matches(topic, self.topic_filter)

    def convert(self, topic, payload_bytes):
        try:
            payload = json.loads(payload_bytes)
        except ValueError as exc:
            raise JsonMqttUplinkConverterError("Payload on topic %r is not valid JSON: %s" % (topic, exc))

        device_name = self._extract_device_name(topic, payload)
        device_type = render_template(self.device_type_expression, payload)

        attributes = self._extract_fields(payload, self._attribute_fields)
        telemetry = self._extract_fields(payload, self._timeseries_fields)

        return {
            "deviceName": device_name,
            "deviceType": device_type,
            "attributes": attributes,
            "telemetry": telemetry,
        }

    def _extract_device_name(self, topic, payload):
        try:
            return resolve_device_name(self.device_name_source, self.device_name_expression, topic, payload)
        except ValueError as exc:
            raise JsonMqttUplinkConverterError(str(exc))

    def _extract_fields(self, payload, fields):
        result = {}
        for field in fields:
            key = field["key"]
            value_type = field.get("type", "string")
            value = render_template(field["value"], payload)
            result[key] = _coerce(value, value_type)
        return result
