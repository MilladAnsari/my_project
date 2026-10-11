import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.device_registry import DeviceRegistry  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class FakeConnector:
    def __init__(self, name):
        self.name = name


class TestDeviceRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry(logger=get_logger("test"))
        self.connector = FakeConnector("CAN Connector")

    def test_register_and_get_connector(self):
        self.registry.register("Device A", "default", self.connector)
        self.assertIs(self.registry.get_connector("Device A"), self.connector)

    def test_get_device_type(self):
        self.registry.register("Device A", "thermometer", self.connector)
        self.assertEqual(self.registry.get_device_type("Device A"), "thermometer")

    def test_is_registered(self):
        self.assertFalse(self.registry.is_registered("Device A"))
        self.registry.register("Device A", "default", self.connector)
        self.assertTrue(self.registry.is_registered("Device A"))

    def test_unregister(self):
        self.registry.register("Device A", "default", self.connector)
        self.registry.unregister("Device A")
        self.assertFalse(self.registry.is_registered("Device A"))
        self.assertIsNone(self.registry.get_connector("Device A"))

    def test_unknown_device_returns_none(self):
        self.assertIsNone(self.registry.get_connector("Nonexistent"))
        self.assertIsNone(self.registry.get_device_type("Nonexistent"))

    def test_list_devices(self):
        self.registry.register("Device A", "default", self.connector)
        self.registry.register("Device B", "sensor", self.connector)
        devices = self.registry.list_devices()
        names = sorted(d["name"] for d in devices)
        self.assertEqual(names, ["Device A", "Device B"])

    def test_count(self):
        self.assertEqual(self.registry.count(), 0)
        self.registry.register("Device A", "default", self.connector)
        self.assertEqual(self.registry.count(), 1)

    def test_re_register_overwrites(self):
        other_connector = FakeConnector("MQTT Connector")
        self.registry.register("Device A", "default", self.connector)
        self.registry.register("Device A", "updated", other_connector)
        self.assertEqual(self.registry.get_device_type("Device A"), "updated")
        self.assertIs(self.registry.get_connector("Device A"), other_connector)


if __name__ == "__main__":
    unittest.main()
