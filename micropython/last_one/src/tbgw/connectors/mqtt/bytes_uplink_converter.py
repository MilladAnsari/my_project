"""tbgw.connectors.mqtt.bytes_uplink_converter
=================================================

Converts a raw (non-JSON) MQTT payload into the ThingsBoard Gateway-API
telemetry/attribute shape, per a `converter` block from a config/mqtt.json
`dataMapping` entry:

    {
      "topicFilter": "sensor/raw/+/data",
      "converter": {
        "type": "bytes",
        "deviceNameExpressionSource": "topic",
        "deviceNameExpression": "sensor/raw/${deviceId}/data",
        "deviceTypeExpression": "default",
        "attributes": [ {"key": ..., "type": ..., "offset": ..., "size": ..., "signed": ..., "byteorder": ...}, ... ],
        "timeseries": [ ... same shape ... ]
      }
    }

Field extraction is delegated to tbgw.utils.tb_utility.unpack_value() --
the exact same decoding rules the CAN uplink converter uses (see
can_uplink_converter.py's docstring), so a device team that already wrote
a byte-layout spec for a CAN frame can reuse the same offset/type/signed/
byteorder attribute definitions for an MQTT "raw bytes" topic. Device
name resolution reuses the same "topic"/"message" logic as
json_uplink_converter.py via tbgw.utils.tb_utility.resolve_device_name();
"message" source renders against an empty context since a raw byte
payload has no dotted-path structure (a static deviceNameExpression is
the only sensible use of "message" source here).
"""

from tbgw.utils.tb_utility import unpack_value, resolve_device_name, topic_matches


class BytesMqttUplinkConverterError(Exception):
    pass


class BytesMqttUplinkConverter:
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
            raise BytesMqttUplinkConverterError(
                "dataMapping entry for topicFilter=%r is missing converter.deviceNameExpression"
                % self.topic_filter
            )

    def matches(self, topic):
        return topic_matches(topic, self.topic_filter)

    def convert(self, topic, payload_bytes):
        try:
            device_name = resolve_device_name(self.device_name_source, self.device_name_expression, topic)
        except ValueError as exc:
            raise BytesMqttUplinkConverterError(str(exc))

        attributes = self._extract_fields(payload_bytes, self._attribute_fields, "attribute")
        telemetry = self._extract_fields(payload_bytes, self._timeseries_fields, "timeseries")

        return {
            "deviceName": device_name,
            "deviceType": self.device_type_expression,
            "attributes": attributes,
            "telemetry": telemetry,
        }

    def _extract_fields(self, data, fields, field_kind):
        result = {}
        for field in fields:
            key = field["key"]
            value_type = field["type"]
            offset = field["offset"]
            size = field.get("size")
            signed = field.get("signed", True)
            byteorder = field.get("byteorder", "big")
            encoding = field.get("encoding", "ascii")
            try:
                result[key] = unpack_value(
                    data, offset, value_type,
                    signed=signed, encoding=encoding, byteorder=byteorder, size=size,
                )
            except (IndexError, ValueError, OverflowError) as exc:
                raise BytesMqttUplinkConverterError(
                    "Failed to decode %s field '%s' (topicFilter=%r, offset=%s, type=%s): %s"
                    % (field_kind, key, self.topic_filter, offset, value_type, exc)
                )
        return result
