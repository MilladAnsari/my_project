"""tbgw.core.uplink_client
============================

Implements the ThingsBoard Gateway MQTT API (v1/gateway/*) on top of
`tbgw.core.mqtt_client_adapter`. This is the ONE connection to
ThingsBoard itself -- separate from whatever broker the MQTT connector
(Phase 3, connectors/mqtt/) talks to, which is an arbitrary external
broker unrelated to ThingsBoard.

Topic contracts (see tbgw.core.constants, confirmed against
https://thingsboard.io/docs/reference/gateway-mqtt-api/):

    PUBLISH  v1/gateway/connect             {"device": ..., "type": ...}
    PUBLISH  v1/gateway/disconnect          {"device": ...}
    PUBLISH  v1/gateway/telemetry           {"<device>": [{"ts": ms, "values": {...}}, ...], ...}
    PUBLISH  v1/gateway/attributes          {"<device>": {...}, ...}
    PUBLISH  v1/gateway/attributes/request  {"id": n, "device": ..., "client": bool, "keys": [...]}
    PUBLISH  v1/gateway/rpc                 {"device": ..., "id": request_id, "data": result}   (reply)
    SUBSCRIBE v1/gateway/rpc                {"device": ..., "data": {"id": ..., "method": ..., "params": ...}}
    SUBSCRIBE v1/gateway/attributes         {"device": ..., "data": {...}}   (shared attribute push)
    SUBSCRIBE v1/gateway/attributes/response {"id": n, "values"/"value": {...}}

Timestamp note: `gw_send_telemetry_batch()` only includes a "ts" field
when the caller explicitly supplies one. MicroPython's on-device clock is
not guaranteed to be Unix-epoch-correct unless NTP-synced (ESP32 RTC
defaults to a 2000-01-01 epoch, not 1970-01-01), so this port deliberately
avoids silently sending a possibly-wrong timestamp -- omitting "ts" is
valid per the Gateway API and makes ThingsBoard assign the receive time
server-side instead. If NTP sync is added later (out of scope for Phase
3), callers can start passing an explicit "ts" per event.
"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - host CPython fallback
    import json

from tbgw.core.constants import (
    GATEWAY_TOPIC_CONNECT,
    GATEWAY_TOPIC_DISCONNECT,
    GATEWAY_TOPIC_TELEMETRY,
    GATEWAY_TOPIC_ATTRIBUTES,
    GATEWAY_TOPIC_ATTRIBUTES_REQUEST,
    GATEWAY_TOPIC_ATTRIBUTES_RESPONSE,
    GATEWAY_TOPIC_RPC,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_KEEPALIVE_S,
    DEFAULT_QOS,
)
from tbgw.core.mqtt_client_adapter import MqttClientAdapter
from tbgw.utils.tb_utility import generate_id


class UplinkClient:
    def __init__(self, thingsboard_config, logger=None):
        self._config = thingsboard_config
        self._logger = logger
        self._qos = thingsboard_config.get("qos", DEFAULT_QOS)

        security = thingsboard_config.get("security", {})
        client_id = security.get("clientId") or ("tbgw-esp32s3-" + generate_id(8))

        self._adapter = MqttClientAdapter.create(
            client_id=client_id,
            host=thingsboard_config["host"],
            port=thingsboard_config.get("port", DEFAULT_MQTT_PORT),
            user=security.get("accessToken"),
            password=None,
            keepalive=thingsboard_config.get("keepalive", DEFAULT_MQTT_KEEPALIVE_S),
            logger_=logger,
        )
        self._adapter.set_callback(self._on_message)

        self._rpc_handler = None  # callback(device_name, request_id, method, params)
        self._attribute_update_handler = None  # callback(device_name, attributes_dict)
        self._attribute_response_handlers = {}  # request_id -> callback(attributes_dict)
        self._next_request_id = 1

    # -- lifecycle --------------------------------------------------------

    def connect(self):
        self._adapter.connect()
        self._adapter.subscribe(GATEWAY_TOPIC_RPC, qos=self._qos)
        self._adapter.subscribe(GATEWAY_TOPIC_ATTRIBUTES, qos=self._qos)
        self._adapter.subscribe(GATEWAY_TOPIC_ATTRIBUTES_RESPONSE, qos=self._qos)
        self._logger.info(
            "Connected to ThingsBoard Gateway API at %s:%s",
            self._config["host"], self._config.get("port", DEFAULT_MQTT_PORT),
        )

    def disconnect(self):
        self._adapter.disconnect()

    def is_connected(self):
        return self._adapter.is_connected()

    def check_msg(self):
        """Non-blocking; call periodically from an asyncio task."""
        self._adapter.check_msg()

    def probe(self):
        """Confirm that the ThingsBoard MQTT peer replies to PINGREQ."""
        self._adapter.probe(timeout_s=2)

    # -- handler registration (wired by TBGatewayService to rpc_dispatcher) --

    def set_rpc_handler(self, handler):
        self._rpc_handler = handler

    def set_attribute_update_handler(self, handler):
        self._attribute_update_handler = handler

    # -- Gateway API operations --------------------------------------------

    def gw_connect_device(self, device_name, device_type="default"):
        self._adapter.publish(
            GATEWAY_TOPIC_CONNECT, {"device": device_name, "type": device_type}, qos=self._qos
        )
        self._logger.info("gw_connect_device: %s (type=%s)", device_name, device_type)

    def gw_disconnect_device(self, device_name):
        self._adapter.publish(GATEWAY_TOPIC_DISCONNECT, {"device": device_name}, qos=self._qos)
        self._logger.info("gw_disconnect_device: %s", device_name)

    def gw_send_telemetry_batch(self, events):
        """events: iterable of {"deviceName": str, "telemetry": dict, "ts": optional int}.
        Events with no "telemetry" (or an empty one) are skipped. Returns
        True if anything was actually published.
        """
        payload = {}
        for event in events:
            values = event.get("telemetry")
            if not values:
                continue
            device_name = event["deviceName"]
            entry = {"values": values}
            if "ts" in event:
                entry["ts"] = event["ts"]
            payload.setdefault(device_name, []).append(entry)
        if not payload:
            return False
        self._adapter.publish(GATEWAY_TOPIC_TELEMETRY, payload, qos=self._qos)
        return True

    def gw_send_attributes_batch(self, events):
        """events: iterable of {"deviceName": str, "attributes": dict}.
        Events with no "attributes" (or an empty one) are skipped.
        """
        payload = {}
        for event in events:
            values = event.get("attributes")
            if not values:
                continue
            device_name = event["deviceName"]
            payload.setdefault(device_name, {}).update(values)
        if not payload:
            return False
        self._adapter.publish(GATEWAY_TOPIC_ATTRIBUTES, payload, qos=self._qos)
        return True

    def gw_send_rpc_reply(self, device_name, request_id, result):
        self._adapter.publish(
            GATEWAY_TOPIC_RPC, {"device": device_name, "id": request_id, "data": result}, qos=self._qos
        )

    def gw_request_attributes(self, device_name, keys, callback, client_scope=True):
        """Fire-and-forget async request: publishes an attributes/request
        message and registers `callback(attributes_dict)` to run when the
        matching attributes/response message arrives (matched by request
        id). There is no timeout/retry here -- if ThingsBoard never
        replies, the callback simply never fires; callers needing a
        timeout should track that themselves.
        """
        request_id = self._next_request_id
        self._next_request_id += 1
        self._attribute_response_handlers[request_id] = callback
        self._adapter.publish(
            GATEWAY_TOPIC_ATTRIBUTES_REQUEST,
            {"id": request_id, "device": device_name, "client": client_scope, "keys": keys},
            qos=self._qos,
        )
        return request_id

    # -- inbound message handling -------------------------------------------

    def _on_message(self, topic, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            self._logger.warning("Received non-JSON payload on %s, ignoring", topic)
            return

        if topic == GATEWAY_TOPIC_RPC:
            self._handle_rpc(data)
        elif topic == GATEWAY_TOPIC_ATTRIBUTES:
            self._handle_attribute_update(data)
        elif topic == GATEWAY_TOPIC_ATTRIBUTES_RESPONSE:
            self._handle_attribute_response(data)
        else:
            self._logger.debug("Unhandled uplink message on topic %s", topic)

    def _handle_rpc(self, data):
        device_name = data.get("device")
        rpc = data.get("data", {})
        if self._rpc_handler is not None:
            self._rpc_handler(device_name, rpc.get("id"), rpc.get("method"), rpc.get("params"))
        else:
            self._logger.warning("No RPC handler registered, dropping RPC for %s", device_name)

    def _handle_attribute_update(self, data):
        device_name = data.get("device")
        attributes = data.get("data", {})
        if self._attribute_update_handler is not None:
            self._attribute_update_handler(device_name, attributes)
        else:
            self._logger.warning(
                "No attribute-update handler registered, dropping update for %s", device_name
            )

    def _handle_attribute_response(self, data):
        request_id = data.get("id")
        handler = self._attribute_response_handlers.pop(request_id, None)
        if handler is not None:
            handler(data.get("values", data.get("value", {})))
        else:
            self._logger.debug("No pending handler for attribute response id=%s", request_id)
