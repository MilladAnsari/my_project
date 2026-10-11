import os
import sys
import json
import asyncio
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.gateway_service import TBGatewayService  # noqa: E402
from tbgw.core.constants import GATEWAY_TOPIC_RPC  # noqa: E402
from tbgw.storage.memory_storage import MemoryEventStorage  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class FakeConnector:
    """Stands in for a connector already registered in the device registry
    (e.g. the CAN connector, self-managed by main.py -- see
    gateway_service.py's docstring) so RPC/attribute-update routing can be
    tested without depending on the real CAN connector or hardware.
    """

    def __init__(self, name):
        self.name = name
        self.rpc_calls = []
        self.attribute_updates = []

    def on_rpc_request(self, device_name, rpc_request):
        self.rpc_calls.append((device_name, rpc_request))

    def on_attribute_update(self, device_name, attributes):
        self.attribute_updates.append((device_name, attributes))


def _write_temp_mqtt_config():
    config = {
        "broker": {"host": "localhost", "port": 1883, "clientId": "test-gw-mqtt"},
        "dataMapping": [],
        "requestsMapping": {},
    }
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(config, f)
    f.close()
    return f.name


class TestTBGatewayServiceIntegration(unittest.TestCase):
    def setUp(self):
        self.mqtt_config_path = _write_temp_mqtt_config()
        self.general_config = {
            "thingsboard": {
                "host": "tb.example.com", "port": 1883,
                "security": {"accessToken": "TEST_TOKEN"},
                "qos": 1, "minPackSendDelayMs": 10, "maxPackSize": 10,
            },
            "connectors": [
                {"name": "MQTT Broker Connector", "type": "mqtt", "configuration": self.mqtt_config_path},
                # "can" is intentionally present here to verify it's skipped
                # (self-managed by main.py, not TBGatewayService) rather than
                # causing a failed file-open on a path that doesn't exist in
                # this test's working directory.
                {"name": "CAN Connector", "type": "can", "configuration": "config/can.json"},
            ],
        }
        self.storage = MemoryEventStorage(maxsize=100)
        self.gateway = TBGatewayService(self.general_config, self.storage, logger=get_logger("test_gateway"))

    def tearDown(self):
        os.unlink(self.mqtt_config_path)

    def _run_scenario(self, body_coro_factory):
        async def scenario():
            task = asyncio.ensure_future(self.gateway.run())
            await asyncio.sleep(0.1)  # let connect + load_connectors + tasks start
            await body_coro_factory()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())

    def test_load_connectors_skips_can_loads_mqtt(self):
        async def body():
            pass

        self._run_scenario(body)
        self.assertIn("MQTT Broker Connector", self.gateway._connectors)
        self.assertNotIn("CAN Connector", self.gateway._connectors)

    def test_send_to_storage_flushes_to_uplink(self):
        async def body():
            self.gateway.send_to_storage("MQTT Broker Connector", {"deviceName": "X", "telemetry": {"t": 1}})
            await asyncio.sleep(0.15)

        self._run_scenario(body)
        self.assertEqual(self.storage.len(), 0)  # flushed and acked
        published_topics = [p["topic"] for p in self.gateway.uplink_client._adapter.published]
        self.assertIn("v1/gateway/telemetry", published_topics)

    def test_connect_device_registers_and_notifies_thingsboard(self):
        fake_connector = FakeConnector("Fake Connector")

        async def body():
            self.gateway.connect_device("Device Y", "default", fake_connector)
            await asyncio.sleep(0.2)

        self._run_scenario(body)
        self.assertIs(self.gateway.device_registry.get_connector("Device Y"), fake_connector)
        published_topics = [p["topic"] for p in self.gateway.uplink_client._adapter.published]
        self.assertIn("v1/gateway/connect", published_topics)

    def test_disconnect_device_unregisters_and_notifies_thingsboard(self):
        fake_connector = FakeConnector("Fake Connector")

        async def body():
            self.gateway.connect_device("Device Y", "default", fake_connector)
            await asyncio.sleep(0.15)
            self.gateway.disconnect_device("Device Y", fake_connector)
            await asyncio.sleep(0.15)

        self._run_scenario(body)
        self.assertFalse(self.gateway.device_registry.is_registered("Device Y"))
        published_topics = [p["topic"] for p in self.gateway.uplink_client._adapter.published]
        self.assertIn("v1/gateway/disconnect", published_topics)

    def test_rpc_request_routes_to_registered_connector_and_acks(self):
        fake_connector = FakeConnector("Fake Connector")

        async def body():
            self.gateway.connect_device("Device Y", "default", fake_connector)
            await asyncio.sleep(0.15)
            self.gateway.uplink_client._adapter.inject_message(
                GATEWAY_TOPIC_RPC, {"device": "Device Y", "data": {"id": 5, "method": "reboot", "params": None}}
            )
            # An RPC reply now crosses the uplink worker thread twice
            # (check_msg() picks up the request there -> inbox queue ->
            # _uplink_inbound_loop's own 0.1s poll -> dispatch -> reply
            # enqueued -> worker thread's outbox drain -> actual publish)
            # instead of happening synchronously within one asyncio tick,
            # so this needs more slack than a single-hop wait.
            await asyncio.sleep(0.3)

        self._run_scenario(body)
        self.assertEqual(fake_connector.rpc_calls, [("Device Y", {"method": "reboot", "params": None})])
        replies = [
            json.loads(p["payload"]) for p in self.gateway.uplink_client._adapter.published
            if p["topic"] == GATEWAY_TOPIC_RPC
        ]
        self.assertTrue(any(r.get("id") == 5 and r.get("data", {}).get("success") for r in replies))


    def test_send_to_storage_updates_device_state_store(self):
        async def body():
            self.gateway.send_to_storage(
                "CAN Connector", {"deviceName": "Light 1", "attributes": {"model": "x"}, "telemetry": {"key1": True}}
            )
            await asyncio.sleep(0.05)

        self._run_scenario(body)
        self.assertEqual(self.gateway.device_states.get("Light 1"), {"model": "x", "key1": True})

    def test_reconnect_resync_queues_device_connect_and_latest_state(self):
        connector = FakeConnector("CAN Connector")
        self.gateway.device_registry.register("Light 1", "light", connector)
        self.gateway.send_to_storage(
            "CAN Connector", {"deviceName": "Light 1", "attributes": {"enabledTarget": True},
                              "telemetry": {"enabledActual": True}}
        )
        self.gateway._resync_uplink_state()
        jobs = self.gateway.uplink_worker._outbox.get_batch(10)
        self.assertEqual(jobs[0], ("connect_device", "Light 1", "light"))
        self.assertEqual(jobs[1], ("event_pack", [{
            "deviceName": "Light 1", "attributes": {"enabledTarget": True},
            "telemetry": {"enabledActual": True},
        }]))

    def test_send_local_command_routes_to_registered_connector(self):
        fake_connector = FakeConnector("Fake Connector")

        async def body():
            self.gateway.connect_device("Light 1", "default", fake_connector)
            await asyncio.sleep(0.15)
            self.gateway.send_local_command("Light 1", "setKeyState", {"key": 1, "state": True})
            await asyncio.sleep(0.15)

        self._run_scenario(body)
        self.assertEqual(
            fake_connector.rpc_calls, [("Light 1", {"method": "setKeyState", "params": {"key": 1, "state": True}})]
        )

    def test_send_local_command_does_not_publish_thingsboard_rpc_reply(self):
        fake_connector = FakeConnector("Fake Connector")

        async def body():
            self.gateway.connect_device("Light 1", "default", fake_connector)
            await asyncio.sleep(0.15)
            self.gateway.send_local_command("Light 1", "setKeyState", {"key": 1, "state": True})
            await asyncio.sleep(0.15)

        self._run_scenario(body)
        rpc_publishes = [p for p in self.gateway.uplink_client._adapter.published if p["topic"] == GATEWAY_TOPIC_RPC]
        self.assertEqual(rpc_publishes, [])


    def test_get_connector_status_reports_mqtt_connector(self):
        captured = {}

        async def body():
            await asyncio.sleep(0.05)
            captured["status"] = self.gateway.get_connector_status()

        self._run_scenario(body)
        self.assertIn("MQTT Broker Connector", captured["status"])
        self.assertTrue(captured["status"]["MQTT Broker Connector"])  # MockMqttClient connects trivially

    def test_can_connector_slot_defaults_to_none(self):
        self.assertIsNone(self.gateway.can_connector)


if __name__ == "__main__":
    unittest.main()
