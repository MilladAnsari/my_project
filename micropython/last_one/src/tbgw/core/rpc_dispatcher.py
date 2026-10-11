"""tbgw.core.rpc_dispatcher
=============================

Routes inbound RPC requests and shared-attribute updates from ThingsBoard
(delivered via UplinkClient's registered handlers) to whichever connector
owns the target device (looked up via DeviceRegistry), and -- for RPC --
sends an immediate acknowledgement back to ThingsBoard once the request
has been handed to the connector.

Also routes GUI-issued local commands (Phase 4, see
`dispatch_local_command()` below and docs/architecture.md's "no
optimistic GUI updates" section) through the same device_registry lookup
and connector interface, but WITHOUT any ThingsBoard ack -- a
locally-issued command has no ThingsBoard request_id to reply to.

Scope note (documented up front, matches the limitation already flagged
in can_downlink_converter.py): this port does not implement a real
request/response round-trip with the physical device. `on_rpc_request()`
on both connectors is fire-and-forget (queues a CAN frame / publishes an
MQTT message and returns immediately) -- there is no mechanism here for
"wait for the device to actually respond, then ack ThingsBoard with the
real result." The RPC reply sent back to ThingsBoard therefore reflects
only "was this request successfully handed off to a connector for a
recognized device+method", not "did the physical device confirm it".
This matches this project's "basic" RPC scope; a real request/response
loop (e.g. waiting for a specific CAN reply frame or MQTT response topic)
is a natural Phase 6+ enhancement, not attempted here.
"""


class RpcDispatcher:
    def __init__(self, device_registry, uplink_client, logger=None):
        self._device_registry = device_registry
        self._uplink_client = uplink_client
        self._logger = logger

    def handle_rpc_request(self, device_name, request_id, method, params):
        connector = self._device_registry.get_connector(device_name)
        if connector is None:
            self._logger.warning(
                "RPC for unknown device '%s' (method=%s) -- no connector owns this device", device_name, method
            )
            self._reply(device_name, request_id, {"success": False, "error": "unknown device"})
            return

        try:
            connector.on_rpc_request(device_name, {"method": method, "params": params})
        except Exception as exc:
            self._logger.exception("Connector %s raised handling RPC for %s", connector.name, device_name)
            self._reply(device_name, request_id, {"success": False, "error": str(exc)})
            return

        # See module docstring: this only confirms hand-off, not a real
        # device-side result.
        self._reply(device_name, request_id, {"success": True})

    def handle_attribute_update(self, device_name, attributes):
        connector = self._device_registry.get_connector(device_name)
        if connector is None:
            self._logger.warning(
                "Attribute update for unknown device '%s' -- no connector owns this device", device_name
            )
            return
        try:
            connector.on_attribute_update(device_name, attributes)
        except Exception:
            self._logger.exception(
                "Connector %s raised handling attribute update for %s", connector.name, device_name
            )

    def dispatch_local_command(self, device_name, method, params):
        """GUI-issued command path (Phase 4) -- see docs/architecture.md,
        "The 'no optimistic GUI updates' rule". Unlike handle_rpc_request(),
        there is no ThingsBoard request_id and no reply to send: the
        command was issued locally, not by ThingsBoard, so there's nothing
        to ack back to the cloud. The resulting device state change (if
        the command succeeds) reaches both ThingsBoard and the GUI through
        the normal uplink event pipeline once the device reports it --
        this method's only job is getting the command onto the bus.
        Returns True if a connector was found and the command was handed
        off to it (NOT whether the physical device executed it -- same
        fire-and-forget scope as handle_rpc_request(), see this module's
        docstring), False if no connector owns this device.
        """
        connector = self._device_registry.get_connector(device_name)
        if connector is None:
            self._logger.warning(
                "Local command for unknown device '%s' (method=%s) -- no connector owns this device",
                device_name, method,
            )
            return False
        try:
            connector.on_rpc_request(device_name, {"method": method, "params": params})
        except Exception:
            self._logger.exception(
                "Connector %s raised handling local command for %s (method=%s)",
                connector.name, device_name, method,
            )
            return False
        return True

    def _reply(self, device_name, request_id, result):
        if request_id is None:
            return  # one-way RPC (no reply expected/possible)
        try:
            self._uplink_client.gw_send_rpc_reply(device_name, request_id, result)
        except Exception:
            self._logger.exception("Failed to send RPC reply for %s (request_id=%s)", device_name, request_id)
