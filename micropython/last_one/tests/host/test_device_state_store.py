import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.device_state_store import DeviceStateStore  # noqa: E402


class TestDeviceStateStore(unittest.TestCase):
    def setUp(self):
        self.store = DeviceStateStore()

    def test_unknown_device_returns_none(self):
        self.assertIsNone(self.store.get("Device A"))

    def test_update_and_get(self):
        self.store.update("Device A", {"on": True})
        self.assertEqual(self.store.get("Device A"), {"on": True})

    def test_update_merges_not_replaces(self):
        self.store.update("Device A", {"on": True})
        self.store.update("Device A", {"brightness": 80})
        self.assertEqual(self.store.get("Device A"), {"on": True, "brightness": 80})

    def test_update_overwrites_same_key(self):
        self.store.update("Device A", {"on": True})
        self.store.update("Device A", {"on": False})
        self.assertEqual(self.store.get("Device A"), {"on": False})

    def test_empty_update_is_noop(self):
        self.store.update("Device A", {})
        self.assertIsNone(self.store.get("Device A"))

    def test_get_returns_copy_not_reference(self):
        self.store.update("Device A", {"on": True})
        snapshot = self.store.get("Device A")
        snapshot["on"] = False
        self.assertEqual(self.store.get("Device A"), {"on": True})

    def test_snapshot_all_devices(self):
        self.store.update("Device A", {"on": True})
        self.store.update("Device B", {"position": "open"})
        snap = self.store.snapshot()
        self.assertEqual(snap, {"Device A": {"on": True}, "Device B": {"position": "open"}})

    def test_snapshot_uplink_events_preserves_telemetry_and_attributes(self):
        self.store.update_uplink_values("Device A", {"modeTarget": "heating"}, {"modeActual": "cooling"})
        self.assertEqual(self.store.snapshot_uplink_events(), [{
            "deviceName": "Device A",
            "attributes": {"modeTarget": "heating"},
            "telemetry": {"modeActual": "cooling"},
        }])

    def test_snapshot_uplink_events_can_be_limited_to_registered_devices(self):
        self.store.update_uplink_values("Device A", telemetry={"on": True})
        self.store.update_uplink_values("Device B", telemetry={"on": False})
        self.assertEqual(self.store.snapshot_uplink_events(["Device B"]), [{
            "deviceName": "Device B", "attributes": {}, "telemetry": {"on": False},
        }])

    def test_get_updated_at_tracks_updates(self):
        self.assertIsNone(self.store.get_updated_at("Device A"))
        self.store.update("Device A", {"on": True})
        self.assertIsNotNone(self.store.get_updated_at("Device A"))

    def test_clear(self):
        self.store.update("Device A", {"on": True})
        self.store.clear()
        self.assertIsNone(self.store.get("Device A"))


if __name__ == "__main__":
    unittest.main()
