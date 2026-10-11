"""tbgw.connectors.mqtt.mqtt_connector
=========================================

The MQTT connector. Subscribes to topics on an EXTERNAL MQTT broker
(config/mqtt.json's "broker" section -- unrelated to the ThingsBoard
uplink connection owned by tbgw.core.uplink_client), converts matching
messages via JsonMqttUplinkConverter/BytesMqttUplinkConverter, and
forwards results to the gateway's storage.

Unlike the CAN connector (which needs a dedicated `_thread` for
interrupt-driven bus access -- see can_connector.py), this connector runs
entirely as an asyncio task on the Core 1 gateway thread, per
docs/architecture.md: MQTT I/O is fundamentally socket-bound, which is
exactly what asyncio's cooperative model handles well without a
dedicated thread's RAM/stack overhead. `open()` schedules `_run()` as an
asyncio task and returns immediately; it must be called from a coroutine
context with a running event loop (TBGatewayService.run() -> load and
open connectors, per gateway_service.py).

Gateway interface expected -- identical contract to can_connector.py, see
that module's docstring for the exact signatures.
"""

try:
    import uasyncio as asyncio
except ImportError:  # pragma: no cover - host CPython fallback
    import asyncio

from tbgw.connectors.base import Connector
from tbgw.connectors.mqtt.json_uplink_converter import JsonMqttUplinkConverter, JsonMqttUplinkConverterError
from tbgw.connectors.mqtt.bytes_uplink_converter import BytesMqttUplinkConverter, BytesMqttUplinkConverterError
from tbgw.connectors.mqtt.mqtt_downlink_converter import (
    build_converters_from_config,
    MqttDownlinkConverterError,
)
from tbgw.core.mqtt_client_adapter import MqttClientAdapter
from tbgw.core.constants import DEFAULT_MQTT_PORT, DEFAULT_MQTT_KEEPALIVE_S
from tbgw.utils.tb_utility import generate_id, topic_matches, resolve_device_name

# How long the poll loop waits between check_msg() calls when connected.
_POLL_INTERVAL_S = 0.05
# Backoff between reconnect attempts after a connection failure/drop.
_RECONNECT_BACKOFF_S = 2.0


