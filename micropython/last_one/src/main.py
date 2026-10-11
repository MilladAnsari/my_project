"""main.py -- integration entry point.

Responsibilities (per docs/architecture.md's 3-worker / 2-core design):

  1. Load configuration and set up logging.
  2. Bring up Wi-Fi and sync NTP (Phase 4) -- best-effort, bounded
     retries, and does NOT block CAN/GUI startup if it fails: the local
     GUI and CAN bus are supposed to keep working with no internet at
     all, only the ThingsBoard uplink genuinely needs connectivity, and
     its own worker thread (tbgw.core.uplink_worker) retries connecting
     independently and indefinitely once Wi-Fi is up.
  3. Start the GUI thread on Core 0.
  4. Start the CAN RX/TX thread on Core 1.
  5. Run the asyncio gateway core (which owns the uplink worker thread)
     on Core 1's main thread, alongside a lightweight Wi-Fi reconnect
     watchdog.

Phase 1 note: steps 3-5 originally only reached as far as their
respective not-yet-implemented modules; main.py was written to degrade
gracefully -- it logs which phase is missing and keeps the process alive
via a small asyncio heartbeat loop, rather than crashing outright, so
this skeleton could already be flashed and observed on real hardware
before every phase landed. That degrade-gracefully behavior is preserved
here even though every phase is now implemented, since it costs nothing
and keeps the board alive to observe if a future change reintroduces a
gap.
"""

import gc
import sys

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from tbgw.core import constants, ntp
from tbgw.core.config_loader import load_general_config, load_logging_config, load_wifi_config
from tbgw.core.runtime_watchdog import RuntimeHealth, WatchdogGate
from tbgw.core.wifi import WifiManager, connect_with_retry
from tbgw.storage.memory_storage import MemoryEventStorage
from tbgw.utils.event import Event
from tbgw.utils.logger import get_logger, configure_from_dict

logger = get_logger("main")

HEARTBEAT_INTERVAL_S = 30
WIFI_CONNECT_TIMEOUT_S = 15
WIFI_CONNECT_MAX_ATTEMPTS = 5  # bounded at boot -- see module docstring
# The automatic safety net runs every five minutes. The Settings button
# signals the same coroutine and is handled immediately.
CONNECTION_HEALTH_CHECK_INTERVAL_S = 5 * 60
CONNECTION_RECOVERY_ATTEMPTS = 2
CONNECTION_RECOVERY_TIMEOUT_S = 8
MANUAL_RECOVERY_POLL_INTERVAL_S = 1

# The ESP32 WDT is armed once per boot and cannot be disabled at runtime.
# Thirty seconds was selected for this display: it tolerates a short LVGL
# render burst while still recovering a genuine freeze without human help.
WDT_TIMEOUT_MS = 30 * 1000
RUNTIME_HEARTBEAT_STALE_AFTER_MS = 5 * 1000
RUNTIME_WDT_POLL_INTERVAL_S = 1
# Touch calibration can legitimately take a while on a first boot.  Feed
# during this bounded window; afterwards a configured GUI must prove it is
# running before the WDT is fed.
RUNTIME_WDT_STARTUP_GRACE_MS = 90 * 1000


def _mem_free():
    """gc.mem_free() is MicroPython-only (absent on CPython). This wrapper
    lets main.py's heartbeat run unmodified during host-side smoke testing.
    """
    try:
        return gc.mem_free()
    except AttributeError:
        return -1


def _init_logging():
    try:
        levels = load_logging_config("config/logs.json")
        configure_from_dict(levels)
    except Exception as exc:
        print("main: failed to load logging config, using defaults:", exc)


def _log_reset_cause():
    """Log the reset source when the platform exposes MicroPython's API."""
    try:
        import machine
        cause = machine.reset_cause()
    except (ImportError, AttributeError):
        return
    names = (
        "PWRON_RESET", "HARD_RESET", "WDT_RESET", "DEEPSLEEP_RESET", "SOFT_RESET",
    )
    label = next((name for name in names if getattr(machine, name, None) == cause), "unknown")
    logger.info("Boot reset cause: %s (%s)", label, cause)


def _create_hardware_wdt():
    """Return the ESP32 hardware WDT, or None on host/unsupported builds."""
    try:
        import machine
        return machine.WDT(timeout=WDT_TIMEOUT_MS)
    except ImportError:
        logger.info("Hardware WDT unavailable on this platform; runtime health is monitor-only")
    except Exception as exc:
        logger.exception("Could not arm hardware WDT", exc=exc)
    return None


