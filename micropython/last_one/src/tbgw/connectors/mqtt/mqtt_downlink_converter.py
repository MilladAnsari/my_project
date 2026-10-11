"""tbgw.connectors.mqtt.mqtt_downlink_converter
==================================================

Builds an outbound message (topic + payload) for the EXTERNAL broker
(the one config/mqtt.json's "broker" section points at -- NOT
ThingsBoard) from either a shared-attribute update or a server-side RPC
request, mirroring can_downlink_converter.py's role for the CAN
connector but producing an MQTT (topic, payload) pair instead of a CAN
(id, bytes) pair.

Example `attributeUpdates` entry (config/mqtt.json):
    {
      "deviceName": "Sensor 17", "attribute": "reportingInterval",
      "topicExpression": "sensor/${deviceName}/attributes/reportingInterval/set",
      "valueExpression": "value"
    }
Example `serverSideRpc` entry:
    {
      "deviceName": "Sensor 17", "method": "setColor",
      "topicExpression": "sensor/${deviceName}/rpc/setColor",
      "valueExpression": "params", "withResponse": false
    }

`topicExpression` is rendered via render_template() with {"deviceName": ...}
available. `valueExpression` is evaluated via safe_eval.evaluate() (eval()
accepted per project decision, same as the CAN downlink converter) with
`value` (attribute updates) or `params` (RPC) available; if the result is
already a string it's published as-is, otherwise it's JSON-encoded --
this keeps simple "publish a raw command string" configs simple while
still supporting structured payloads.
"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - host CPython fallback
    import json

from tbgw.utils.tb_utility import render_template
from tbgw.utils.safe_eval import evaluate


class MqttDownlinkConverterError(Exception):
    pass


class MqttDownlinkConverter:
    def __init__(self, mapping_entry, strict_eval=True, logger=None):
        self.mapping_entry = mapping_entry
        self.strict_eval = strict_eval
        self.logger = logger
        self.device_name = mapping_entry.get("deviceName")
        self.topic_expression = mapping_entry["topicExpression"]
        self.value_expression = mapping_entry.get("valueExpression", "value")
        self.qos = mapping_entry.get("qos", 0)
        # Attribute-update-specific:
        self.attribute_key = mapping_entry.get("attribute")
        # RPC-specific:
        self.method = mapping_entry.get("method")
        self.with_response = mapping_entry.get("withResponse", False)
        if self.with_response:
            (self.logger or _NULL_LOGGER).warning(
                "serverSideRpc entry for method '%s' sets withResponse=true, but "
                "synchronous response-waiting over the external MQTT broker is not "
                "implemented in this port; the command will be published fire-and-forget.",
                self.method,
            )

    def matches_attribute(self, device_name, attribute_key):
        return device_name == self.device_name and attribute_key == self.attribute_key

    def matches_rpc(self, device_name, method):
        return device_name == self.device_name and method == self.method

    def convert(self, context):
        """context: {"value": ...} for an attribute update, or
        {"params": ...} for an RPC call. Returns (topic: str, payload: bytes).
        """
        topic = render_template(self.topic_expression, {"deviceName": self.device_name})
        try:
            computed_value = evaluate(self.value_expression, context, strict=self.strict_eval)
        except ValueError as exc:
            raise MqttDownlinkConverterError(
                "Failed to evaluate valueExpression %r for topic %r: %s"
                % (self.value_expression, topic, exc)
            )
        if isinstance(computed_value, str):
            payload = computed_value.encode()
        else:
            payload = json.dumps(computed_value).encode()
        return topic, payload


class _NullLogger:
    def warning(self, *args, **kwargs):
        pass


_NULL_LOGGER = _NullLogger()


def build_converters_from_config(requests_mapping, strict_eval=True, logger=None):
    """Mirrors can_downlink_converter.build_converters_from_config(): builds
    the full set of MqttDownlinkConverter instances from config/mqtt.json's
    `requestsMapping.attributeUpdates` and `.serverSideRpc` lists.
    """
    attribute_converters = [
        MqttDownlinkConverter(entry, strict_eval=strict_eval, logger=logger)
        for entry in requests_mapping.get("attributeUpdates", [])
    ]
    rpc_converters = [
        MqttDownlinkConverter(entry, strict_eval=strict_eval, logger=logger)
        for entry in requests_mapping.get("serverSideRpc", [])
    ]
    return attribute_converters, rpc_converters
