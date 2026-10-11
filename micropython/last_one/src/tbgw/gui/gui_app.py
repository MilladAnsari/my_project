"""tbgw.gui.gui_app
=====================

Entry point for the GUI (Phase 4). `start_gui_thread()` is called once
from `main.py` after `TBGatewayService` exists (so the GUI can read
`gateway.device_states`/`gateway.device_registry`/`gateway.can_connector`
and call `gateway.send_local_command()` -- all of which are documented
as thread-safe for exactly this cross-thread use, see
docs/architecture.md and gateway_service.py's module docstring).

Owns the Core 0 thread (see tbgw.core.constants.CORE_GUI and its
core-affinity caveat), but only long enough to bring up the display and
hand LVGL off to `lvgl_micropython`'s `task_handler.TaskHandler` -- see
`_run()`'s comment for why this thread does NOT run its own pump loop.
TaskHandler drives:
  1. LVGL's timer handler (rendering, animations, touch input), and
  2. an `lv.timer_create()` callback, every ~200ms, that reads a snapshot
     from `gateway.device_states` and pushes it into whichever screen is
     currently visible.

Screen navigation starts directly on the thermostat, with direct switches
between the dedicated thermostat and lights pages. A settings/diagnostics
screen is reachable from either device page. Every screen is built once, up
front (see `_Navigator`'s docstring for why).

Verified against real hardware during Phase 5 bring-up -- see
`tbgw.gui.lvgl_setup`'s module docstring and this file's own comments for
what broke and why.
"""

try:
    import _thread
except ImportError:  # pragma: no cover - host CPython fallback
    _thread = None
    import threading

import gc
import os

try:  # lvgl_micropython-only module; absent on host CPython
    import task_handler
except ImportError:  # pragma: no cover - host fallback
    task_handler = None

from tbgw.gui.device_catalog import load_device_catalog
from tbgw.gui.lvgl_setup import init_display_and_touch, LvglSetupError
from tbgw.gui.screens.home_detail_screen import HomeDetailScreen
from tbgw.gui.screens.settings_screen import SettingsScreen
from tbgw.utils.logger import get_logger

logger = get_logger("gui")

_REFRESH_INTERVAL_MS = 400
# Create this file in the filesystem root and reboot to force a fresh
# touch calibration (see _run() for why that is sometimes necessary).
_RECALIBRATE_MARKER = "recalibrate_touch"
# How often TaskHandler pumps LVGL. 33ms (~30fps) is TaskHandler's own
# default and is deliberate here: an earlier value of 5ms made the GUI
# feel snappy but starved the rest of the system, because every pump can
# trigger a flush and this driver's copy task memcpy's a full
# 1024x600x2 (~1.2MB) framebuffer per flush. Measured effect on
# hardware: the asyncio gateway loop ran at HALF its intended rate (a
# 30s heartbeat arriving every 60s), CAN polling nearly stopped, and
# GUI-issued commands took a minute or more to reach the bus -- often
# only landing once the user navigated away and the render load dropped.
_TIMER_HANDLER_INTERVAL_MS = 50
# Refresh ticks between explicit gc.collect() calls -- see _run().
# 25 * 200ms = about every 5s.
_GC_EVERY_N_REFRESHES = 25

# Kept alive for the process lifetime -- see _run() for why.
_task_handler = None
_refresh_timer = None


