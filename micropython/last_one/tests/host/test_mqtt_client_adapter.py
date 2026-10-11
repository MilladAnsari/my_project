import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.mqtt_client_adapter import MqttClientAdapter, MockMqttClient  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class TestMqttClientAdapterFactory(unittest.TestCase):
    def test_create_falls_back_to_mock_on_host(self):
        adapter = MqttClientAdapter.create("client1", "localhost", 1883, logger_=get_logger("test"))
        self.assertIsInstance(adapter, MockMqttClient)


class TestMockMqttClient(unittest.TestCase):
    def setUp(self):
        self.client = MockMqttClient("client1", "localhost", 1883, None, None, 60, get_logger("test"))

    def test_publish_dict_auto_json_encoded(self):
        self.client.connect()
        self.client.publish("a/b", {"x": 1})
        self.assertEqual(len(self.client.published), 1)
        self.assertIn('"x"', self.client.published[0]["payload"])

    def test_subscribe_recorded(self):
        self.client.subscribe("a/b", qos=1)
        self.assertEqual(self.client.subscriptions, [("a/b", 1)])

    def test_check_msg_dispatches_to_callback(self):
        received = []
        self.client.set_callback(lambda topic, payload: received.append((topic, payload)))
        self.client.inject_message("a/b", {"x": 1})
        self.client.check_msg()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "a/b")

    def test_check_msg_noop_when_empty(self):
        received = []
        self.client.set_callback(lambda t, p: received.append(1))
        self.client.check_msg()
        self.assertEqual(received, [])

    def test_inject_message_str_payload(self):
        received = []
        self.client.set_callback(lambda t, p: received.append(p))
        self.client.inject_message("a/b", "hello")
        self.client.check_msg()
        self.assertEqual(received[0], b"hello")

    def test_is_connected_reflects_state(self):
        self.assertFalse(self.client.is_connected())
        self.client.connect()
        self.assertTrue(self.client.is_connected())
        self.client.disconnect()
        self.assertFalse(self.client.is_connected())

    def test_probe_requires_an_open_connection(self):
        with self.assertRaises(OSError):
            self.client.probe()
        self.client.connect()
        self.client.probe()


class _FakeUmqttClient:
    """Stands in for umqtt.robust/umqtt.simple's MQTTClient well enough
    to reproduce, on a desktop host, the exact crash confirmed on real
    hardware: umqtt's real __init__/connect() computes len(password)
    whenever `user` is set, regardless of whether password itself is
    None. This fake replicates that specific behaviour so
    test_none_password_is_coerced_to_empty_string_when_user_is_set
    actually catches a regression instead of passing regardless of what
    _NativeMqttClientAdapter does internally.
    """

    def __init__(self, client_id, host, port, user, password, keepalive):
        if user is not None:
            len(password)  # raises TypeError if password is None, like real umqtt
        self.client_id = client_id
        self.password = password

    def set_callback(self, callback):
        pass


class TestNativeMqttClientAdapterPasswordCoercion(unittest.TestCase):
    """Regression test for a real hardware-only bug: ThingsBoard's
    access-token auth (token as MQTT username, no password) crashed with
    "TypeError: object of type 'NoneType' has no len()" the moment
    connect() was called, because uplink_client.py passes password=None
    alongside a real username.
    """

    def _make_adapter(self, user, password):
        from tbgw.core.mqtt_client_adapter import _NativeMqttClientAdapter
        return _NativeMqttClientAdapter(
            _FakeUmqttClient, "client1", "host", 1883, user, password, 60, get_logger("test")
        )

    def test_none_password_is_coerced_to_empty_string_when_user_is_set(self):
        adapter = self._make_adapter(user="some-access-token", password=None)
        self.assertEqual(adapter._client.password, "")

    def test_explicit_password_is_left_alone(self):
        adapter = self._make_adapter(user="someuser", password="realpassword")
        self.assertEqual(adapter._client.password, "realpassword")

    def test_none_password_stays_none_when_no_user(self):
        # umqtt only computes len(password) when `user` is set -- no
        # coercion needed (or wanted) when there's no username at all.
        adapter = self._make_adapter(user=None, password=None)
        self.assertIsNone(adapter._client.password)


if __name__ == "__main__":
    unittest.main()