def _init_wifi():
    """Best-effort, bounded Wi-Fi bring-up. Returns the WifiManager (even
    if it never actually connected within the attempt budget -- the
    watchdog task started later keeps retrying), or None if no SSID is
    configured at all (a deliberately supported "offline" mode: CAN and
    the local GUI don't need the internet).
    """
    try:
        wifi_config = load_wifi_config()
    except OSError as exc:
        logger.warning("No Wi-Fi config found (%s) -- running offline", exc)
        return None
    ssid = wifi_config.get("ssid")
    password = wifi_config.get("password")
    if not ssid:
        logger.info("No Wi-Fi ssid configured -- running offline")
        return None

    wlan = WifiManager.create(logger_=get_logger("wifi"))
    connected = connect_with_retry(
        wlan, ssid, password,
        timeout_s=WIFI_CONNECT_TIMEOUT_S, max_attempts=WIFI_CONNECT_MAX_ATTEMPTS,
        logger=get_logger("wifi"),
    )
    if not connected:
        logger.warning(
            "Wi-Fi did not connect within %d attempts -- continuing to boot anyway; "
            "the watchdog task will keep retrying, and the uplink worker will connect "
            "once Wi-Fi eventually comes up",
            WIFI_CONNECT_MAX_ATTEMPTS,
        )
    return wlan


def _init_ntp():
    ntp.sync(logger=get_logger("ntp"))


async def _connection_watchdog(wlan, gateway, recovery_requested):
    """Check Wi-Fi then ThingsBoard every five minutes.

    The Settings screen signals ``recovery_requested`` for an immediate
    check. It never performs blocking network calls from the GUI thread.
    """
    seconds_until_check = CONNECTION_HEALTH_CHECK_INTERVAL_S
    while True:
        await asyncio.sleep(MANUAL_RECOVERY_POLL_INTERVAL_S)
        manual_request = recovery_requested.is_set()
        if manual_request:
            recovery_requested.clear()
        seconds_until_check -= MANUAL_RECOVERY_POLL_INTERVAL_S
        if not manual_request and seconds_until_check > 0:
            continue
        seconds_until_check = CONNECTION_HEALTH_CHECK_INTERVAL_S

        try:
            wifi_connected = wlan.is_connected()
        except Exception:
            wifi_connected = False

        if not wifi_connected:
            logger.warning("Connection watchdog: Wi-Fi is down; reconnecting...")
            try:
                wifi_config = load_wifi_config()
                wifi_connected = connect_with_retry(
                    wlan, wifi_config.get("ssid"), wifi_config.get("password"),
                    timeout_s=CONNECTION_RECOVERY_TIMEOUT_S,
                    max_attempts=CONNECTION_RECOVERY_ATTEMPTS,
                    logger=get_logger("wifi"),
                )
                if wifi_connected and not ntp.is_synced():
                    _init_ntp()
            except OSError:
                logger.exception("Connection watchdog: could not reload Wi-Fi config")
                wifi_connected = False

        # Wi-Fi must be healthy before asking for MQTT. A manual request
        # deliberately replaces even a seemingly connected session, which
        # recovers sockets left stale by an AP/broker restart.
        if wifi_connected:
            try:
                thingsboard_connected = gateway.uplink_worker.is_healthy()
            except Exception:
                thingsboard_connected = False
            if manual_request or not thingsboard_connected:
                logger.info(
                    "%s ThingsBoard reconnect",
                    "Manual request: starting" if manual_request else "Automatic health check: requesting",
                )
                gateway.uplink_worker.request_reconnect()


def _init_storage(general_config):
    storage_cfg = general_config.get("storage", {})
    storage = MemoryEventStorage(
        maxsize=storage_cfg.get("maxRecordsCount", constants.DEFAULT_STORAGE_MAXSIZE),
        overflow_policy=storage_cfg.get("overflowPolicy", "drop_oldest"),
        logger=get_logger("storage"),
    )
    logger.info(
        "Storage initialized: memory, maxsize=%s, overflowPolicy=%s",
        storage.maxsize, storage.overflow_policy,
    )
    return storage