class _Navigator:
    """Owns which screen is currently active.

    EVERY screen is built once, up front, from the GUI thread -- never
    lazily from inside a touch handler. An earlier revision built each
    device-detail screen on demand to save RAM, and that hard-locked the
    board on real hardware: tapping a control page froze the
    entire system, USB dropped off the bus, and only a physical power
    cycle recovered it -- no panic message, no traceback, nothing
    catchable in Python (wrapping the call in try/except changed
    nothing).

    The reason is where that construction ran. Touch handlers are
    dispatched by LVGL from inside `lv.timer_handler()`, which the
    TaskHandler drives from a `micropython.schedule()` callback on the
    main task's stack. Building a whole detail screen there means
    allocating ~20 LVGL widgets (two tiles' worth) nested several frames
    deep inside that already-deep C call chain, which overruns the
    available stack -- a hard fault, not something Python can trap.

    So navigation callbacks must stay trivial: they only call
    `screen_load()`, which is a pointer swap. All allocation happens here,
    in the constructor, on the GUI thread. At this device count the extra
    resident memory is irrelevant next to 8MB of PSRAM.
    """

    def __init__(self, lv, catalog, gateway):
        self._lv = lv
        self._catalog = catalog
        self._gateway = gateway
        self.current_kind = "device_detail"
        self.current_screen = None

        # The old device-selection screen is intentionally not built: this
        # product has exactly the two primary pages users switch between.
        # Pre-build both pages, keyed by device name, so touch callbacks
        # only load an existing LVGL screen and never allocate widgets.
        self.detail_screens = {}
        self._devices = catalog.all_devices()
        self._smart_switch_device_names = [
            device.device_name for device in self._devices if device.type == "light"
        ]
        thermostat = next((d for d in self._devices if d.type == "thermostat"), None)
        self._start_device_name = thermostat.device_name if thermostat is not None else (
            self._devices[0].device_name if self._devices else None
        )

        for home in catalog.homes:
            for device in home.devices:
                target = self._navigation_target(device.device_name)
                target_device = catalog.get_device(target) if target is not None else None
                try:
                    self.detail_screens[device.device_name] = HomeDetailScreen(
                        lv, home, gateway,
                        devices=(device,), title=device.display_name,
                        on_navigate=(
                            (lambda target_name=target: self.show_device_detail(target_name))
                            if target is not None else None
                        ),
                        navigate_label=(self._navigation_label(target_device) if target_device is not None else None),
                        on_settings=self.show_settings,
                    )
                except Exception as exc:
                    logger.exception(
                        "Could not build detail screen for device %r", device.device_name, exc=exc
                    )

        thingsboard = getattr(gateway, "gui_thingsboard_config", {})
        self.settings_screen = SettingsScreen(
            lv, on_back=self.show_start_device,
            wifi_ssid=getattr(gateway, "gui_wifi_ssid", None),
            thingsboard_host=thingsboard.get("host"),
            thingsboard_port=thingsboard.get("port"),
            on_reconnect=gateway.request_network_recovery,
        )
        self.show_start_device()

    def _navigation_target(self, device_name):
        """Return the other primary device page, if one exists.

        The current delivery has a thermostat and lights page. Keeping this
        lookup catalog-driven avoids hard-coding a display label into LVGL
        callbacks and still gives a sensible direct link if the catalog is
        later extended.
        """
        for device in self._devices:
            if device.device_name != device_name:
                return device.device_name
        return None

    def _refresh_connection_status(self, detail_screen):
        """Update the header only while entering a device page.

        Device telemetry keeps refreshing at the normal GUI interval, but
        connection indicators intentionally remain a navigation-time
        snapshot. This avoids continuously repainting the status bar and
        implements the panel's requested stable page-level status display.
        """
        wlan = getattr(self._gateway, "gui_wifi_manager", None)
        try:
            wifi_connected = wlan is not None and wlan.is_connected()
        except Exception:
            wifi_connected = False

        try:
            thingsboard_connected = self._gateway.uplink_worker.is_healthy()
        except Exception:
            thingsboard_connected = False

        can = getattr(self._gateway, "can_connector", None)
        try:
            can_connected = can is not None and can.is_connected()
        except Exception:
            can_connected = False

        smart_switch_connected = False
        if can_connected and self._smart_switch_device_names:
            try:
                smart_switch_connected = all(
                    can.is_device_online(device_name) is True
                    for device_name in self._smart_switch_device_names
                )
            except Exception:
                smart_switch_connected = False

        detail_screen.refresh_status(
            wifi_connected, thingsboard_connected,
            can_connected, smart_switch_connected,
        )

    @staticmethod
    def _navigation_label(device):
        if device.type == "light":
            return "Lights"
        if device.type == "thermostat":
            return "Thermostat"
        return device.display_name

    def show_start_device(self):
        if self._start_device_name is None:
            logger.warning("No devices configured; cannot open initial GUI screen")
            return
        self.show_device_detail(self._start_device_name)

    def show_device_detail(self, device_name):
        # Must stay trivial -- see the class docstring. No allocation, no
        # screen building: this runs inside an LVGL touch callback.
        detail_screen = self.detail_screens.get(device_name)
        if detail_screen is None:
            logger.warning("show_device_detail: no prebuilt screen for %r", device_name)
            return
        try:
            self._lv.screen_load(detail_screen.screen)
        except Exception as exc:
            logger.exception("Failed to show device detail screen for %r", device_name, exc=exc)
            return
        self._refresh_connection_status(detail_screen)
        self.current_kind = "device_detail"
        self.current_screen = detail_screen

    def show_settings(self):
        # Same containment rule as show_device_detail() -- see there.
        try:
            self._lv.screen_load(self.settings_screen.screen)
        except Exception as exc:
            logger.exception("Failed to open settings screen", exc=exc)
            return
        self.current_kind = "settings"
        self.current_screen = self.settings_screen


