import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.connectors.mqtt.json_uplink_converter import (  # noqa: E402
    JsonMqttUplinkConverter,
    JsonMqttUplinkConverterError,
)

MAPPING_ENTRY = {
    "topicFilter": "sensor/+/data",
    "converter": {
        "type": "json",
        "deviceNameExpressionSource": "topic",
        "deviceNameExpression": "sensor/${deviceId}/data",
        "attributes": [{"key": "model", "type": "string", "value": "${model}"}],
        "timeseries": [
            {"key": "temperature", "type": "double", "value": "${temp}"},
            {"key": "humidity", "type": "double", "value": "${hum}"},
        ],
    },
}


class TestJsonMqttUplinkConverter(unittest.TestCase):
    def test_matches_topic_filter(self):
        conv = JsonMqttUplinkConverter(MAPPING_ENTRY)
        self.assertTrue(conv.matches("sensor/17/data"))
        self.assertFalse(conv.matches("other/17/data"))

    def test_convert_extracts_device_name_from_topic(self):
        conv = JsonMqttUplinkConverter(MAPPING_ENTRY)
        payload = json.dumps({"model": "X1", "temp": 21.5, "hum": 40}).encode()
        result = conv.convert("sensor/17/data", payload)
        self.assertEqual(result["deviceName"], "17")
        self.assertEqual(result["attributes"], {"model": "X1"})
        self.assertEqual(result["telemetry"], {"temperature": 21.5, "humidity": 40.0})

    def test_convert_with_message_source(self):
        entry = {
            "topicFilter": "sensor/data",
            "converter": {
                "type": "json",
                "deviceNameExpressionSource": "message",
                "deviceNameExpression": "${deviceId}",
                "timeseries": [{"key": "temp", "type": "double", "value": "${temp}"}],
            },
        }
        conv = JsonMqttUplinkConverter(entry)
        payload = json.dumps({"deviceId": "sensor-9", "temp": 12.3}).encode()
        result = conv.convert("sensor/data", payload)
        self.assertEqual(result["deviceName"], "sensor-9")

    def test_missing_device_name_expression_raises_at_construction(self):
        bad_entry = {"topicFilter": "x", "converter": {"type": "json"}}
        with self.assertRaises(JsonMqttUplinkConverterError):
            JsonMqttUplinkConverter(bad_entry)

    def test_invalid_json_payload_raises(self):
        conv = JsonMqttUplinkConverter(MAPPING_ENTRY)
        with self.assertRaises(JsonMqttUplinkConverterError):
            conv.convert("sensor/17/data", b"not json{{{")

    def test_topic_not_matching_device_name_template_raises(self):
        conv = JsonMqttUplinkConverter(MAPPING_ENTRY)
        payload = json.dumps({}).encode()
        with self.assertRaises(JsonMqttUplinkConverterError):
            conv.convert("completely/different/topic/shape", payload)

    def test_multi_placeholder_template_rejected(self):
        entry = dict(MAPPING_ENTRY)
        entry["converter"] = dict(MAPPING_ENTRY["converter"])
        entry["converter"]["deviceNameExpression"] = "sensor/${a}/${b}"
        conv = JsonMqttUplinkConverter(entry)
        with self.assertRaises(JsonMqttUplinkConverterError):
            conv.convert("sensor/1/2", b"{}")

    def test_device_type_expression_static_default(self):
        conv = JsonMqttUplinkConverter(MAPPING_ENTRY)
        result = conv.convert("sensor/17/data", json.dumps({"model": "x", "temp": 1, "hum": 1}).encode())
        self.assertEqual(result["deviceType"], "default")


if __name__ == "__main__":
    unittest.main()
