import time
import unittest

from tbgw.core.uplink_worker import UplinkWorker
from tbgw.utils.logger import get_logger


class FakeUplinkClient:
    """Stands in for UplinkClient: records every gw_* call, and lets a
    test simulate an inbound RPC/attribute-update by invoking whatever
    handler UplinkWorker registered, exactly as the real check_msg()
    would.
    """

    def __init__(self, connect_should_fail=False):
        self.connect_should_fail = connect_should_fail
        self.connect_calls = 0
        self.check_msg_calls = 0
        self.disconnect_calls = 0
        self.check_msg_should_fail = False
        self.probe_calls = 0
        self.probe_should_fail = False
        self.calls = []
        self._connected = False
        self._rpc_handler = None
        self._attribute_update_handler = None

    def connect(self):
        self.connect_calls += 1
        if self.connect_should_fail:
            raise OSError("connection refused")
        self._connected = True

    def disconnect(self):
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self):
        return self._connected

    def check_msg(self):
        self.check_msg_calls += 1
        if self.check_msg_should_fail:
            raise OSError("stale socket")

    def probe(self):
        self.probe_calls += 1
        if self.probe_should_fail:
            raise OSError("broker stopped")

    def set_rpc_handler(self, handler):
        self._rpc_handler = handler

    def set_attribute_update_handler(self, handler):
        self._attribute_update_handler = handler

    def gw_connect_device(self, name, device_type):
        self.calls.append(("connect_device", name, device_type))

    def gw_disconnect_device(self, name):
        self.calls.append(("disconnect_device", name))

    def gw_send_rpc_reply(self, name, request_id, result):
        self.calls.append(("rpc_reply", name, request_id, result))

    def gw_send_telemetry_batch(self, pack):
        self.calls.append(("telemetry_batch", pack))

    def gw_send_attributes_batch(self, pack):
        self.calls.append(("attributes_batch", pack))


def _wait_until(predicate, timeout_s=2.0, interval_s=0.02):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class TestUplinkWorkerRegistration(unittest.TestCase):
    def test_registers_itself_as_the_clients_handlers(self):
        client = FakeUplinkClient()
        UplinkWorker(client)
        self.assertIsNotNone(client._rpc_handler)
        self.assertIsNotNone(client._attribute_update_handler)


class TestUplinkWorkerEnqueueOnly(unittest.TestCase):
    """Confirms gw_* calls do NOT touch the real client synchronously --
    the whole point of this class is to keep blocking network calls off
    whatever thread calls these methods.
    """

    def setUp(self):
        self.client = FakeUplinkClient()
        self.worker = UplinkWorker(self.client, logger=get_logger("test"))

    def test_gw_connect_device_only_enqueues(self):
        self.worker.gw_connect_device("Dev", "light")
        self.assertEqual(self.client.calls, [])
        self.worker._drain_outbox()
        self.assertEqual(self.client.calls, [("connect_device", "Dev", "light")])

    def test_gw_disconnect_device_only_enqueues(self):
        self.worker.gw_disconnect_device("Dev")
        self.assertEqual(self.client.calls, [])
        self.worker._drain_outbox()
        self.assertEqual(self.client.calls, [("disconnect_device", "Dev")])

    def test_gw_send_rpc_reply_only_enqueues(self):
        self.worker.gw_send_rpc_reply("Dev", 7, {"success": True})
        self.assertEqual(self.client.calls, [])
        self.worker._drain_outbox()
        self.assertEqual(self.client.calls, [("rpc_reply", "Dev", 7, {"success": True})])

    def test_enqueue_event_pack_sends_telemetry_and_attributes_as_one_job(self):
        pack = [{"deviceName": "Dev", "telemetry": {"t": 1}, "attributes": {"a": 1}}]
        self.worker.enqueue_event_pack(pack)
        self.worker._drain_outbox()
        self.assertEqual(
            self.client.calls,
            [("telemetry_batch", pack), ("attributes_batch", pack)],
        )

    def test_outbox_full_drops_the_job_without_raising(self):
        worker = UplinkWorker(FakeUplinkClient(), outbox_maxsize=1, logger=get_logger("test"))
        worker.gw_connect_device("A", "light")
        worker.gw_connect_device("B", "light")  # queue full -- must not raise
        worker._drain_outbox()
        self.assertEqual(worker._client.calls, [("connect_device", "A", "light")])

    def test_a_failing_job_does_not_stop_the_rest_of_the_batch(self):
        client = FakeUplinkClient()
        client.gw_connect_device = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        worker = UplinkWorker(client, logger=get_logger("test"))
        worker.gw_connect_device("A", "light")
        worker.gw_disconnect_device("B")
        worker._drain_outbox()  # must not raise despite the first job failing
        self.assertEqual(client.calls, [("disconnect_device", "B")])


