"""tbgw.core.uplink_worker
============================

Runs the ThingsBoard uplink connection (UplinkClient, umqtt-based) on
its own dedicated `_thread`, and exposes an enqueue-only proxy for the
`gw_*` methods that `gateway_service.py`/`rpc_dispatcher.py` call from
the asyncio gateway core.

Why this exists: umqtt's `connect()` and a `qos=1` `publish()` are both
genuinely blocking socket calls (confirmed against `umqtt.simple`'s
source -- `publish(qos=1)` explicitly sets the socket back to blocking
mode while it waits for a PUBACK). Calling either directly from an
asyncio task stalls MicroPython's single-threaded cooperative event
loop for as long as the network happens to take -- which, on a lost
Wi-Fi connection or an unreachable broker, can be many seconds. That
would freeze every other asyncio task sharing the loop: the device-event
loop, the local-command loop (i.e. the GUI-issued command path), and the
heartbeat. `check_msg()` itself is not the problem (umqtt's own
non-blocking-socket implementation), but there is no way to `publish()`
at `qos=1` without blocking, so the whole uplink client's socket
lifecycle -- connect, check_msg, publish -- lives on this dedicated
thread instead, exactly mirroring how `can_connector.py` already
isolates the TWAI driver on its own thread for the same class of reason.

Design: every `gw_*` call this class exposes just enqueues a job onto a
thread-safe outbox `Queue`; the worker thread (`_run`) drains it and
calls the REAL `UplinkClient` method, where blocking is fine because
nothing else shares this thread. Inbound RPC/attribute-update
notifications (which fire from inside `UplinkClient.check_msg()`, i.e.
FROM this worker thread) are pushed onto a second `Queue` rather than
dispatched directly, so `RpcDispatcher.handle_rpc_request()`/
`handle_attribute_update()` -- which call back into connectors expected
to only ever be touched from the gateway's own asyncio thread (see
`mqtt_connector.py`'s docstring) -- keep running there, unchanged. See
`gateway_service.py`'s `_uplink_inbound_loop` for the asyncio-side drain.

`is_connected()` and other read-only queries pass straight through to
the real client -- those are plain attribute reads, not socket
operations, and are already safe to call from any thread (the same
convention `can_connector.py`'s `is_connected()` relies on).
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

from tbgw.utils.queue import Queue, Full, Empty
from tbgw.utils.event import Event

_DEFAULT_OUTBOX_MAXSIZE = 200
_DEFAULT_INBOX_MAXSIZE = 200
_DEFAULT_RECONNECT_BACKOFF_S = 30.0
_DEFAULT_POLL_INTERVAL_S = 0.05
_DEFAULT_HEALTH_PROBE_INTERVAL_S = 5.0
_MAX_JOBS_PER_ITERATION = 20


def _sleep_s(seconds):
    if time is None:
        return
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(int(seconds * 1000))
    else:
        time.sleep(seconds)


def _now_ms():
    if time is None:
        return 0
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _is_due(now_ms, due_ms):
    if time is not None and hasattr(time, "ticks_diff"):
        return time.ticks_diff(now_ms, due_ms) >= 0
    return now_ms >= due_ms


class UplinkWorker:
    def __init__(self, uplink_client, outbox_maxsize=_DEFAULT_OUTBOX_MAXSIZE,
                 inbox_maxsize=_DEFAULT_INBOX_MAXSIZE,
                 reconnect_backoff_s=_DEFAULT_RECONNECT_BACKOFF_S,
                 poll_interval_s=_DEFAULT_POLL_INTERVAL_S,
                 health_probe_interval_s=_DEFAULT_HEALTH_PROBE_INTERVAL_S,
                 logger=None):
        self._client = uplink_client
        self._outbox = Queue(maxsize=outbox_maxsize)
        self._inbox = Queue(maxsize=inbox_maxsize)
        self._reconnect_backoff_s = reconnect_backoff_s
        self._poll_interval_s = poll_interval_s
        self._health_probe_interval_ms = int(health_probe_interval_s * 1000)
        self._next_health_probe_ms = 0
        self._logger = logger
        self._stop_event = Event()
        self._healthy_event = Event()
        # Set from the GUI/asyncio side when an operator explicitly asks
        # for a new ThingsBoard session.  Only this worker ever touches the
        # MQTT socket, so a flag is safer than calling disconnect/connect
        # directly from another thread.
        self._reconnect_event = Event()

        self._client.set_rpc_handler(self._on_rpc_request)
        self._client.set_attribute_update_handler(self._on_attribute_update)

    # -- read-only passthroughs (safe from any thread) -----------------------

    def is_connected(self):
        return self._client.is_connected()

    def is_healthy(self):
        """True only after an MQTT connection/probe, not just a local socket."""
        return self._healthy_event.is_set() and self._client.is_connected()

    def request_reconnect(self):
        """Ask the socket-owning worker to create a fresh MQTT session.

        This method is deliberately non-blocking and thread-safe: the GUI
        can call it from Core 0, while the actual disconnect/connect pair is
        performed by ``_run()`` on the uplink worker thread.
        """
        self._reconnect_event.set()

    # -- enqueue-only API used by gateway_service.py / rpc_dispatcher.py -----
    # Each of these mirrors a real UplinkClient method name/signature so
    # both call sites are drop-in replacements for what used to call the
    # client directly.

    def gw_connect_device(self, device_name, device_type="default"):
        self._put(("connect_device", device_name, device_type))

    def gw_disconnect_device(self, device_name):
        self._put(("disconnect_device", device_name))

    def gw_send_rpc_reply(self, device_name, request_id, result):
        self._put(("rpc_reply", device_name, request_id, result))

    def enqueue_event_pack(self, pack):
        """Telemetry + attributes for one flushed storage pack, sent as
        ONE job -- so a publish failure partway through can't resend
        telemetry while retrying attributes (or vice versa), which is
        exactly how the earlier synchronous `_uplink_flush_loop` could
        end up double-sending telemetry on a partial failure.
        """
        self._put(("event_pack", pack))

    def _put(self, job):
        try:
            self._outbox.put_nowait(job)
        except Full:
            if self._logger is not None:
                self._logger.warning("Uplink outbox full, dropping job: %s", job[0])

    # -- inbound callbacks, invoked BY UplinkClient FROM THIS WORKER THREAD --

    def _on_rpc_request(self, device_name, request_id, method, params):
        try:
            self._inbox.put_nowait(("rpc", device_name, request_id, method, params))
        except Full:
            if self._logger is not None:
                self._logger.warning("Uplink inbox full, dropping RPC for %s", device_name)

    def _on_attribute_update(self, device_name, attributes):
        try:
            self._inbox.put_nowait(("attributes", device_name, attributes))
        except Full:
            if self._logger is not None:
                self._logger.warning("Uplink inbox full, dropping attribute update for %s", device_name)

    def get_inbound_batch(self, max_items):
        """Called from the gateway's asyncio thread to drain RPC/
        attribute-update events that arrived on this worker thread --
        keeps dispatch running only on the gateway asyncio thread, per
        this module's docstring.
        """
        return self._inbox.get_batch(max_items)

    # -- the worker thread itself ---------------------------------------------

    def start(self):
        if _thread is not None:
            _thread.start_new_thread(self._run, ())
        else:  # pragma: no cover - host fallback if _thread is ever unavailable
            threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        try:
            while not self._stop_event.is_set():
                if self._reconnect_event.is_set():
                    self._reconnect_event.clear()
                    self._healthy_event.clear()
                    try:
                        self._client.disconnect()
                    except Exception:
                        if self._logger is not None:
                            self._logger.exception("Uplink reconnect: disconnect failed")
                if not self._client.is_connected():
                    try:
                        self._client.connect()
                        # Do not expose TB:OK until the immediate probe below
                        # confirms both a new TCP connection and MQTT reply.
                        self._healthy_event.clear()
                        self._next_health_probe_ms = _now_ms()
                        if self._logger is not None:
                            self._logger.info("Uplink connected to ThingsBoard")
                    except Exception:
                        if self._logger is not None:
                            self._logger.exception(
                                "Uplink connect failed, retrying in %ss", self._reconnect_backoff_s
                            )
                        _sleep_s(self._reconnect_backoff_s)
                        continue
                else:
                    try:
                        self._client.check_msg()
                    except Exception:
                        self._healthy_event.clear()
                        if self._logger is not None:
                            self._logger.exception("Uplink check_msg failed")
                        # A lost AP/broker can leave umqtt's local object
                        # marked connected even though its socket is dead.
                        # Reset it here so the next worker iteration invokes
                        # connect() instead of repeatedly calling check_msg()
                        # on the stale socket forever.
                        try:
                            self._client.disconnect()
                        except Exception:
                            if self._logger is not None:
                                self._logger.exception("Uplink reset after check_msg failure failed")
                    if not self._client.is_connected():
                        self._healthy_event.clear()

                now = _now_ms()
                if self._client.is_connected() and _is_due(now, self._next_health_probe_ms):
                    try:
                        self._client.probe()
                        self._healthy_event.set()
                    except Exception:
                        self._healthy_event.clear()
                        if self._logger is not None:
                            self._logger.exception("ThingsBoard health probe failed")
                        try:
                            self._client.disconnect()
                        except Exception:
                            pass
                    self._next_health_probe_ms = now + self._health_probe_interval_ms
                self._drain_outbox()
                _sleep_s(self._poll_interval_s)
        finally:
            # Runs on this same thread, which is the only thread that is
            # ever allowed to touch self._client's socket -- see this
            # module's docstring. gateway_service.py's stop() must not
            # call self.uplink_client.disconnect() itself.
            try:
                self._client.disconnect()
            except Exception:
                pass

    def _drain_outbox(self):
        for _ in range(_MAX_JOBS_PER_ITERATION):
            try:
                job = self._outbox.get_nowait()
            except Empty:
                return
            self._execute(job)

    def _execute(self, job):
        kind = job[0]
        try:
            if kind == "connect_device":
                _, name, device_type = job
                self._client.gw_connect_device(name, device_type)
            elif kind == "disconnect_device":
                _, name = job
                self._client.gw_disconnect_device(name)
            elif kind == "rpc_reply":
                _, name, request_id, result = job
                self._client.gw_send_rpc_reply(name, request_id, result)
            elif kind == "event_pack":
                _, pack = job
                self._client.gw_send_telemetry_batch(pack)
                self._client.gw_send_attributes_batch(pack)
        except Exception:
            self._healthy_event.clear()
            if self._logger is not None:
                self._logger.exception("Uplink job failed: %s", kind)
            try:
                self._client.disconnect()
            except Exception:
                pass
