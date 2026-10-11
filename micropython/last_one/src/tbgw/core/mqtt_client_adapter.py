"""tbgw.core.mqtt_client_adapter
==================================

Thin adapter over MicroPython's `umqtt.robust`/`umqtt.simple`, isolated
behind a small stable interface (`MqttClientAdapter`) for the same two
reasons `twai_driver.py` isolates the CAN native module (see that file's
docstring): a single place to absorb API differences, and a software mock
(`MockMqttClient`) that lets every piece of MQTT-dependent logic --
ThingsBoard Gateway-API framing, the MQTT connector's topic/converter
matching, RPC/attribute-update dispatch -- be unit-tested on a desktop
host with no real broker.

Both `tbgw.core.uplink_client` (talks to ThingsBoard) and
`tbgw.connectors.mqtt.mqtt_connector` (talks to an arbitrary external
broker) are built on top of this adapter, since they need the same
primitive operations even though they serve different purposes.

Concurrency note (see docs/architecture.md): all instances of this
adapter that matter for this port -- the ThingsBoard uplink connection
and the MQTT connector's external-broker connection -- are driven
exclusively from asyncio tasks on the Core 1 gateway thread via
`check_msg()` polling, never from the CAN thread. `check_msg()` is
umqtt's non-blocking "process one pending message if any, else return
immediately" call, which is what makes cooperative polling from an
asyncio task work at all without a dedicated blocking-recv thread per
MQTT connection.
"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - host CPython fallback
    import json

try:
    import usocket as socket
except ImportError:  # pragma: no cover - CPython/alternate firmware fallback
    try:
        import socket
    except ImportError:  # pragma: no cover - extremely minimal firmware
        socket = None

from tbgw.utils.logger import get_logger

logger = get_logger("mqtt_client_adapter")


class MqttClientAdapter:
    """Public interface used by uplink_client.py and mqtt_connector.py.
    Construct via `MqttClientAdapter.create(...)`.
    """

    @staticmethod
    def create(client_id, host, port, user=None, password=None, keepalive=60, logger_=None):
        log = logger_ or logger
        try:
            from umqtt.robust import MQTTClient as _UmqttClient
            return _NativeMqttClientAdapter(_UmqttClient, client_id, host, port, user, password, keepalive, log)
        except ImportError:
            pass
        try:
            from umqtt.simple import MQTTClient as _UmqttClient
            return _NativeMqttClientAdapter(_UmqttClient, client_id, host, port, user, password, keepalive, log)
        except ImportError:
            log.warning(
                "Neither umqtt.robust nor umqtt.simple is importable -- falling back to "
                "MockMqttClient. This is expected on a desktop/host interpreter; on real "
                "hardware this means the umqtt package is missing from the firmware/filesystem."
            )
            return MockMqttClient(client_id, host, port, user, password, keepalive, log)

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def is_connected(self):
        raise NotImplementedError

    def subscribe(self, topic, qos=0):
        raise NotImplementedError

    def publish(self, topic, payload, qos=0, retain=False):
        raise NotImplementedError

    def set_callback(self, callback):
        """callback(topic: str, payload: bytes) is invoked from within
        check_msg() whenever a message arrives on a subscribed topic.
        """
        raise NotImplementedError

    def check_msg(self):
        """Non-blocking: processes at most one pending incoming message
        (invoking the registered callback), or returns immediately if none
        is pending. Must be safe to call frequently in a tight asyncio
        polling loop.
        """
        raise NotImplementedError

    def probe(self, timeout_s=2):
        """Verify that the MQTT peer is still reachable.

        Implementations must raise ``OSError`` when the connection cannot
        be confirmed inside ``timeout_s`` seconds. This does more than
        returning ``is_connected()``: that flag only describes the local TCP
        socket and can remain true after an AP or broker disappears.
        """
        raise NotImplementedError


class _NativeMqttClientAdapter(MqttClientAdapter):
    def __init__(self, umqtt_client_cls, client_id, host, port, user, password, keepalive, logger_):
        # umqtt's connect() computes len(password) whenever `user` is set,
        # REGARDLESS of whether password itself is None -- confirmed on
        # real hardware: ThingsBoard's access-token auth (token as MQTT
        # username, no password) crashes with
        # "TypeError: object of type 'NoneType' has no len()" the moment
        # connect() is called, unless password is an empty string instead
        # of None. CPython's paho-mqtt has no such restriction (tested
        # working directly against the token with no password in Phase 0),
        # so this is a umqtt-specific quirk, not a ThingsBoard requirement.
        if user is not None and password is None:
            password = ""
        self._client = umqtt_client_cls(
            client_id, host, port=port, user=user, password=password, keepalive=keepalive
        )
        self._logger = logger_
        self._connected = False
        self._callback = None
        self._host = host
        self._port = port
        self._client.set_callback(self._on_message)

    def _on_message(self, topic, payload):
        if self._callback is not None:
            topic_str = topic.decode() if isinstance(topic, (bytes, bytearray)) else topic
            self._callback(topic_str, payload)

    def connect(self):
        self._client.connect()
        self._connected = True
        self._logger.info("MQTT client '%s' connected", self._client.client_id)

    def disconnect(self):
        try:
            self._client.disconnect()
        finally:
            self._connected = False

    def is_connected(self):
        return self._connected

    def subscribe(self, topic, qos=0):
        self._client.subscribe(topic.encode() if isinstance(topic, str) else topic, qos=qos)

    def publish(self, topic, payload, qos=0, retain=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        self._client.publish(
            topic.encode() if isinstance(topic, str) else topic, payload, retain=retain, qos=qos
        )

    def set_callback(self, callback):
        self._callback = callback

    def check_msg(self):
        try:
            self._client.check_msg()
        except OSError as exc:
            self._connected = False
            self._logger.warning("MQTT check_msg() failed, marking disconnected: %s", exc)

    def probe(self, timeout_s=2):
        """Prove the broker is reachable through a fresh TCP/MQTT exchange."""
        if not self._connected:
            raise OSError("MQTT client is not connected")

        # An existing socket may look open after a broker/container restart.
        # First open a fresh TCP connection to the MQTT endpoint, then test
        # the active MQTT session with PINGREQ/PINGRESP.
        if socket is not None:
            test_socket = None
            try:
                address = socket.getaddrinfo(self._host, self._port)[0][-1]
                test_socket = socket.socket()
                test_socket.settimeout(timeout_s)
                test_socket.connect(address)
            finally:
                if test_socket is not None:
                    try:
                        test_socket.close()
                    except Exception:
                        pass

        sock = getattr(self._client, "sock", None)
        if sock is None or not hasattr(self._client, "wait_msg"):
            self.publish("v1/gateway/telemetry", {}, qos=1)
            return
        try:
            sock.settimeout(timeout_s)
            self._client.ping()
            self._client.wait_msg()
        except Exception:
            self._connected = False
            raise
        finally:
            try:
                sock.setblocking(False)
            except Exception:
                pass


class MockMqttClient(MqttClientAdapter):
    """Software-only stand-in used for host-side unit tests (mirrors
    MockTwaiBus's role for the CAN connector) and for development without
    a real broker. Test code drives this via `inject_message()`; `publish()`
    records what was sent in `published` for assertions.
    """

    def __init__(self, client_id, host, port, user, password, keepalive, logger_):
        self.client_id = client_id
        self.host = host
        self.port = port
        self._logger = logger_
        self._connected = False
        self._callback = None
        self.subscriptions = []
        self.published = []
        self._incoming = []

    def connect(self):
        self._connected = True
        self._logger.info(
            "MockMqttClient '%s' connected to %s:%s -- NOT a real broker", self.client_id, self.host, self.port
        )

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})

    def set_callback(self, callback):
        self._callback = callback

    def check_msg(self):
        if not self._incoming:
            return
        topic, payload = self._incoming.pop(0)
        if self._callback is not None:
            self._callback(topic, payload)

    def probe(self, timeout_s=2):
        if not self._connected:
            raise OSError("MQTT client is not connected")

    # --- test-only helpers, not part of the MqttClientAdapter interface --
    def inject_message(self, topic, payload):
        """payload may be a dict/list (auto-JSON-encoded) or bytes/str."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        self._incoming.append((topic, payload))
