import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.connectors.mqtt.bytes_uplink_converter import (  # noqa: E402
    BytesMqttUplinkConverter,
    BytesMqttUplinkConverterError,
)
from tbgw.utils.tb_utility import pack_value  # noqa: E402

MAPPING_ENTRY = {
    "topicFilter": "sensor/raw/+/data",
    "converter": {
        "type": "bytes",
        "deviceNameExpressionSource": "topic",
        "deviceNameExpression": "sensor/raw/${deviceId}/data",
        "deviceTypeExpression": "default",
        "attributes": [{"key": "batteryPercent", "type": "int", "offset": 0, "size": 1, "signed": False}],
        "timeseries": [{"key": "pressure", "type": "float", "offset": 1}],
    },
}


class TestBytesMqttUplinkConverter(unittest.TestCase):
    def test_matches(self):
        conv = BytesMqttUplinkConverter(MAPPING_ENTRY)
        self.assertTrue(conv.matches("sensor/raw/9/data"))
        self.assertFalse(conv.matches("sensor/9/data"))

    def test_convert_extracts_device_name_and_fields(self):
        conv = BytesMqttUplinkConverter(MAPPING_ENTRY)
        payload = pack_value(85, "int", signed=False, size=1) + pack_value(1013.25, "float")
        result = conv.convert("sensor/raw/9/data", payload)
        self.assertEqual(result["deviceName"], "9")
        self.assertEqual(result["deviceType"], "default")
        self.assertEqual(result["attributes"], {"batteryPercent": 85})
        self.assertAlmostEqual(result["telemetry"]["pressure"], 1013.25, places=2)

    def test_short_payload_raises(self):
        conv = BytesMqttUplinkConverter(MAPPING_ENTRY)
        with self.assertRaises(BytesMqttUplinkConverterError):
            conv.convert("sensor/raw/9/data", b"\x01")

    def test_missing_device_name_expression_raises_at_construction(self):
        bad_entry = {"topicFilter": "x", "converter": {"type": "bytes"}}
        with self.assertRaises(BytesMqttUplinkConverterError):
            BytesMqttUplinkConverter(bad_entry)


if __name__ == "__main__":
    unittest.main()