def _start_gui(general_config, gateway, runtime_health=None):
    """Starts the LVGL GUI thread on Core 0 (Phase 4). Returns True if the
    GUI actually started, False if the Phase 4 module isn't implemented yet.
    The GUI needs `gateway` (for send_local_command()/device_states/
    device_registry/can_connector -- all documented as safe for this
    cross-thread use, see gateway_service.py) so this must be called
    AFTER _start_gateway_service() and, ideally, after _start_can() so
    gateway.can_connector is already set for the status bar.
    """
    try:
        from tbgw.gui.gui_app import start_gui_thread
    except (ImportError, NotImplementedError) as exc:
        logger.warning("GUI not started (Phase 4 not yet implemented): %s", exc)
        return False
    if not general_config.get("gui", {}).get("enabled", True):
        logger.info("GUI disabled in config (gui.enabled=false), skipping")
        return False
    if gateway is None:
        logger.warning("GUI not started: no gateway available (Phase 3 not yet implemented or failed to start)")
        return False
    try:
        start_gui_thread(gateway, core=constants.CORE_GUI, runtime_health=runtime_health)
        logger.info("GUI thread started on core %s", constants.CORE_GUI)
        return True
    except Exception:
        logger.exception("GUI thread failed to start")
        return False


def _attach_gui_connection_context(gateway, general_config, wlan, recovery_requested):
    """Expose only display-safe connection metadata to the local GUI.

    The GUI needs a live WLAN object for its navigation-time status
    snapshots plus SSID/ThingsBoard endpoint for Settings. Passwords and
    access tokens are deliberately never copied into this context.
    """
    if gateway is None:
        return
    wifi_ssid = None
    try:
        wifi_ssid = load_wifi_config().get("ssid")
    except OSError:
        pass

    thingsboard = general_config.get("thingsboard", {})
    gateway.gui_wifi_manager = wlan
    gateway.gui_wifi_ssid = wifi_ssid
    gateway.gui_thingsboard_config = {
        "host": thingsboard.get("host"),
        "port": thingsboard.get("port"),
    }
    gateway.set_network_recovery_request(recovery_requested)


def _start_can(general_config, gateway, storage, runtime_health=None):
    """Starts the CAN connector's RX/TX thread on Core 1 (Phase 2). Returns
    the connector instance, or None if the Phase 2 module isn't implemented
    yet or the connector is disabled in config.
    """
    can_enabled = any(
        c.get("type") == "can" for c in general_config.get("connectors", [])
    )
    if not can_enabled:
        logger.info("No CAN connector configured, skipping")
        return None
    try:
        from tbgw.connectors.can.can_connector import CANConnector
    except (ImportError, NotImplementedError) as exc:
        logger.warning("CAN connector not started (Phase 2 not yet implemented): %s", exc)
        return None
    try:
        from tbgw.core.config_loader import load_connector_config
        can_config = load_connector_config("config/can.json")
        connector = CANConnector(
            name="CAN Connector",
            connector_type="can",
            config=can_config,
            gateway=gateway,
            logger=get_logger("can_connector"),
            runtime_health=runtime_health,
        )
        connector.open()
        logger.info("CAN connector started (TX=GPIO%s, RX=GPIO%s)",
                    constants.CAN_TX_PIN, constants.CAN_RX_PIN)
        if gateway is not None:
            gateway.can_connector = connector
        return connector
    except Exception:
        logger.exception("CAN connector failed to start")
        return None


def _start_gateway_service(general_config, storage, runtime_health=None):
    """Builds TBGatewayService (Phase 3). Returns the instance, or None if
    the Phase 3 module isn't implemented yet.
    """
    try:
        from tbgw.core.gateway_service import TBGatewayService
    except (ImportError, NotImplementedError) as exc:
        logger.warning("Gateway service not started (Phase 3 not yet implemented): %s", exc)
        return None
    try:
        return TBGatewayService(
            general_config, storage=storage, logger=get_logger("gateway_service"),
            runtime_health=runtime_health,
        )
    except Exception:
        logger.exception("Gateway service failed to initialize")
        return None


