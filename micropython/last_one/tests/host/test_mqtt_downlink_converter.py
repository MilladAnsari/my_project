import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.connectors.mqtt.mqtt_downlink_converter import (  # noqa: E402
    MqttDownlinkConverter,
    MqttDownlinkConverterError,
    build_converters_from_config,
)

ATTRIBUTE_ENTRY = {
    "deviceName": "sensor-17",
    "attribute": "reportingInterval",
    "topicExpression": "sensor/${deviceName}/attributes/reportingInterval/set",
    "valueExpression": "value",
    "qos": 1,
}

RPC_ENTRY = {
    "deviceName": "sensor-17",
    "method": "setColor",
    "topicExpression": "sensor/${deviceName}/rpc/setColor",
    "valueExpression": "params",
    "withResponse": False,
}


class TestMqttDownlinkConverterAttribute(unittest.TestCase):
    def test_matches_attribute(self):
        conv = MqttDownlinkConverter(ATTRIBUTE_ENTRY)
        self.assertTrue(conv.matches_attribute("sensor-17", "reportingInterval"))
        self.assertFalse(conv.matches_attribute("sensor-17", "other"))

    def test_convert_renders_topic_and_raw_string_payload(self):
        conv = MqttDownlinkConverter(ATTRIBUTE_ENTRY)
        topic, payload = conv.convert({"value": "60"})
        self.assertEqual(topic, "sensor/sensor-17/attributes/reportingInterval/set")
        self.assertEqual(payload, b"60")

    def test_convert_non_string_value_json_encoded(self):
        conv = MqttDownlinkConverter(ATTRIBUTE_ENTRY)
        topic, payload = conv.convert({"value": 60})
        self.assertEqual(json.loads(payload), 60)


class TestMqttDownlinkConverterRpc(unittest.TestCase):
    def test_matches_rpc(self):
        conv = MqttDownlinkConverter(RPC_ENTRY)
        self.assertTrue(conv.matches_rpc("sensor-17", "setColor"))
        self.assertFalse(conv.matches_rpc("sensor-17", "otherMethod"))

    def test_convert_dict_params_json_encoded(self):
        conv = MqttDownlinkConverter(RPC_ENTRY)
        topic, payload = conv.convert({"params": {"r": 255, "g": 0, "b": 0}})
        self.assertEqual(topic, "sensor/sensor-17/rpc/setColor")
        self.assertEqual(json.loads(payload), {"r": 255, "g": 0, "b": 0})

    def test_expression_over_params(self):
        entry = dict(RPC_ENTRY)
        entry["valueExpression"] = "'on' if params else 'off'"
        conv = MqttDownlinkConverter(entry)
        _, payload = conv.convert({"params": True})
        self.assertEqual(payload, b"on")

    def test_bad_expression_raises(self):
        entry = dict(RPC_ENTRY)
        entry["valueExpression"] = "params.nonexistent"
        conv = MqttDownlinkConverter(entry)
        with self.assertRaises(MqttDownlinkConverterError):
            conv.convert({"params": 5})

    def test_with_response_true_logs_warning(self):
        entry = dict(RPC_ENTRY)
        entry["withResponse"] = True

        class RecordingLogger:
            def __init__(self):
                self.warnings = []

            def warning(self, msg, *args):
                self.warnings.append(msg % args if args else msg)

        logger = RecordingLogger()
        MqttDownlinkConverter(entry, logger=logger)
        self.assertEqual(len(logger.warnings), 1)


class TestBuildConvertersFromConfig(unittest.TestCase):
    def test_builds_both_lists(self):
        requests_mapping = {"attributeUpdates": [ATTRIBUTE_ENTRY], "serverSideRpc": [RPC_ENTRY]}
        attr_converters, rpc_converters = build_converters_from_config(requests_mapping)
        self.assertEqual(len(attr_converters), 1)
        self.assertEqual(len(rpc_converters), 1)

    def test_missing_sections_default_empty(self):
        attr_converters, rpc_converters = build_converters_from_config({})
        self.assertEqual(attr_converters, [])
        self.assertEqual(rpc_converters, [])


if __name__ == "__main__":
    unittest.main()
