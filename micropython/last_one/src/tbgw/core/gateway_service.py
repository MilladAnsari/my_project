"""tbgw.core.gateway_service
==============================

`TBGatewayService` is the asyncio-driven orchestrator that owns:

  * the ThingsBoard Gateway-API uplink connection (uplink_client.py),
  * the device registry (device_registry.py),
  * the RPC dispatcher (rpc_dispatcher.py),
  * the storage instance events flow through on their way from a
    connector to the uplink client (passed in from main.py, not created
    here -- see storage/memory_storage.py),
  * every ASYNCIO-MANAGED connector (currently: MQTTConnector).

Runs on Core 1 (tbgw.core.constants.CORE_GATEWAY) as the main asyncio
event loop. See docs/architecture.md.

Connector ownership split (important, and easy to miss): the CAN
connector is deliberately NOT loaded/owned here. It needs a dedicated
`_thread` with a tuned stack size and direct GPIO/TWAI access (see
can_connector.py), which main.py sets up directly before calling
`gateway.run()` -- passing this same TBGatewayService instance in as
`gateway=` so CANConnector can still call send_to_storage() /
connect_device() / disconnect_device() on it. `load_connectors()` below
only instantiates connector types in `SUPPORTED_CONNECTOR_TYPES`
(currently just "mqtt"); any other type in tb_gateway.json's
"connectors" list (i.e. "can") is skipped here with a debug log, since
main.py already handles it.

Cross-thread safety (see also device_registry.py, storage/memory_storage.py):
  * `send_to_storage()` may be called from the CAN thread. It only ever
    touches `self._storage`, which has its own internal lock -- safe.
  * `connect_device()`/`disconnect_device()` may also be called from the
    CAN thread. Rather than touching the device registry or publishing
    an MQTT message directly (umqtt clients are not thread-safe, and
    doing so would mean the CAN thread reaching into the gateway
    thread's MQTT socket), these just enqueue onto a thread-safe
    `Queue`; `_device_event_loop()` (an asyncio task on the gateway
    thread) is the only code that ever touches the device registry or
    calls `self.uplink_worker.gw_connect_device()`/`gw_disconnect_device()`.
  * `send_local_command()` (Phase 4, called from the GUI thread / Core 0)
    follows the identical enqueue pattern: `_local_command_loop()` (an
    asyncio task on the gateway thread) is the only code that ever calls
    into `rpc_dispatcher.dispatch_local_command()` / a connector's
    `on_rpc_request()`.

Uplink threading (Phase 4): `self.uplink_client` is the real
UplinkClient (umqtt-based) -- its `connect()` and a `qos=1` `publish()`
are genuinely blocking socket calls, which would otherwise stall this
entire asyncio loop for as long as the network takes. `self.uplink_worker`
(tbgw.core.uplink_worker.UplinkWorker) runs that client on its own
dedicated `_thread`, exactly mirroring how `can_connector.py` isolates
the TWAI driver. Everything on THIS asyncio side that used to call
`self.uplink_client.gw_*` directly now calls the equivalent
`self.uplink_worker.gw_*` method instead, which only ever enqueues a job
-- see uplink_worker.py's own docstring for the full design. `RpcDispatcher`
is constructed with `self.uplink_worker` (not `self.uplink_client`) for
the same reason; it still only calls `gw_send_rpc_reply()`, which the
worker exposes with an identical signature, so rpc_dispatcher.py itself
needed no changes.
"""

try:
    import uasyncio as asyncio
except ImportError:  # pragma: no cover - host CPython fallback
    import asyncio

from tbgw.connectors.mqtt.mqtt_connector import MQTTConnector
from tbgw.core import ntp
from tbgw.core.config_loader import load_connector_config
from tbgw.core.constants import DEFAULT_MIN_PACK_SEND_DELAY_MS, DEFAULT_MAX_PACK_SIZE
from tbgw.core.device_registry import DeviceRegistry
from tbgw.core.device_state_store import DeviceStateStore
from tbgw.core.rpc_dispatcher import RpcDispatcher
from tbgw.core.uplink_client import UplinkClient
from tbgw.core.uplink_worker import UplinkWorker
from tbgw.utils.logger import get_logger
from tbgw.utils.queue import Queue, Full
from tbgw.utils.tb_utility import unix_time_ms