async def _heartbeat(storage, can_connector, gateway):
    """Keeps the asyncio loop alive and gives a single, periodic, at-a-glance
    log line of overall gateway health -- useful during Phase 1/2/3
    bring-up before the GUI (Phase 4) provides the same information
    visually.
    """
    while True:
        logger.info(
            "heartbeat: free_heap=%s storage_len=%s can=%s gateway=%s",
            _mem_free(),
            storage.len(),
            "up" if can_connector is not None else "down",
            "up" if gateway is not None else "down",
        )
        gc.collect()
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _runtime_watchdog(health, required_workers):
    """Feed the ESP32 WDT only after all required workers are fresh.

    The supervisor itself runs on the gateway asyncio loop.  A full asyncio
    stall therefore prevents both this coroutine and the gateway heartbeat
    from running, allowing the hardware WDT to reset the board.  Wi-Fi,
    broker and physical CAN liveness are deliberately not checked here.
    """
    wdt = _create_hardware_wdt()
    if wdt is None:
        return

    gate = WatchdogGate(
        health, required_workers,
        stale_after_ms=RUNTIME_HEARTBEAT_STALE_AFTER_MS,
        startup_grace_ms=RUNTIME_WDT_STARTUP_GRACE_MS,
    )
    gate.arm()
    last_stale = None
    logger.info(
        "Runtime WDT armed: timeout=%dms workers=%s grace=%dms",
        WDT_TIMEOUT_MS, list(required_workers), RUNTIME_WDT_STARTUP_GRACE_MS,
    )
    while True:
        should_feed, stale = gate.should_feed()
        if should_feed:
            try:
                wdt.feed()
            except Exception as exc:
                logger.exception("Runtime WDT feed failed", exc=exc)
                return
            if last_stale is not None:
                logger.info("Runtime WDT workers recovered")
                last_stale = None
        elif stale != last_stale:
            logger.error(
                "Runtime WDT feed withheld; stale worker(s): %s", ", ".join(stale)
            )
            last_stale = stale
        await asyncio.sleep(RUNTIME_WDT_POLL_INTERVAL_S)


async def _run_gateway_or_idle(general_config, storage, wlan):
    runtime_health = RuntimeHealth()
    runtime_health.register("gateway")
    gateway = _start_gateway_service(general_config, storage, runtime_health=runtime_health)

    runtime_health.register("can")
    can_connector = _start_can(
        general_config, gateway, storage, runtime_health=runtime_health
    )
    recovery_requested = Event()
    _attach_gui_connection_context(gateway, general_config, wlan, recovery_requested)
    runtime_health.register("gui")
    gui_started = _start_gui(general_config, gateway, runtime_health=runtime_health)

    # Stashed at module level purely for live field debugging: a serial
    # REPL session (mpremote exec, or a raw-REPL script) can then do
    # `import main; main.gateway` to introspect live state -- e.g. call
    # `.device_states.snapshot()`, `.get_connector_status()`,
    # `.uplink_client.is_connected()` -- without needing a special debug
    # build. Reading these from the REPL is safe even while the gateway/
    # GUI threads are running (none of them touch LVGL), which is what
    # made isolating a Phase 5 GUI-thread-only freeze possible: heartbeat
    # logging kept going while the screen was dead, and being able to
    # call these from the REPL without redeploying confirmed the freeze
    # wasn't in any of this plain data path.
    globals()["gateway"] = gateway

    tasks = []
    if wlan is not None and gateway is not None:
        tasks.append(_connection_watchdog(wlan, gateway, recovery_requested))

    if gateway is not None:
        # TBGatewayService.run() takes over most of the event loop from
        # here (it owns the uplink worker thread and schedules its own
        # heartbeat/flush/inbound tasks) -- run it alongside the Wi-Fi
        # watchdog rather than awaiting it alone, so a dropped Wi-Fi
        # connection still gets noticed and retried.
        tasks.append(gateway.run())
        required_workers = ["gateway"]
        if can_connector is not None:
            required_workers.append("can")
        if gui_started:
            required_workers.append("gui")
        tasks.append(_runtime_watchdog(runtime_health, required_workers))
    else:
        # Degrade gracefully: keep the board alive and observable via the
        # heartbeat log even though the full gateway core isn't wired up
        # (e.g. it failed to initialize).
        tasks.append(_heartbeat(storage, can_connector, gateway))

    await asyncio.gather(*tasks)


def main():
    _init_logging()
    _log_reset_cause()
    logger.info("tbgw starting, version=%s", __import__("tbgw").__version__)

    try:
        general_config = load_general_config("config/tb_gateway.json")
    except OSError as exc:
        logger.error("Could not load config/tb_gateway.json: %s", exc)
        sys.exit(1)

    wlan = _init_wifi()
    if wlan is not None and wlan.is_connected():
        _init_ntp()

    storage = _init_storage(general_config)

    try:
        asyncio.run(_run_gateway_or_idle(general_config, storage, wlan))
    except KeyboardInterrupt:
        logger.info("tbgw stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