class TestUplinkWorkerInbound(unittest.TestCase):
    def test_rpc_request_is_queued_for_the_asyncio_side_to_drain(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client)
        client._rpc_handler("Dev", 5, "reboot", None)
        batch = worker.get_inbound_batch(10)
        self.assertEqual(batch, [("rpc", "Dev", 5, "reboot", None)])

    def test_attribute_update_is_queued_for_the_asyncio_side_to_drain(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client)
        client._attribute_update_handler("Dev", {"x": 1})
        batch = worker.get_inbound_batch(10)
        self.assertEqual(batch, [("attributes", "Dev", {"x": 1})])

    def test_inbox_full_drops_without_raising(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, inbox_maxsize=1, logger=get_logger("test"))
        client._rpc_handler("A", 1, "m", None)
        client._rpc_handler("B", 2, "m", None)  # queue full -- must not raise
        batch = worker.get_inbound_batch(10)
        self.assertEqual(len(batch), 1)


class TestUplinkWorkerThreaded(unittest.TestCase):
    """Exercises the real background thread (start()/stop()), not just
    the pure job-dispatch logic above.
    """

    def test_connects_and_drains_jobs_on_its_own_thread(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, poll_interval_s=0.01, logger=get_logger("test"))
        worker.start()
        try:
            self.assertTrue(_wait_until(lambda: client.is_connected()))
            worker.gw_connect_device("Dev", "light")
            self.assertTrue(_wait_until(lambda: ("connect_device", "Dev", "light") in client.calls))
        finally:
            worker.stop()
            _wait_until(lambda: not client.is_connected())

    def test_retries_connect_after_failure(self):
        client = FakeUplinkClient(connect_should_fail=True)
        worker = UplinkWorker(client, reconnect_backoff_s=0.02, poll_interval_s=0.01, logger=get_logger("test"))
        worker.start()
        try:
            self.assertTrue(_wait_until(lambda: client.connect_calls >= 2))
        finally:
            worker.stop()

    def test_stop_disconnects_the_client_from_the_worker_thread(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, poll_interval_s=0.01)
        worker.start()
        self.assertTrue(_wait_until(lambda: client.is_connected()))
        worker.stop()
        self.assertTrue(_wait_until(lambda: not client.is_connected()))

    def test_manual_reconnect_replaces_an_existing_session(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, poll_interval_s=0.01)
        worker.start()
        try:
            self.assertTrue(_wait_until(lambda: client.is_connected()))
            worker.request_reconnect()
            self.assertTrue(_wait_until(lambda: client.connect_calls >= 2))
            self.assertGreaterEqual(client.disconnect_calls, 1)
        finally:
            worker.stop()

    def test_check_message_failure_resets_the_stale_session(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, poll_interval_s=0.01)
        worker.start()
        try:
            self.assertTrue(_wait_until(lambda: client.is_connected()))
            client.check_msg_should_fail = True
            self.assertTrue(_wait_until(lambda: client.connect_calls >= 2))
            self.assertGreaterEqual(client.disconnect_calls, 1)
        finally:
            worker.stop()

    def test_failed_health_probe_marks_connection_unhealthy_and_reconnects(self):
        client = FakeUplinkClient()
        worker = UplinkWorker(client, poll_interval_s=0.01, health_probe_interval_s=0.02)
        worker.start()
        try:
            self.assertTrue(_wait_until(worker.is_healthy))
            client.probe_should_fail = True
            client.connect_should_fail = True
            self.assertTrue(_wait_until(lambda: client.probe_calls >= 1))
            self.assertTrue(_wait_until(lambda: not worker.is_healthy()))
            self.assertGreaterEqual(client.disconnect_calls, 1)
        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
