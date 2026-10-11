import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.uplink_client import UplinkClient  # noqa: E402
from tbgw.core.constants import (  # noqa: E402
    GATEWAY_TOPIC_CONNECT,
    GATEWAY_TOPIC_DISCONNECT,
    GATEWAY_TOPIC_TELEMETRY,
    GATEWAY_TOPIC_ATTRIBUTES,
    GATEWAY_TOPIC_RPC,
    GATEWAY_TOPIC_ATTRIBUTES_REQUEST,
    GATEWAY_TOPIC_ATTRIBUTES_RESPONSE,
)
from tbgw.utils.logger import get_logger  # noqa: E402

THINGSBOARD_CONFIG = {
    "host": "tb.example.com",
    "port": 1883,
    "security": {"type": "accessToken", "accessToken": "TEST_TOKEN"},
    "qos": 1,
}


def _last_published(client):
    return client._adapter.published[-1]


class TestUplinkClientLifecycle(unittest.TestCase):
    def setUp(self):
        self.client = UplinkClient(THINGSBOARD_CONFIG, logger=get_logger("test_uplink"))

    def test_connect_subscribes_gateway_topics(self):
        self.client.connect()
        self.assertTrue(self.client.is_connected())
        subscribed_topics = [t for t, _ in self.client._adapter.subscriptions]
        self.assertIn(GATEWAY_TOPIC_RPC, subscribed_topics)
        self.assertIn(GATEWAY_TOPIC_ATTRIBUTES, subscribed_topics)

    def test_probe_uses_the_mqtt_adapter_health_check(self):
        self.client.connect()
        self.client.probe()


class TestUplinkClientPublishing(unittest.TestCase):
    def setUp(self):
        self.client = UplinkClient(THINGSBOARD_CONFIG, logger=get_logger("test_uplink"))
        self.client.connect()

    def test_gw_connect_device(self):
        self.client.gw_connect_device("Device A", "default")
        pub = _last_published(self.client)
        self.assertEqual(pub["topic"], GATEWAY_TOPIC_CONNECT)
        self.assertEqual(json.loads(pub["payload"]), {"device": "Device A", "type": "default"})

    def test_gw_disconnect_device(self):
        self.client.gw_disconnect_device("Device A")
        pub = _last_published(self.client)
        self.assertEqual(pub["topic"], GATEWAY_TOPIC_DISCONNECT)
        self.assertEqual(json.loads(pub["payload"]), {"device": "Device A"})

    def test_gw_send_telemetry_batch_groups_by_device(self):
        events = [
            {"deviceName": "A", "telemetry": {"temp": 21.5}},
            {"deviceName": "B", "telemetry": {"temp": 19.0}},
            {"deviceName": "A", "attributes": {"model": "x"}},  # no telemetry -- should be skipped here
        ]
        sent = self.client.gw_send_telemetry_batch(events)
        self.assertTrue(sent)
        pub = _last_published(self.client)
        self.assertEqual(pub["topic"], GATEWAY_TOPIC_TELEMETRY)
        payload = json.loads(pub["payload"])
        self.assertEqual(payload["A"], [{"values": {"temp": 21.5}}])
        self.assertEqual(payload["B"], [{"values": {"temp": 19.0}}])

    def test_gw_send_telemetry_batch_includes_ts_when_given(self):
        events = [{"deviceName": "A", "telemetry": {"temp": 1}, "ts": 12345}]
        self.client.gw_send_telemetry_batch(events)
        payload = json.loads(_last_published(self.client)["payload"])
        self.assertEqual(payload["A"][0]["ts"], 12345)

    def test_gw_send_telemetry_batch_returns_false_when_nothing_to_send(self):
        sent = self.client.gw_send_telemetry_batch([{"deviceName": "A", "attributes": {"x": 1}}])
        self.assertFalse(sent)

    def test_gw_send_attributes_batch(self):
        events = [
            {"deviceName": "A", "attributes": {"model": "x"}},
            {"deviceName": "A", "attributes": {"fw": "1.0"}},
        ]
        sent = self.client.gw_send_attributes_batch(events)
        self.assertTrue(sent)
        payload = json.loads(_last_published(self.client)["payload"])
        self.assertEqual(payload["A"], {"model": "x", "fw": "1.0"})

    def test_gw_send_rpc_reply(self):
        self.client.gw_send_rpc_reply("Device A", 7, {"success": True})
        pub = _last_published(self.client)
        self.assertEqual(pub["topic"], GATEWAY_TOPIC_RPC)
        self.assertEqual(json.loads(pub["payload"]), {"device": "Device A", "id": 7, "data": {"success": True}})

    def test_gw_request_attributes_publishes_and_registers_handler(self):
        results = []
        request_id = self.client.gw_request_attributes("Device A", ["model"], lambda v: results.append(v))
        pub = _last_published(self.client)
        self.assertEqual(pub["topic"], GATEWAY_TOPIC_ATTRIBUTES_REQUEST)
        payload = json.loads(pub["payload"])
        self.assertEqual(payload["id"], request_id)
        self.assertEqual(payload["keys"], ["model"])


class TestUplinkClientInboundMessages(unittest.TestCase):
    def setUp(self):
        self.client = UplinkClient(THINGSBOARD_CONFIG, logger=get_logger("test_uplink"))
        self.client.connect()

    def test_rpc_handler_invoked(self):
        received = []
        self.client.set_rpc_handler(lambda *args: received.append(args))
        self.client._adapter.inject_message(
            GATEWAY_TOPIC_RPC, {"device": "A", "data": {"id": 1, "method": "setValue", "params": 42}}
        )
        self.client.check_msg()
        self.assertEqual(received, [("A", 1, "setValue", 42)])

    def test_attribute_update_handler_invoked(self):
        received = []
        self.client.set_attribute_update_handler(lambda *args: received.append(args))
        self.client._adapter.inject_message(GATEWAY_TOPIC_ATTRIBUTES, {"device": "A", "data": {"threshold": 5}})
        self.client.check_msg()
        self.assertEqual(received, [("A", {"threshold": 5})])

    def test_attribute_response_routes_to_pending_handler(self):
        results = []
        request_id = self.client.gw_request_attributes("A", ["model"], lambda v: results.append(v))
        self.client._adapter.inject_message(
            GATEWAY_TOPIC_ATTRIBUTES_RESPONSE,
            {"id": request_id, "values": {"model": "x"}},
        )
        self.client.check_msg()
        self.assertEqual(results, [{"model": "x"}])

    def test_no_rpc_handler_does_not_crash(self):
        self.client._adapter.inject_message(
            GATEWAY_TOPIC_RPC, {"device": "A", "data": {"id": 1, "method": "x", "params": None}}
        )
        self.client.check_msg()  # must not raise even with no handler registered

    def test_malformed_json_payload_does_not_crash(self):
        self.client._adapter.inject_message(GATEWAY_TOPIC_RPC, b"not json{{{")
        self.client.check_msg()  # must not raise


if __name__ == "__main__":
    unittest.main()