# Connector types this service loads/owns as asyncio tasks. "can" is
# intentionally absent -- see this module's docstring.
SUPPORTED_CONNECTOR_TYPES = {
    "mqtt": MQTTConnector,
}

_DEVICE_EVENT_INTERVAL_S = 0.1
_HEARTBEAT_INTERVAL_S = 30
_RUNTIME_HEARTBEAT_INTERVAL_S = 1
_UPLINK_RESYNC_INTERVAL_S = 0.1


class TBGatewayService:
    def __init__(self, general_config, storage, logger=None, runtime_health=None):
        self._general_config = general_config
        self._storage = storage
        self._logger = logger or get_logger("gateway_service")

        thingsboard_config = general_config["thingsboard"]
        self.uplink_client = UplinkClient(thingsboard_config, logger=get_logger("uplink_client"))
        self.uplink_worker = UplinkWorker(self.uplink_client, logger=get_logger("uplink_worker"))
        self.device_registry = DeviceRegistry(logger=get_logger("device_registry"))
        self.device_states = DeviceStateStore()
        self.rpc_dispatcher = RpcDispatcher(self.device_registry, self.uplink_worker, logger=get_logger("rpc_dispatcher"))

        self._connectors = {}  # name -> connector instance (asyncio-managed only)
        self._device_events = Queue(maxsize=200)  # see module docstring
        self._local_commands = Queue(maxsize=100)  # GUI-issued commands, see module docstring
        self._running = False
        self._network_recovery_request = None
        self._uplink_was_healthy = False
        # A lightweight heartbeat lets main.py's hardware WDT distinguish
        # an alive asyncio gateway from a stalled event loop.  It is
        # intentionally unrelated to Wi-Fi/MQTT connection state.
        self._runtime_health = runtime_health

        # Set by main.py after it starts the (self-managed) CAN connector,
        # purely so the GUI (Phase 4) has one place to check CAN
        # connectivity for its status bar -- TBGatewayService itself never
        # calls anything on this reference. See docs/architecture.md's
        # "Connector ownership split" section for why CAN isn't in
        # self._connectors to begin with.
        self.can_connector = None

        self.min_pack_send_delay_s = thingsboard_config.get(
            "minPackSendDelayMs", DEFAULT_MIN_PACK_SEND_DELAY_MS
        ) / 1000.0
        self.max_pack_size = thingsboard_config.get("maxPackSize", DEFAULT_MAX_PACK_SIZE)

    # -- cross-thread-safe public API (called by connectors) -----------------

    def send_to_storage(self, connector_name, converted_data):
        event = dict(converted_data)
        event["_connector"] = connector_name
        # Only attach a timestamp once NTP has actually synced (tbgw.core.ntp) --
        # sending a pre-sync, wrong-epoch "ts" would be worse than omitting
        # it; ThingsBoard assigns receive time server-side when "ts" is
        # absent (see uplink_client.py's "Timestamp note").
        if ntp.is_synced():
            event["ts"] = unix_time_ms()
        try:
            self._storage.put(event)
        except Full:
            self._logger.warning("Storage full, dropping event from connector %s", connector_name)

        device_name = converted_data.get("deviceName")
        if device_name:
            self.device_states.update_uplink_values(
                device_name,
                attributes=converted_data.get("attributes"),
                telemetry=converted_data.get("telemetry"),
            )

    def send_local_command(self, device_name, method, params):
        """GUI-issued command entry point (Phase 4, Core 0 thread) --
        thread-safe, mirrors connect_device()/disconnect_device()'s
        enqueue pattern: the actual dispatch happens on the Core 1
        gateway thread via _local_command_loop(), so no GUI-thread code
        ever touches a connector or MQTT/CAN client directly. See
        docs/architecture.md, "The 'no optimistic GUI updates' rule".
        """
        try:
            self._local_commands.put_nowait((device_name, method, params))
        except Full:
            self._logger.warning(
                "Local command queue full, dropping command for %s (method=%s)", device_name, method
            )

    def set_network_recovery_request(self, request_event):
        """Attach main.py's cross-thread network-recovery signal.

        ``main.py`` owns Wi-Fi credentials and reconnection policy; this
        service only exposes a GUI-safe entry point to request that policy.
        """
        self._network_recovery_request = request_event

    def request_network_recovery(self):
        """Request Wi-Fi-then-ThingsBoard recovery without blocking the GUI."""
        if self._network_recovery_request is not None:
            self._network_recovery_request.set()
        else:
            # Useful for host tests or deployments that deliberately do not
            # create a Wi-Fi supervisor: still request a fresh MQTT session.
            self.uplink_worker.request_reconnect()

    def connect_device(self, device_name, device_type, connector):
        try:
            self._device_events.put_nowait(("connect", device_name, device_type, connector))
        except Full:
            self._logger.warning("Device event queue full, dropping connect event for %s", device_name)

    def disconnect_device(self, device_name, connector):
        try:
            self._device_events.put_nowait(("disconnect", device_name, None, connector))
        except Full:
            self._logger.warning("Device event queue full, dropping disconnect event for %s", device_name)

    # -- connector loading (asyncio-managed connectors only) -----------------

    def load_connectors(self):
        for entry in self._general_config.get("connectors", []):
            connector_type = entry.get("type")
            connector_cls = SUPPORTED_CONNECTOR_TYPES.get(connector_type)
            if connector_cls is None:
                self._logger.debug(
                    "Connector type '%s' is not asyncio-managed by TBGatewayService "
                    "(self-managed elsewhere, e.g. 'can' by main.py, or unsupported); skipping",
                    connector_type,
                )
                continue
            try:
                connector_config = load_connector_config(entry["configuration"])
            except OSError as exc:
                self._logger.error("Failed to load config for connector '%s': %s", entry.get("name"), exc)
                continue
            connector_name = entry.get("name", connector_type)
            connector = connector_cls(
                connector_name, connector_type, connector_config,
                gateway=self, logger=get_logger(connector_type + "_connector"),
            )
            self._connectors[connector_name] = connector
            self._logger.info("Loaded connector: %s (type=%s)", connector_name, connector_type)

    # -- main entry point ------------------------------------------------------

    async def run(self):
        self._running = True
        # Non-blocking: the worker thread handles its own connect-with-
        # retry loop independently from here on, so a ThingsBoard outage
        # (or Wi-Fi not being up yet) never delays connector loading or
        # any other task below.
        self.uplink_worker.start()

        self.load_connectors()
        for connector in self._connectors.values():
            connector.open()

        tasks = [
            asyncio.create_task(self._uplink_inbound_loop()),
            asyncio.create_task(self._uplink_flush_loop()),
            asyncio.create_task(self._device_event_loop()),
            asyncio.create_task(self._local_command_loop()),
            asyncio.create_task(self._uplink_resync_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self.stop()

    def stop(self):
        self._running = False
        for connector in self._connectors.values():
            try:
                connector.close()
            except Exception:
                self._logger.exception("Error closing connector %s", connector.name)
        # Do NOT touch self.uplink_client directly here (e.g. disconnect())
        # -- its socket is now owned by the uplink worker thread (see
        # uplink_worker.py), and this method runs on the asyncio thread.
        # uplink_worker.stop() signals the worker thread to exit; it
        # disconnects the real client itself, from the thread that
        # actually owns it.
        self.uplink_worker.stop()

    def get_connector_status(self):
        """Returns {connector_name: is_connected} for every asyncio-managed
        connector -- used by the GUI (Phase 4) status bar. Does NOT
        include the CAN connector (see `self.can_connector` instead,
        set directly by main.py).
        """
        return {name: connector.is_connected() for name, connector in self._connectors.items()}

    # -- asyncio tasks -----------------------------------------------------------

    async def _uplink_inbound_loop(self):
        """Drains RPC/attribute-update events that arrived on the uplink
        worker thread (see uplink_worker.py) and dispatches them here, on
        the gateway asyncio thread -- preserving the existing invariant
        that `rpc_dispatcher.handle_rpc_request()`/`handle_attribute_update()`,
        and therefore every connector's `on_rpc_request()`/
        `on_attribute_update()`, are only ever called from this thread.
        Replaces the old `_uplink_rx_loop`, which called
        `uplink_client.check_msg()` directly here -- that call itself
        wasn't the blocking concern (umqtt's check_msg() is non-blocking),
        but it now lives on the worker thread anyway since connect()/
        publish() had to move there regardless.
        """
        while self._running:
            for item in self.uplink_worker.get_inbound_batch(20):
                if item[0] == "rpc":
                    _, device_name, request_id, method, params = item
                    self.rpc_dispatcher.handle_rpc_request(device_name, request_id, method, params)
                elif item[0] == "attributes":
                    _, device_name, attributes = item
                    self.rpc_dispatcher.handle_attribute_update(device_name, attributes)
            await asyncio.sleep(_DEVICE_EVENT_INTERVAL_S)

    async def _uplink_flush_loop(self):
        while self._running:
            if self.uplink_worker.is_connected() and self._storage.len() > 0:
                pack = self._storage.get_event_pack(self.max_pack_size)
                if pack:
                    # Acks storage on successful ENQUEUE, not confirmed
                    # network delivery -- the actual publish now happens
                    # asynchronously on the uplink worker thread, so
                    # synchronous success/failure isn't observable here.
                    # This trades a small amount of delivery certainty
                    # (a pack could still be lost if the worker thread's
                    # publish call later fails) for not blocking this
                    # loop; MemoryEventStorage was already documented as
                    # non-durable across power loss, so this is a
                    # comparable-order tradeoff, not a new one. Sending
                    # telemetry+attributes as ONE job (enqueue_event_pack)
                    # also closes the old double-send bug, where a
                    # partial failure (telemetry sent, attributes failed)
                    # caused a retry to resend telemetry a second time.
                    self.uplink_worker.enqueue_event_pack(pack)
                    self._storage.event_pack_processing_done()
            await asyncio.sleep(self.min_pack_send_delay_s)

    async def _device_event_loop(self):
        while self._running:
            for kind, device_name, device_type, connector in self._device_events.get_batch(20):
                if kind == "connect":
                    self.device_registry.register(device_name, device_type, connector)
                    self.uplink_worker.gw_connect_device(device_name, device_type or "default")
                elif kind == "disconnect":
                    self.device_registry.unregister(device_name)
                    self.uplink_worker.gw_disconnect_device(device_name)
            await asyncio.sleep(_DEVICE_EVENT_INTERVAL_S)

    async def _local_command_loop(self):
        while self._running:
            for device_name, method, params in self._local_commands.get_batch(20):
                self.rpc_dispatcher.dispatch_local_command(device_name, method, params)
            await asyncio.sleep(_DEVICE_EVENT_INTERVAL_S)

    def _resync_uplink_state(self):
        """Restore Gateway device sessions and latest confirmed state once.

        After a broker reconnect, ThingsBoard has no Gateway API session for
        the child devices.  Re-sending ``connect`` restores RPC routing;
        replaying the last confirmed CAN telemetry and attributes wakes the
        dashboard without waiting for a physical switch change.
        """
        devices = self.device_registry.list_devices()
        names = []
        for device in devices:
            name = device["name"]
            names.append(name)
            self.uplink_worker.gw_connect_device(name, device.get("type") or "default")

        events = self.device_states.snapshot_uplink_events(names)
        if events:
            self.uplink_worker.enqueue_event_pack(events)
        self._logger.info(
            "ThingsBoard reconnect resync queued: devices=%d state_events=%d",
            len(names), len(events),
        )

    async def _uplink_resync_loop(self):
        """Perform exactly one replay for each unhealthy -> healthy change."""
        while self._running:
            try:
                healthy = self.uplink_worker.is_healthy()
            except Exception:
                healthy = False
            if healthy and not self._uplink_was_healthy:
                self._resync_uplink_state()
            self._uplink_was_healthy = healthy
            await asyncio.sleep(_UPLINK_RESYNC_INTERVAL_S)

    async def _heartbeat_loop(self):
        seconds_until_log = 0
        while self._running:
            if self._runtime_health is not None:
                self._runtime_health.beat("gateway")
            if seconds_until_log <= 0:
                self._logger.info(
                    "gateway heartbeat: uplink=%s storage_len=%s devices=%s connectors=%s",
                    "up" if self.uplink_worker.is_connected() else "down",
                    self._storage.len(),
                    self.device_registry.count(),
                    list(self._connectors.keys()),
                )
                seconds_until_log = _HEARTBEAT_INTERVAL_S
            seconds_until_log -= _RUNTIME_HEARTBEAT_INTERVAL_S
            await asyncio.sleep(_RUNTIME_HEARTBEAT_INTERVAL_S)
