"""tbgw.connectors.can.can_connector
=======================================

The CAN connector: a general register/selector-oriented CAN gateway (see
docs/can_connector_design.md). Everything protocol-specific about the
bus this project actually targets (xxx) lives in config/can.json --
this module has no knowledge of registers, node addresses, or the R/W
bit convention. It owns:

  * a `TwaiDriver` instance (native straga-backed, or MockTwaiBus when
    the native module isn't importable -- see twai_driver.py),
  * one dedicated `_thread` (intended for Core 1 / `CORE_CAN`, see the
    core-affinity caveat in tbgw.core.constants) that is the ONLY thread
    touching the bus handle -- both RX and TX go through this one
    thread, so there is no need for a separate lock around driver calls,
  * a `FrameCodec` (frame <-> (deviceKey, frameKind, selector, payload)),
    a `CanUplinkConverter` and `CanDownlinkConverter` (selector <->
    ThingsBoard telemetry/attributes/RPC), and a `ReadScheduler` (polling
    + pending-read correlation, since this bus's read replies carry no
    register number at all),
  * a bounded TX queue that on_rpc_request()/on_attribute_update()
    (called from the gateway core, potentially a different thread/task)
    push onto, which the CAN thread drains between recv() polls -- this
    is what keeps those two entry points non-blocking.

Devices are registered with the gateway at `open()`, from config --
NOT discovered from bus traffic. xxx nodes never announce
themselves, so waiting for a "connect" frame (as a byte-offset/upstream-
style connector would) leaves the device registry empty forever, and
every RPC/GUI command silently fails with "unknown device". See
docs/can_connector_design.md section 4.

Gateway interface expected (duck-typed; implemented by TBGatewayService
-- if `gateway` is None, e.g. during standalone testing, converted
events/device connects are only logged, not forwarded anywhere):

    gateway.send_to_storage(connector_name: str, converted_data: dict)
    gateway.connect_device(device_name: str, device_type: str, connector: Connector)

`converted_data` has the shape produced by CanUplinkConverter.convert():
    {"deviceName": ..., "deviceType": ..., "attributes": {...}, "telemetry": {...}}
"""

try:
    import _thread
except ImportError:  # pragma: no cover - host CPython fallback
    _thread = None
    import threading

try:
    import time
except ImportError:  # pragma: no cover
    time = None

from tbgw.connectors.base import Connector
from tbgw.connectors.can.twai_driver import TwaiDriver
from tbgw.connectors.can.frame_codec import FrameCodec
from tbgw.connectors.can.can_uplink_converter import CanUplinkConverter
from tbgw.connectors.can.can_downlink_converter import CanDownlinkConverter
from tbgw.connectors.can.read_scheduler import ReadScheduler
from tbgw.core.constants import CAN_TX_PIN, CAN_RX_PIN, CORE_CAN
from tbgw.utils.queue import Queue, Full, Empty
from tbgw.utils.event import Event
from tbgw.utils.tb_utility import parse_can_id


# Generous stack size for the CAN thread -- see architecture.md section 3.4
# (MicroPython/ESP32-S3's _thread stack-size gotcha: undersized stacks
# manifest as misleading "maximum recursion depth exceeded" errors, not
# obvious stack-overflow errors). Tune based on real measurements once
# running on hardware; this value is a conservative starting point, not a
# measured minimum.
CAN_THREAD_STACK_SIZE_BYTES = 12 * 1024