class MQTTConnector(Connector):
    def __init__(self, name, connector_type, config, gateway=None, logger=None):
        super().__init__(name, connector_type, config, gateway, logger)

        broker_cfg = config.get("broker", {})
        self._host = broker_cfg["host"]
        self._port = broker_cfg.get("port", DEFAULT_MQTT_PORT)
        client_id = broker_cfg.get("clientId") or ("tbgw-mqtt-" + generate_id(8))
        security = broker_cfg.get("security", {})

        self._adapter = MqttClientAdapter.create(
            client_id=client_id,
            host=self._host,
            port=self._port,
            user=security.get("username") or None,
            password=security.get("password") or None,
            keepalive=broker_cfg.get("keepalive", DEFAULT_MQTT_KEEPALIVE_S),
            logger_=self.logger,
        )
        self._adapter.set_callback(self._on_message)

        self._uplink_converters = self._build_uplink_converters(config.get("dataMapping", []))

        requests_mapping = config.get("requestsMapping", {})
        self._connect_requests = requests_mapping.get("connectRequests", [])
        self._disconnect_requests = requests_mapping.get("disconnectRequests", [])
        strict_eval = config.get("strictEval", True)
        self._attribute_downlink_converters, self._rpc_downlink_converters = build_converters_from_config(
            requests_mapping, strict_eval=strict_eval, logger=self.logger
        )

        self._connected_devices = set()
        self._running = False
        self._task = None

    def _build_uplink_converters(self, data_mapping):
        converters = []
        for entry in data_mapping:
            converter_type = entry.get("converter", {}).get("type", "json")
            try:
                if converter_type == "json":
                    converters.append(JsonMqttUplinkConverter(entry, logger=self.logger))
                elif converter_type == "bytes":
                    converters.append(BytesMqttUplinkConverter(entry, logger=self.logger))
                else:
                    self.logger.warning(
                        "Unknown converter type '%s' for topicFilter=%r, skipping",
                        converter_type, entry.get("topicFilter"),
                    )
            except (JsonMqttUplinkConverterError, BytesMqttUplinkConverterError) as exc:
                self.logger.error("Failed to build converter for topicFilter=%r: %s", entry.get("topicFilter"), exc)
        return converters

    # -- Connector interface --------------------------------------------------

    def open(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        self.logger.info("MQTT connector task created for broker %s:%s", self._host, self._port)

    def close(self):
        self._running = False
        if self._adapter.is_connected():
            try:
                self._adapter.disconnect()
            except Exception:
                pass

    def is_connected(self):
        return self._adapter.is_connected()

    def on_attribute_update(self, device_name, attribute_update):
        for key, value in attribute_update.items():
            converter = self._find_attribute_converter(device_name, key)
            if converter is not None:
                self._publish_downlink(converter, {"value": value})

    def on_rpc_request(self, device_name, rpc_request):
        method = rpc_request.get("method")
        converter = self._find_rpc_converter(device_name, method)
        if converter is None:
            self.logger.warning(
                "No serverSideRpc mapping for device=%s method=%s -- ignoring", device_name, method
            )
            return
        self._publish_downlink(converter, {"params": rpc_request.get("params")})

    # -- internals -------------------------------------------------------------

    def _find_attribute_converter(self, device_name, attribute_key):
        for converter in self._attribute_downlink_converters:
            if converter.matches_attribute(device_name, attribute_key):
                return converter
        return None

    def _find_rpc_converter(self, device_name, method):
        for converter in self._rpc_downlink_converters:
            if converter.matches_rpc(device_name, method):
                return converter
        return None

    def _publish_downlink(self, converter, context):
        try:
            topic, payload = converter.convert(context)
        except MqttDownlinkConverterError as exc:
            self.logger.error("Downlink conversion failed: %s", exc)
            return
        try:
            self._adapter.publish(topic, payload, qos=converter.qos)
            self.messages_sent += 1
            self.bytes_sent += len(payload)
        except Exception:
            self.logger.exception("Failed to publish downlink message to topic %s", topic)

    async def _run(self):
        # ⚠️ KNOWN DEFECT: `self._adapter.connect()` below is a BLOCKING
        # socket call made directly on the asyncio event loop. When the
        # broker is unreachable it blocks until the TCP connect times out
        # -- about 60 SECONDS on lwIP -- and for that whole time NOTHING
        # else on this loop runs: no GUI-issued command dispatch, no
        # device-event processing, no heartbeat.
        #
        # Measured on hardware with a broker configured at an address
        # that had no listener: the entire gateway advanced in ~60s
        # bursts (a 30s heartbeat arrived every 60s), and a light-switch
        # tap in the GUI took a minute or more to reach the CAN bus.
        # That presented as "the GUI is broken", which cost real
        # debugging time before it was traced here.
        #
        # This is the SAME flaw Phase 4 already fixed for the ThingsBoard
        # uplink by moving its client onto a dedicated thread (see
        # tbgw/core/uplink_worker.py); this connector never got the same
        # treatment. The proper fix is to give it the same worker-thread
        # isolation. Until then, do not enable this connector against a
        # broker that may be unreachable.
        while self._running:
            try:
                self._adapter.connect()
                self._subscribe_all()
                self.logger.info("MQTT connector connected to %s:%s", self._host, self._port)
                while self._running and self._adapter.is_connected():
                    self._adapter.check_msg()
                    await asyncio.sleep(_POLL_INTERVAL_S)
            except Exception:
                self.logger.exception("MQTT connector error, will retry")
            if self._running:
                await asyncio.sleep(_RECONNECT_BACKOFF_S)
        self.logger.info("MQTT connector task stopped")

    def _subscribe_all(self):
        for converter in self._uplink_converters:
            self._adapter.subscribe(converter.topic_filter)
        for entry in self._connect_requests:
            self._adapter.subscribe(entry["topicFilter"])
        for entry in self._disconnect_requests:
            self._adapter.subscribe(entry["topicFilter"])

    def _on_message(self, topic, payload):
        self.messages_received += 1
        self.bytes_received += len(payload)

        self._maybe_handle_connect_request(topic)
        self._maybe_handle_disconnect_request(topic)

        for converter in self._uplink_converters:
            if not converter.matches(topic):
                continue
            try:
                converted = converter.convert(topic, payload)
            except (JsonMqttUplinkConverterError, BytesMqttUplinkConverterError) as exc:
                self.logger.error("Uplink conversion failed: %s", exc)
                continue
            self._forward_to_gateway(converted)

    def _maybe_handle_connect_request(self, topic):
        for entry in self._connect_requests:
            if not topic_matches(topic, entry["topicFilter"]):
                continue
            device_name = self._resolve_request_device_name(topic, entry)
            if device_name is None or device_name in self._connected_devices:
                continue
            self._connected_devices.add(device_name)
            device_type = entry.get("deviceType", "default")
            self.logger.info("MQTT device connected: %s (type=%s)", device_name, device_type)
            if self.gateway is not None and hasattr(self.gateway, "connect_device"):
                self.gateway.connect_device(device_name, device_type, self)

    def _maybe_handle_disconnect_request(self, topic):
        for entry in self._disconnect_requests:
            if not topic_matches(topic, entry["topicFilter"]):
                continue
            device_name = self._resolve_request_device_name(topic, entry)
            if device_name is None or device_name not in self._connected_devices:
                continue
            self._connected_devices.discard(device_name)
            self.logger.info("MQTT device disconnected: %s", device_name)
            if self.gateway is not None and hasattr(self.gateway, "disconnect_device"):
                self.gateway.disconnect_device(device_name, self)

    def _resolve_request_device_name(self, topic, entry):
        source = entry.get("deviceNameExpressionSource", "topic")
        expression = entry.get("deviceNameExpression")
        if not expression:
            self.logger.warning(
                "connect/disconnect request for topicFilter=%r missing deviceNameExpression", entry.get("topicFilter")
            )
            return None
        try:
            return resolve_device_name(source, expression, topic)
        except ValueError as exc:
            self.logger.warning("Failed to resolve device name for topic %r: %s", topic, exc)
            return None

    def _forward_to_gateway(self, converted_data):
        self.messages_sent += 1
        if self.gateway is not None and hasattr(self.gateway, "send_to_storage"):
            self.gateway.send_to_storage(self.name, converted_data)
        else:
            self.logger.debug("(no gateway attached) converted MQTT event: %s", converted_data)
