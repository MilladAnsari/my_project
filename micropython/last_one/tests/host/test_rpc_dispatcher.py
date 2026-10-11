import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.core.device_registry import DeviceRegistry  # noqa: E402
from tbgw.core.rpc_dispatcher import RpcDispatcher  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class RecordingConnector:
    def __init__(self, name, raise_on_rpc=False):
        self.name = name
        self.raise_on_rpc = raise_on_rpc
        self.rpc_calls = []
        self.attribute_updates = []

    def on_rpc_request(self, device_name, rpc_request):
        if self.raise_on_rpc:
            raise RuntimeError("boom")
        self.rpc_calls.append((device_name, rpc_request))

    def on_attribute_update(self, device_name, attributes):
        self.attribute_updates.append((device_name, attributes))


class RecordingUplinkClient:
    def __init__(self):
        self.replies = []

    def gw_send_rpc_reply(self, device_name, request_id, result):
        self.replies.append((device_name, request_id, result))


class TestRpcDispatcherRpc(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry(logger=get_logger("test"))
        self.uplink = RecordingUplinkClient()
        self.dispatcher = RpcDispatcher(self.registry, self.uplink, logger=get_logger("test"))
        self.connector = RecordingConnector("CAN Connector")
        self.registry.register("Device A", "default", self.connector)

    def test_routes_to_owning_connector(self):
        self.dispatcher.handle_rpc_request("Device A", 1, "setValue", 42)
        self.assertEqual(self.connector.rpc_calls, [("Device A", {"method": "setValue", "params": 42})])

    def test_acks_success_on_dispatch(self):
        self.dispatcher.handle_rpc_request("Device A", 1, "setValue", 42)
        self.assertEqual(self.uplink.replies, [("Device A", 1, {"success": True})])

    def test_unknown_device_replies_failure(self):
        self.dispatcher.handle_rpc_request("Unknown Device", 2, "setValue", 1)
        self.assertEqual(self.uplink.replies, [("Unknown Device", 2, {"success": False, "error": "unknown device"})])

    def test_connector_exception_replies_failure(self):
        raising_connector = RecordingConnector("Broken Connector", raise_on_rpc=True)
        self.registry.register("Device B", "default", raising_connector)
        self.dispatcher.handle_rpc_request("Device B", 3, "setValue", 1)
        device, request_id, result = self.uplink.replies[0]
        self.assertEqual(device, "Device B")
        self.assertFalse(result["success"])

    def test_none_request_id_skips_reply(self):
        self.dispatcher.handle_rpc_request("Device A", None, "setValue", 42)
        self.assertEqual(self.uplink.replies, [])
        self.assertEqual(len(self.connector.rpc_calls), 1)


class TestRpcDispatcherAttributeUpdate(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry(logger=get_logger("test"))
        self.uplink = RecordingUplinkClient()
        self.dispatcher = RpcDispatcher(self.registry, self.uplink, logger=get_logger("test"))
        self.connector = RecordingConnector("CAN Connector")
        self.registry.register("Device A", "default", self.connector)

    def test_routes_to_owning_connector(self):
        self.dispatcher.handle_attribute_update("Device A", {"threshold": 5})
        self.assertEqual(self.connector.attribute_updates, [("Device A", {"threshold": 5})])

    def test_unknown_device_logged_not_raised(self):
        self.dispatcher.handle_attribute_update("Unknown Device", {"x": 1})  # must not raise

    def test_connector_exception_does_not_propagate(self):
        raising_connector = RecordingConnector("Broken Connector")
        raising_connector.on_attribute_update = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        self.registry.register("Device B", "default", raising_connector)
        self.dispatcher.handle_attribute_update("Device B", {"x": 1})  # must not raise


class TestRpcDispatcherLocalCommand(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry(logger=get_logger("test"))
        self.uplink = RecordingUplinkClient()
        self.dispatcher = RpcDispatcher(self.registry, self.uplink, logger=get_logger("test"))
        self.connector = RecordingConnector("CAN Connector")
        self.registry.register("Device A", "default", self.connector)

    def test_routes_to_owning_connector(self):
        result = self.dispatcher.dispatch_local_command("Device A", "setKeyState", {"key": 1, "state": True})
        self.assertTrue(result)
        self.assertEqual(
            self.connector.rpc_calls,
            [("Device A", {"method": "setKeyState", "params": {"key": 1, "state": True}})],
        )

    def test_no_thingsboard_ack_sent(self):
        self.dispatcher.dispatch_local_command("Device A", "setKeyState", {"key": 1, "state": True})
        self.assertEqual(self.uplink.replies, [])  # local commands never touch the TB reply path

    def test_unknown_device_returns_false(self):
        result = self.dispatcher.dispatch_local_command("Unknown Device", "setKeyState", {})
        self.assertFalse(result)

    def test_connector_exception_returns_false_and_does_not_propagate(self):
        raising_connector = RecordingConnector("Broken Connector", raise_on_rpc=True)
        self.registry.register("Device B", "default", raising_connector)
        result = self.dispatcher.dispatch_local_command("Device B", "setKeyState", {})
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
