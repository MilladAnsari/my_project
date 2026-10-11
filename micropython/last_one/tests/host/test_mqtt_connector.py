import os
import sys
import json
import asyncio
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.connectors.mqtt.mqtt_connector import MQTTConnector  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class FakeGateway:
    def __init__(self):
        self.stored_events = []
        self.connected_devices = []
        self.disconnected_devices = []

    def send_to_storage(self, connector_name, converted_data):
        self.stored_events.append((connector_name, converted_data))

    def connect_device(self, device_name, device_type, connector):
        self.connected_devices.append((device_name, device_type, connector.name))

    def disconnect_device(self, device_name, connector):
        self.disconnected_devices.append((device_name, connector.name))


CONFIG = {
    "broker": {"host": "localhost", "port": 1883, "clientId": "test-mqtt-connector"},
    "strictEval": True,
    "dataMapping": [
        {
            "topicFilter": "sensor/+/data",
            "converter": {
                "type": "json",
                "deviceNameExpressionSource": "topic",
                "deviceNameExpression": "sensor/${deviceId}/data",
                "timeseries": [{"key": "temp", "type": "double", "value": "${temp}"}],
            },
        }
    ],
    "requestsMapping": {
        "connectRequests": [
            {
                "topicFilter": "sensor/+/connect",
                "deviceNameExpressionSource": "topic",
                "deviceNameExpression": "sensor/${deviceId}/connect",
                "deviceType": "default",
            }
        ],
        "disconnectRequests": [
            {
                "topicFilter": "sensor/+/disconnect",
                "deviceNameExpressionSource": "topic",
                "deviceNameExpression": "sensor/${deviceId}/disconnect",
            }
        ],
        "attributeUpdates": [
            {
                "deviceName": "17",
                "attribute": "interval",
                "topicExpression": "sensor/${deviceName}/attributes/interval/set",
                "valueExpression": "value",
            }
        ],
        "serverSideRpc": [
            {
                "deviceName": "17",
                "method": "reboot",
                "topicExpression": "sensor/${deviceName}/rpc/reboot",
                "valueExpression": "params",
                "withResponse": False,
            }
        ],
    },
}


def run_async(coro):
    return asyncio.run(coro)


class TestMQTTConnectorUplink(unittest.TestCase):
    def _build(self):
        gateway = FakeGateway()
        connector = MQTTConnector("MQTT Connector", "mqtt", CONFIG, gateway=gateway, logger=get_logger("test_mqtt_connector"))
        return gateway, connector

    def test_telemetry_message_forwarded(self):
        gateway, connector = self._build()

        async def scenario():
            connector.open()
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/data", json.dumps({"temp": 21.5}).encode())
            await asyncio.sleep(0.15)
            connector.close()
            await asyncio.sleep(0.05)

        run_async(scenario())
        self.assertEqual(len(gateway.stored_events), 1)
        connector_name, converted = gateway.stored_events[0]
        self.assertEqual(connector_name, "MQTT Connector")
        self.assertEqual(converted["deviceName"], "17")
        self.assertAlmostEqual(converted["telemetry"]["temp"], 21.5, places=3)

    def test_connect_request_triggers_gateway_connect_device(self):
        gateway, connector = self._build()

        async def scenario():
            connector.open()
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/connect", b"")
            await asyncio.sleep(0.15)
            connector.close()
            await asyncio.sleep(0.05)

        run_async(scenario())
        self.assertEqual(gateway.connected_devices, [("17", "default", "MQTT Connector")])

    def test_disconnect_request_triggers_gateway_disconnect_device(self):
        gateway, connector = self._build()

        async def scenario():
            connector.open()
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/connect", b"")
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/disconnect", b"")
            await asyncio.sleep(0.15)
            connector.close()
            await asyncio.sleep(0.05)

        run_async(scenario())
        self.assertEqual(gateway.disconnected_devices, [("17", "MQTT Connector")])

    def test_malformed_payload_does_not_crash_connector(self):
        gateway, connector = self._build()

        async def scenario():
            connector.open()
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/data", b"not json{{{")
            await asyncio.sleep(0.1)
            connector._adapter.inject_message("sensor/17/data", json.dumps({"temp": 5}).encode())
            await asyncio.sleep(0.15)
            connector.close()
            await asyncio.sleep(0.05)

        run_async(scenario())
        self.assertEqual(len(gateway.stored_events), 1)  # only the well-formed one


class TestMQTTConnectorDownlink(unittest.TestCase):
    def test_attribute_update_publishes(self):
        gateway = FakeGateway()
        connector = MQTTConnector("MQTT Connector", "mqtt", CONFIG, gateway=gateway, logger=get_logger("test"))
        connector.on_attribute_update("17", {"interval": 30})
        pub = connector._adapter.published[-1]
        self.assertEqual(pub["topic"], "sensor/17/attributes/interval/set")
        self.assertEqual(json.loads(pub["payload"]), 30)

    def test_rpc_request_publishes(self):
        gateway = FakeGateway()
        connector = MQTTConnector("MQTT Connector", "mqtt", CONFIG, gateway=gateway, logger=get_logger("test"))
        connector.on_rpc_request("17", {"method": "reboot", "params": True})
        pub = connector._adapter.published[-1]
        self.assertEqual(pub["topic"], "sensor/17/rpc/reboot")

    def test_unrelated_attribute_ignored(self):
        gateway = FakeGateway()
        connector = MQTTConnector("MQTT Connector", "mqtt", CONFIG, gateway=gateway, logger=get_logger("test"))
        connector.on_attribute_update("17", {"unrelated": 1})
        self.assertEqual(connector._adapter.published, [])

    def test_unknown_rpc_method_ignored(self):
        gateway = FakeGateway()
        connector = MQTTConnector("MQTT Connector", "mqtt", CONFIG, gateway=gateway, logger=get_logger("test"))
        connector.on_rpc_request("17", {"method": "doesNotExist", "params": 1})
        self.assertEqual(connector._adapter.published, [])


if __name__ == "__main__":
    unittest.main()