_DEFAULT_RX_POLL_INTERVAL_MS = 5
_DEFAULT_TX_QUEUE_SIZE = 32
_DEFAULT_READ_TIMEOUT_MS = 300
_DEFAULT_READ_RETRIES = 1
# The Smart Switch is probed every second (see config/can.json), therefore
# three seconds without a valid reply is enough to report a physically
# disconnected CAN/switch link while still tolerating one missed frame.
_DEFAULT_OFFLINE_AFTER_MS = 3000
_LIVENESS_CHECK_INTERVAL_MS = 250
_MAX_TX_PER_LOOP = 8
# Direct-register writes are idempotent on the target smart switch.  A
# short spaced retry is therefore safer than relying on a single frame when
# a touch command races the switch's own polling/processing cycle.
_DEFAULT_WRITE_SETTLE_MS = 45
_DEFAULT_WRITE_RETRY_DELAY_MS = 90
_DEFAULT_WRITE_RETRIES = 2
_TX_ECHO_WINDOW_MS = 500
# How often to re-check controller state while the bus is unusable, and
# how aggressively to log repeated TX failures -- see _bus_ok().
_BUS_CHECK_INTERVAL_MS = 1000
_TX_FAIL_LOG_FIRST = 3
_TX_FAIL_LOG_EVERY = 500
_HEALTH_HEARTBEAT_INTERVAL_MS = 100


def _now_ms():
    if time is None:
        return 0
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _elapsed_ms(now_ms, then_ms):
    if time is not None and hasattr(time, "ticks_diff"):
        return time.ticks_diff(now_ms, then_ms)
    return now_ms - then_ms


def _sleep_ms(ms):
    if time is None:
        return
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