def _refresh_current_screen(navigator, gateway):
    if navigator.current_kind == "device_detail":
        navigator.current_screen.refresh_devices(gateway.device_states.snapshot())


def _run(gateway, catalog, runtime_health=None):
    try:
        handles = init_display_and_touch()
    except LvglSetupError as exc:
        logger.error("GUI cannot start: %s", exc)
        return

    lv = handles.lv

    # `lvgl_micropython`'s pointer_framework persists calibration in NVS
    # once done (see indev.is_calibrated), so this normally runs
    # interactively only on a genuinely first boot and is a no-op every
    # boot after. calibrate() briefly takes over the screen and drives
    # LVGL itself while it runs -- both fine to do before the
    # navigator/screens below exist.
    #
    # The marker file forces a re-run. That matters because calibration
    # data is only valid for the display geometry it was captured
    # against: a calibration done while the display was mis-configured
    # (Phase 5 spent a while driving this 1024x600 panel as 800x480)
    # stays stored and silently maps every tap to the wrong place. NVS
    # stores it under a namespace derived from the driver class and
    # instance id, which is awkward to clear from outside, so this
    # marker is the supported way to ask for a fresh one: create
    # `recalibrate_touch` in the filesystem root and reboot.
    force_recalibrate = False
    try:
        os.stat(_RECALIBRATE_MARKER)
        force_recalibrate = True
    except OSError:
        pass

    try:
        if force_recalibrate or not handles.indev.is_calibrated:
            logger.info(
                "Starting touch calibration (%s)",
                "forced by marker file" if force_recalibrate else "not yet calibrated",
            )
            handles.indev.calibrate()
            logger.info("Touch calibration complete")
            if force_recalibrate:
                try:
                    os.remove(_RECALIBRATE_MARKER)
                except OSError:
                    logger.warning("Could not remove %s -- calibration will re-run next boot",
                                   _RECALIBRATE_MARKER)
    except Exception:
        logger.exception("Touch calibration failed -- continuing uncalibrated")

    navigator = _Navigator(lv, catalog, gateway)

    # LVGL IS NOT THREAD-SAFE, and this function runs on its own thread.
    # An earlier revision pumped LVGL directly from here in a
    # `while True: lv.timer_handler()` loop. That deadlocked on real
    # hardware -- silently, with no exception and no log line: the screen
    # rendered once and then froze, touch never responded, and the status
    # bar stayed on its initial "Starting..." text forever. The cause is
    # that `indev.calibrate()` internally starts a
    # `task_handler.TaskHandler` (machine.Timer + micropython.schedule)
    # which pumps LVGL on its own and is never stopped, so this thread's
    # manual pump raced against it for LVGL's internal state.
    #
    # The fix is to have exactly ONE pump, the vendor-supported one, and
    # to do our periodic refresh as an LVGL timer so it is called from
    # inside that same pump rather than from this thread. After this
    # setup the thread has nothing left to do and returns; TaskHandler's
    # machine.Timer keeps everything running, so the module-level
    # references below exist purely to keep both objects alive against
    # garbage collection.
    global _task_handler, _refresh_timer

    # A custom exception_hook is ESSENTIAL, not optional. TaskHandler's
    # own `_default_exception_hook` responds to ANY exception by calling
    # `TaskHandler._current_instance.deinit()` -- permanently stopping the
    # LVGL pump. Caught on hardware: a single transient
    # `MemoryError: memory allocation failed, allocating 36843 bytes`
    # inside `lv.task_handler()` killed the pump for good, so the screen
    # froze and touch died for the rest of the boot while the gateway
    # threads carried on heartbeating -- which is exactly what made this
    # look like a mysterious "GUI-only hang". Log and keep pumping
    # instead: a dropped frame is survivable, a dead display is not.
    def _on_lvgl_exception(exc):
        logger.exception("LVGL task handler error (pump kept alive)", exc=exc)

    _task_handler = task_handler.TaskHandler(
        duration=_TIMER_HANDLER_INTERVAL_MS, exception_hook=_on_lvgl_exception
    )

    refresh_count = [0]

    def _on_refresh_timer(_timer):
        # This callback is executed by TaskHandler's LVGL pump.  Its
        # heartbeat therefore proves the display/touch scheduling path is
        # still live, rather than merely proving the setup thread returned.
        if runtime_health is not None:
            runtime_health.beat("gui")
        try:
            _refresh_current_screen(navigator, gateway)
        except Exception as exc:
            # One bad refresh must never take the display down.
            logger.exception("GUI refresh failed", exc=exc)

        # LVGL's renderer needs large-ish CONTIGUOUS blocks (a ~36KB
        # allocation is what failed on hardware). Total free memory was
        # never the problem -- fragmentation was, since this refresh
        # allocates a fresh state snapshot plus label strings several
        # times a second. An occasional explicit collect keeps the heap
        # compact enough for the renderer to get its block. Deliberately
        # not every tick: gc.collect() is not free.
        refresh_count[0] += 1
        if refresh_count[0] % _GC_EVERY_N_REFRESHES == 0:
            gc.collect()

    _refresh_timer = lv.timer_create(_on_refresh_timer, _REFRESH_INTERVAL_MS, None)
    _refresh_timer.set_repeat_count(-1)

    logger.info("GUI running (TaskHandler pump, %dms refresh)", _REFRESH_INTERVAL_MS)


def start_gui_thread(gateway, core=None, catalog_path="config/home_devices.json",
                     runtime_health=None):
    """Loads the device catalog and starts the Core 0 GUI thread. `core`
    is accepted for API symmetry with the rest of this project's
    core-assignment conventions (tbgw.core.constants.CORE_GUI) but, per
    the core-affinity caveat documented there, isn't currently able to
    force actual thread placement -- see that module's docstring.
    """
    try:
        catalog = load_device_catalog(catalog_path)
    except (OSError, ValueError) as exc:
        logger.error("GUI cannot start: failed to load device catalog from %s: %s", catalog_path, exc)
        return

    if _thread is not None:
        _thread.start_new_thread(_run, (gateway, catalog, runtime_health))
    else:  # pragma: no cover - host fallback if _thread is ever unavailable
        threading.Thread(target=_run, args=(gateway, catalog, runtime_health), daemon=True).start()

    logger.info(
        "GUI thread requested on core %s (%d homes, %d devices)",
        core, len(catalog.homes), len(catalog.all_devices()),
    )