class CANConnector(Connector):
    def __init__(self, name, connector_type, config, gateway=None, logger=None,
                 runtime_health=None):
        super().__init__(name, connector_type, config, gateway, logger)

        bus_cfg = config.get("bus", {})
        self._bitrate = bus_cfg.get("bitrate", 500000)
        self._extended_id_default = bus_cfg.get("extendedId", False)
        self._strict_eval = bus_cfg.get("strictEval", True)
        self._rx_poll_interval_ms = bus_cfg.get("rxPollIntervalMs", _DEFAULT_RX_POLL_INTERVAL_MS)
        self._tx_queue_size = bus_cfg.get("txQueueSize", _DEFAULT_TX_QUEUE_SIZE)
        self._write_settle_ms = bus_cfg.get("writeSettleMs", _DEFAULT_WRITE_SETTLE_MS)
        self._write_retry_delay_ms = bus_cfg.get("writeRetryDelayMs", _DEFAULT_WRITE_RETRY_DELAY_MS)
        self._write_retries = bus_cfg.get("writeRetries", _DEFAULT_WRITE_RETRIES)
        read_timeout_ms = bus_cfg.get("readTimeoutMs", _DEFAULT_READ_TIMEOUT_MS)
        read_retries = bus_cfg.get("readRetries", _DEFAULT_READ_RETRIES)

        self._driver = TwaiDriver.create(
            CAN_TX_PIN, CAN_RX_PIN, self._bitrate,
            extended_id=self._extended_id_default, logger_=self.logger,
        )

        self._codec = FrameCodec(config["frameProfile"])
        payload_length = config["frameProfile"].get("payload", {}).get("length", 1)

        self._uplink_converter = CanUplinkConverter(
            config.get("devices", []), config.get("datapointSets", {}), logger=self.logger,
        )
        self._downlink_converter = CanDownlinkConverter(
            config.get("writes", []), payload_length=payload_length,
            strict_eval=self._strict_eval, logger=self.logger,
        )

        poll_config = {}
        for device in self._uplink_converter.all_devices():
            poll_entries = device.get("poll", [])
            if poll_entries:
                poll_config[device["address"]] = poll_entries
        self._read_scheduler = ReadScheduler(
            poll_config, read_timeout_ms=read_timeout_ms, read_retries=read_retries,
            logger=self.logger,
        )

        self._offline_after_ms = config.get("liveness", {}).get(
            "offlineAfterMs", _DEFAULT_OFFLINE_AFTER_MS
        )
        self._last_seen_ms = {}
        self._online = {}
        self._next_liveness_check_ms = 0

        # Forward only values that changed since the previous uplink.
        self._last_forwarded_values = {}

        # Bus-health gate + TX failure accounting -- see _bus_ok(). Starts
        # optimistic so the first transmit is attempted immediately;
        # _bus_ok() corrects it within _BUS_CHECK_INTERVAL_MS.
        self._bus_running = True
        self._next_bus_check_ms = 0
        self._tx_fail_count = 0

        self._tx_queue = Queue(maxsize=self._tx_queue_size)
        # CAN-thread-owned delayed idempotent write retries and per-node
        # read holdoffs.  No producer thread touches either structure.
        self._pending_write_retries = []
        self._read_holdoff_until = {}        # A few TWAI/driver combinations return our own normal-mode TX frame
        # through recv().  Never present that echo as switch telemetry.
        self._recent_write_echoes = []
        self._stop_event = Event()
        self._connected = False
        # Shared with main.py's hardware-WDT supervisor.  This reports that
        # the CAN *thread* is running, not that a physical node is online:
        # unplugging the bus must leave local/offline operation alive.
        self._runtime_health = runtime_health

    # -- Connector interface --------------------------------------------------

    def open(self):
        if _thread is not None and hasattr(_thread, "stack_size"):
            try:
                _thread.stack_size(CAN_THREAD_STACK_SIZE_BYTES)
            except ValueError:
                # Some platforms (including host CPython on several OSes)
                # enforce a minimum stack size larger than what's typical/
                # sufficient on MicroPython/ESP32-S3. Falling back to the
                # platform default here is safe -- this call is a tuning
                # hint, not a correctness requirement -- and keeps this
                # connector portable to host-side testing without a
                # host-specific code path.
                pass
        if _thread is not None:
            _thread.start_new_thread(self._run, ())
        else:  # pragma: no cover - host fallback if _thread is ever unavailable
            self._can_thread = threading.Thread(target=self._run, daemon=True)
            self._can_thread.start()
        self.logger.info(
            "CAN connector thread requested on core %s (tx=GPIO%s, rx=GPIO%s, bitrate=%s)",
            CORE_CAN, CAN_TX_PIN, CAN_RX_PIN, self._bitrate,
        )

    def close(self):
        self._stop_event.set()

    def is_connected(self):
        return self._connected

    def is_device_online(self, device_name):
        """True/False if liveness has an opinion, None if never seen at
        all. Not used by the connector itself -- a diagnostic surface
        for callers (GUI status bar, health checks).
        """
        device = self._uplink_converter.device_by_name(device_name)
        if device is None:
            return None
        return self._online.get(device["address"])

    def on_attribute_update(self, device_name, attribute_update):
        """attribute_update: {"<attribute_key>": <new_value>, ...} -- may
        contain several keys in one call; each matching mapping is
        queued for transmission independently.
        """
        device = self._uplink_converter.device_by_name(device_name)
        if device is None:
            self.logger.warning("Attribute update for unknown device %s -- ignoring", device_name)
            return
        for key, value in attribute_update.items():
            built = self._downlink_converter.build_for_attribute_update(device, key, value)
            if built is not None:
                self._enqueue_tx(device, built)

    def on_rpc_request(self, device_name, rpc_request):
        """rpc_request: {"method": str, "params": <any>}"""
        device = self._uplink_converter.device_by_name(device_name)
        if device is None:
            self.logger.warning("RPC for unknown device %s -- ignoring", device_name)
            return
        method = rpc_request.get("method")
        params = rpc_request.get("params")

        built = self._downlink_converter.build_for_rpc(device, method, params)
        if built is None:
            self.logger.warning(
                "No write mapping for device=%s method=%s -- ignoring", device_name, method
            )
            return
        self._enqueue_tx(device, built)

    # -- internals -------------------------------------------------------------

    def _enqueue_tx(self, device, built):
        selector, payload, op = built
        try:
            self._tx_queue.put_nowait((device["address"], selector, payload, op))
        except Full:
            self.logger.warning(
                "CAN TX queue full, dropping outbound frame (device=%s selector=%s)",
                device["deviceName"], selector,
            )

    def _run(self):
        try:
            self._driver.start()
            self._connected = True
            self._register_all_devices()
            last_health_heartbeat_ms = None
            while not self._stop_event.is_set():
                now = _now_ms()
                if (self._runtime_health is not None and
                        (last_health_heartbeat_ms is None or
                         _elapsed_ms(now, last_health_heartbeat_ms) >=
                         _HEALTH_HEARTBEAT_INTERVAL_MS)):
                    self._runtime_health.beat("can", now)
                    last_health_heartbeat_ms = now
                frame = self._driver.recv()
                if frame is not None:
                    self._handle_rx_frame(frame, now)
                else:
                    _sleep_ms(self._rx_poll_interval_ms)
                self._drain_tx_queue(now)
                self._drain_due_write_retries(now)
                self._drain_due_reads(now)
                self._drain_expired_reads(now)
                self._check_liveness(now)
        except Exception as exc:
            self.logger.exception("CAN thread crashed", exc=exc)
        finally:
            self._connected = False
            try:
                self._driver.stop()
            except Exception:
                pass
            self.logger.info("CAN thread stopped")

    def _register_all_devices(self):
        """Declares every configured device to the gateway up front --
        see this module's docstring for why bus-traffic-based discovery
        doesn't work for this protocol.
        """
        for device in self._uplink_converter.all_devices():
            self.logger.info(
                "CAN device declared: %s (profile=%s, address=0x%X)",
                device["deviceName"], device["deviceProfile"], device["address"],
            )
            if self.gateway is not None and hasattr(self.gateway, "connect_device"):
                self.gateway.connect_device(device["deviceName"], device["deviceProfile"], self)

    def _handle_rx_frame(self, frame, now):
        self.messages_received += 1
        self.bytes_received += len(frame.data)

        decoded = self._codec.decode(frame.arbitration_id, frame.data)
        if decoded is None:
            return  # not a frame shape this protocol recognizes -- ignore, not an error

        if self._is_recent_write_echo(decoded, now):
            return

        device = self._uplink_converter.device_by_key(decoded.device_key)
        if device is None:
            return  # traffic from a node this config doesn't know about

        self._last_seen_ms[decoded.device_key] = now

        selector = decoded.selector
        selectorless_read_reply = selector is None
        if selector is None:
            # Ambiguous reply (e.g. xxx's DLC=1 read reply, which
            # echoes no register) -- attribute it to whatever this
            # device's outstanding read was for.
            selector = self._read_scheduler.pending_for(decoded.device_key)
            if selector is None:
                self.logger.debug(
                    "Selector-less frame from device=0x%X with no pending read -- ignoring",
                    decoded.device_key,
                )
                return

        self._read_scheduler.on_value(decoded.device_key, selector, now)
        self._forward_converted(decoded.device_key, selector, decoded.payload)

    def _forward_converted(self, device_key, selector, payload):
        try:
            converted = self._uplink_converter.convert(device_key, selector, payload)
        except Exception as exc:
            self.logger.exception(
                "Uplink conversion crashed (device=0x%X selector=%s)", device_key, selector, exc=exc
            )
            return
        if converted is None:
            return
        converted = self._only_changed_values(converted)
        if converted is None:
            return
        self.messages_sent += 1
        if self.gateway is not None and hasattr(self.gateway, "send_to_storage"):
            self.gateway.send_to_storage(self.name, converted)
        else:
            self.logger.debug("(no gateway attached) converted CAN event: %s", converted)
    def _only_changed_values(self, converted):
        """Return only confirmed telemetry/attributes changed since last uplink.

        The CAN connector still polls rapidly and the GUI state store still
        receives every fresh CAN reply.  This filter applies only at the
        ThingsBoard uplink boundary, where repeated values provide no new
        dashboard state but can delay a genuinely changed value behind MQTT
        QoS-1 publish work.
        """
        device_name = converted.get("deviceName")
        if not device_name:
            return converted

        previous = self._last_forwarded_values.setdefault(device_name, {})
        changed_telemetry = {}
        changed_attributes = {}

        for bucket_name, changed in (
            ("telemetry", changed_telemetry),
            ("attributes", changed_attributes),
        ):
            for key, value in (converted.get(bucket_name) or {}).items():
                if key not in previous or previous[key] != value:
                    previous[key] = value
                    changed[key] = value

        if not changed_telemetry and not changed_attributes:
            return None

        filtered = dict(converted)
        filtered["telemetry"] = changed_telemetry
        filtered["attributes"] = changed_attributes
        return filtered

    def _drain_tx_queue(self, now):
        """Drain queued generic CAN reads and writes without starving RX."""
        for _ in range(_MAX_TX_PER_LOOP):
            try:
                device_key, selector, payload, op = self._tx_queue.get_nowait()
            except Empty:
                return
            if op == "read":
                self._send_frame(device_key, selector, payload, op)
                continue
            self._pending_write_retries = [
                item for item in self._pending_write_retries
                if not (item[1] == device_key and item[2] == selector)
            ]
            self._send_write_with_retry(
                device_key, selector, payload, op, now, self._write_retries
            )

    def _send_write_with_retry(self, device_key, selector, payload, op, now, retries_left):
        """Send a direct-register write, defer reads, and schedule retry."""
        # A queued selector-less read can otherwise race the write/reply on
        # this protocol.  The follow-up refresh below will obtain a clean
        # confirmation after all repeat writes have had time to settle.
        self._read_scheduler.cancel_pending(device_key)
        sent = self._send_frame(device_key, selector, payload, op)
        holdoff = now + self._write_settle_ms + (retries_left * self._write_retry_delay_ms)
        previous = self._read_holdoff_until.get(device_key, 0)
        self._read_holdoff_until[device_key] = max(previous, holdoff)
        self._read_scheduler.request_refresh(device_key, selector, now)
        if retries_left > 0:
            self._pending_write_retries.append((
                now + self._write_retry_delay_ms,
                device_key, selector, payload, op, retries_left - 1,
            ))
        return sent

    def _drain_due_write_retries(self, now):
        pending = self._pending_write_retries
        self._pending_write_retries = []
        for due, device_key, selector, payload, op, retries_left in pending:
            if now >= due:
                self._send_write_with_retry(device_key, selector, payload, op, now, retries_left)
            else:
                self._pending_write_retries.append((due, device_key, selector, payload, op, retries_left))

    def _drain_due_reads(self, now):
        for device_key, selector in self._read_scheduler.due(now):
            if now < self._read_holdoff_until.get(device_key, 0):
                continue
            if self._send_frame(device_key, selector, b"", "read"):
                self._read_scheduler.on_read_sent(device_key, selector, now)

    def _drain_expired_reads(self, now):
        for device_key, selector, retries_left in self._read_scheduler.expired(now):
            if retries_left > 0:
                if self._send_frame(device_key, selector, b"", "read"):
                    self._read_scheduler.on_read_sent(device_key, selector, now, retries_left - 1)
            # else: given up for now -- the next scheduled poll (or a
            # future event) will pick this datapoint up again on its own.

    def _is_recent_write_echo(self, decoded, now):
        """Discard a locally transmitted normal-mode write echoed by TWAI."""
        remaining = []
        echoed = False
        for expires_at, device_key, selector, payload in self._recent_write_echoes:
            if now > expires_at:
                continue
            if (
                decoded.frame_kind == "response"
                and decoded.device_key == device_key
                and decoded.selector == selector
                and bytes(decoded.payload) == payload
            ):
                echoed = True
                continue
            remaining.append((expires_at, device_key, selector, payload))
        self._recent_write_echoes = remaining
        if echoed:
            self.logger.debug(
                "Ignoring echoed local CAN write (device=0x%X selector=%s)",
                decoded.device_key, decoded.selector,
            )
        return echoed

    def _bus_ok(self, now):
        """False while the controller is not in RUNNING state (BUS_OFF or
        stopped), throttling every transmit attempt until it recovers.

        Without this, a bus fault turns into a self-inflicted denial of
        service. Observed on real hardware with the bus terminator
        switched off: the controller went BUS_OFF ~30s after boot, after
        which every `send()` raised "Device is not ready" immediately,
        the poll scheduler retried each datapoint on every loop
        iteration, and each failure logged two lines. That produced
        **33MB** of console output in a few minutes. Writing that to the
        USB-Serial-JTAG console is itself blocking, GIL-held I/O, so the
        board stopped responding entirely -- indistinguishable from a
        crash, and it masked the real state (a perfectly recoverable bus
        fault) behind a wall of noise.

        The controller is constructed with auto_restart=True, so it
        recovers on its own; this just stops us hammering it meanwhile,
        and reports the transition once at each edge rather than
        thousands of times.
        """
        if now < self._next_bus_check_ms:
            return self._bus_running

        self._next_bus_check_ms = now + _BUS_CHECK_INTERVAL_MS
        try:
            state = self._driver.get_status().get("state")
        except Exception:
            state = "unknown"
        running = state == "running"

        if not running:
            # The controller does not leave BUS_OFF on its own (the
            # native module refuses auto_restart), so recovery has to be
            # driven from here -- otherwise a brief bus fault disables
            # transmits permanently until the board is rebooted.
            recovered = False
            try:
                recovered = self._driver.recover()
            except Exception as exc:
                self.logger.warning("CAN recovery attempt failed: %s", exc)
            if recovered:
                running = True

        if running != self._bus_running:
            if running:
                self.logger.info("CAN bus recovered -- resuming transmits")
            else:
                self.logger.warning(
                    "CAN bus not usable (state=%s) -- suppressing transmits until it recovers. "
                    "Check bus wiring/termination and that another node is ACKing.", state
                )
            self._bus_running = running
        return running

    def _send_frame(self, device_key, selector, payload, op):
        try:
            arbitration_id, data = self._codec.encode(device_key, selector, payload, op)
        except Exception as exc:
            self.logger.exception(
                "Frame encode failed (device=0x%X selector=%s op=%s)", device_key, selector, op, exc=exc
            )
            return False

        if not self._bus_ok(_now_ms()):
            return False

        if op != "read":
            self.logger.debug("CAN write id=0x%X selector=%s data=%s", arbitration_id, selector, bytes(data))
        sent = self._driver.send(arbitration_id, data, extended=self._extended_id_default)
        if sent:
            self.messages_sent += 1
            self.bytes_sent += len(data)
            if op == "write":
                self._recent_write_echoes.append(
                    (_now_ms() + _TX_ECHO_WINDOW_MS, device_key, selector, bytes(payload))
                )
        else:
            self._tx_fail_count += 1
            # Rate-limited: a persistent fault must never be allowed to
            # flood the console (see _bus_ok()). Report the first few,
            # then only periodically, with a running total.
            if self._tx_fail_count <= _TX_FAIL_LOG_FIRST or (
                self._tx_fail_count % _TX_FAIL_LOG_EVERY == 0
            ):
                self.logger.warning(
                    "CAN TX failed (id=0x%X op=%s) [%d failures so far]",
                    arbitration_id, op, self._tx_fail_count,
                )
        return sent

    def _check_liveness(self, now):
        if now < self._next_liveness_check_ms:
            return
        self._next_liveness_check_ms = now + _LIVENESS_CHECK_INTERVAL_MS

        for device in self._uplink_converter.all_devices():
            device_key = device["address"]
            last_seen = self._last_seen_ms.get(device_key)
            online = last_seen is not None and (now - last_seen) < self._offline_after_ms
            previous = self._online.get(device_key)
            if previous == online:
                continue
            self._online[device_key] = online
            self.logger.info(
                "CAN device %s -> %s", device["deviceName"], "online" if online else "offline"
            )
            if self.gateway is not None and hasattr(self.gateway, "send_to_storage"):
                self.gateway.send_to_storage(self.name, {
                    "deviceName": device["deviceName"],
                    "deviceType": device["deviceProfile"],
                    "attributes": {"active": online},
                    "telemetry": {},
                })
