"""tbgw.connectors.can.twai_driver
====================================

Thin adapter over the vendored `straga/micropython-esp32-twai` C module
(see build/build_firmware.sh), isolated behind a small, stable interface
(`TwaiDriver`) so that:

  1. `can_connector.py` never calls the third-party module's API directly
     -- if its exact function/class names change between versions, only
     this one file needs updating;
  2. the connector's logic (frame decode/encode, converters, RPC
     dispatch) can be unit-tested on a desktop CPython interpreter using
     `MockTwaiBus`, without any ESP32 hardware or the native module
     present.

This is a REWRITE of the Phase 2 version of this file, which was written
against an assumed API that turned out to be wrong in every particular
once tested against real hardware (Phase 1 bring-up). The real,
hardware-verified API of the vendored module (confirmed on an
ESP32-S3-Touch-LCD-5B, real xxx bus, 2026-09-05):

    import CAN
    dev = CAN(0, extframe=False, tx=15, rx=16, mode=CAN.NORMAL,
              bitrate=50000, auto_restart=False)   # no separate start()
    dev.any()                                       # -> bool
    dev.recv()                                       # -> (id, extended, rtr, data)
    dev.send(data, id)                               # data FIRST, id second;
                                                       # data must be a list/tuple
                                                       # of ints -- a bytes object
                                                       # is REJECTED outright
                                                       # ("object 'bytes' isn't a
                                                       # tuple or list"), asymmetric
                                                       # with recv(), which returns
                                                       # actual bytes for its data
    dev.state()                                      # -> int (0 stopped/1 running/2 bus-off/3 recovering)
    dev.info()                                       # -> dict: state, tx_error_counter,
                                                       #    rx_error_counter, bus_error_count,
                                                       #    msgs_to_tx, msgs_to_rx,
                                                       #    tx_failed_count, rx_missed_count,
                                                       #    arb_lost_count
    dev.deinit()

Traps hit and worth remembering (see docs/can_connector_design.md
section 9 for the full writeup):

  * There is NO blocking receive and no timeout parameter on recv() --
    it is a plain `any()`-poll-then-`recv()` API. The RX loop must poll
    with a short sleep between checks.
  * The module names `get_state()`/`get_counters()`/`get_timings()` --
    the underscore-prefixed-sounding "getter" names -- are unimplemented
    stubs that always return None. The REAL, working methods are
    `state()` and `info()` (no `get_` prefix).
  * The CAN object is a process-wide SINGLETON. Constructing a second one
    without calling `deinit()` on the first raises
    `OSError(-259, 'ESP_ERR_INVALID_STATE')`. A MicroPython *soft* reset
    does NOT clear this (the underlying driver install is C-level static
    state) -- only `deinit()` or a hard reset does.
  * A previously-shipped bug in the vendored module silently ignored the
    requested `bitrate=` on ESP-IDF >= 5.x (ALL `TWAI_TIMING_CONFIG_*()`
    macros there set `.brp = 0`, and the module's "initialize singleton
    timing on first use" guard was keyed on `brp == 0`, which is then
    always true). This is patched in the vendored copy this project
    builds -- see build/patches/. Symptom if a future re-vendor drops
    the patch: the driver's own printed `CAN: timing ...` values are
    IDENTICAL regardless of the requested bitrate, and `info()["bus_error_count"]`
    climbs into the tens of thousands with zero received frames.
"""

from tbgw.utils.logger import get_logger

logger = get_logger("can_connector")

# Hardware-debug switch: set True to suppress every real CAN transmit
# while leaving the whole GUI/command path intact. Used to bisect a
# Phase 5 whole-system freeze between the CAN driver and the display
# stack (the freeze persisted with TX stubbed, which is what pointed at
# lvgl_micropython's RGB driver -- see build/patches/). MUST be False
# for normal operation.
_DRY_RUN_TX = False

# Transmit timeout in ms. Kept SHORT on purpose: it bounds how long the
# native send() blocks waiting for the module's TX-completion flag, and
# that flag is unreliable under load (see send()). It is not a deadline
# for the frame itself -- the frame is already queued by then.
_TX_TIMEOUT_MS = 10


_ETIMEDOUT = 116  # errno on this port; OSError.args[0] for ESP_ERR_TIMEOUT


def _errno_of(exc):
    """Best-effort errno extraction -- MicroPython OSErrors carry it as
    args[0], but a few paths raise with a message only.
    """
    try:
        return exc.args[0]
    except (AttributeError, IndexError):
        return None


class TwaiError(Exception):
    pass


class CanFrame:
    """Normalizes the native module's `(id, extended, rtr, data)` tuple
    (and the mock's constructor args) into one shape can_connector.py
    can rely on regardless of backend.
    """

    __slots__ = ("arbitration_id", "data", "extended", "rtr")

    def __init__(self, arbitration_id, data, extended=False, rtr=False):
        self.arbitration_id = arbitration_id
        self.data = bytes(data)
        self.extended = extended
        self.rtr = rtr

    def __repr__(self):
        return "CanFrame(id=0x{:X}, data={}, extended={})".format(
            self.arbitration_id, self.data, self.extended
        )


class TwaiDriver:
    """Public interface used by can_connector.py. Construct via
    `TwaiDriver.create(...)`, which picks the native backend when the
    vendored C module is importable, or MockTwaiBus otherwise (logging a
    clear warning so a mock silently standing in for real hardware is
    never mistaken for the real thing).
    """

    @staticmethod
    def create(tx_pin, rx_pin, bitrate, extended_id=False, logger_=None):
        try:
            import CAN as _can_native  # noqa: F401  -- provided by the vendored straga module
            return _NativeTwaiDriver(tx_pin, rx_pin, bitrate, extended_id, logger_ or logger)
        except ImportError:
            (logger_ or logger).warning(
                "Native 'CAN' module not importable -- falling back to MockTwaiBus. "
                "This is expected on a desktop/host interpreter; on real hardware this "
                "means the custom firmware from build/build_firmware.sh was not flashed."
            )
            return MockTwaiBus(tx_pin, rx_pin, bitrate, extended_id, logger_ or logger)

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def send(self, arbitration_id, data, extended=False):
        """Returns True if the frame was queued/transmitted, False on a
        driver-reported failure (must not raise for a normal transmit
        failure -- callers treat False as "log and move on", the TX
        queue does not retry automatically).
        """
        raise NotImplementedError

    def recv(self):
        """Non-blocking: returns a CanFrame if one is immediately
        available, else None. There is no native blocking-with-timeout
        primitive -- callers poll this from a loop with their own sleep.
        """
        raise NotImplementedError

    def recover(self):
        """Attempt to bring the controller back after BUS_OFF. Returns
        True if it is RUNNING afterwards. Default: nothing to do.
        """
        return True

    def get_status(self):
        """Returns a dict, at minimum: {"state": "running"|"bus_off"|"stopped"|"recovering",
        "bus_error_count": int, "tx_error_counter": int, "rx_error_counter": int}.
        """
        raise NotImplementedError


_STATE_NAMES = {0: "stopped", 1: "running", 2: "bus_off", 3: "recovering"}


class _NativeTwaiDriver(TwaiDriver):
    """Wraps the vendored straga/micropython-esp32-twai module (the real,
    hardware-verified API -- see this file's module docstring).
    """

    def __init__(self, tx_pin, rx_pin, bitrate, extended_id, logger_):
        import CAN
        self._CAN = CAN
        self._tx_pin = tx_pin
        self._rx_pin = rx_pin
        self._bitrate = bitrate
        self._extended_id = extended_id
        self._logger = logger_
        self._dev = None

    def start(self):
        # Construction IS start -- there is no separate start() call.
        self._dev = self._CAN(
            0, extframe=self._extended_id, tx=self._tx_pin, rx=self._rx_pin,
            # auto_restart MUST stay False: straga's module raises
            # NotImplementedError("Auto-restart not supported") outright
            # if it is True. BUS_OFF recovery is therefore done from
            # Python instead -- see recover() below and can_connector.py's
            # _bus_ok().
            mode=self._CAN.NORMAL, bitrate=self._bitrate, auto_restart=False,
        )
        self._logger.info(
            "TWAI started: tx=GPIO%s rx=GPIO%s bitrate=%s extended=%s",
            self._tx_pin, self._rx_pin, self._bitrate, self._extended_id,
        )

    def stop(self):
        if self._dev is not None:
            try:
                self._dev.deinit()
            except Exception:
                self._logger.exception("TWAI deinit failed")
            self._dev = None
            self._logger.info("TWAI stopped")

    def recover(self):
        """Re-initialises the controller after BUS_OFF. Returns True if it
        came back RUNNING.

        Deliberately does NOT use the native `CAN.restart()`: that method
        spins on `while (bus_recovery_success < 0)` with no timeout, so if
        the bus never becomes healthy (unterminated, unplugged) it never
        returns. A deinit + fresh construction is bounded and gets us to
        the same place. Note the native CAN object is a process-wide
        singleton, so the deinit in stop() is required before start()
        can construct a new one.
        """
        try:
            self.stop()
            self.start()
        except Exception as exc:
            self._logger.warning("TWAI recovery failed: %s", exc)
            return False
        try:
            return self.get_status().get("state") == "running"
        except Exception:
            return False

    def send(self, arbitration_id, data, extended=False):
        # ---- TEMPORARY DIAGNOSTIC, REMOVE ----------------------------
        # Set True to stub out every real CAN transmit while leaving the
        # entire GUI/command path intact, to bisect a freeze between the
        # CAN driver and the display/GUI stack. Unsolicited RX (device
        # events) still arrives, so the screen should keep updating when
        # a light is switched physically.
        if _DRY_RUN_TX:
            self._logger.info(
                "DRY-RUN: suppressing CAN TX (id=0x%X, data=%s)", arbitration_id, bytes(data)
            )
            return True
        # --------------------------------------------------------------
        try:
            # Asymmetric with recv() (which returns bytes): the native
            # send() rejects a bytes object outright ("object 'bytes'
            # isn't a tuple or list") -- confirmed on real hardware. It
            # wants a list (or tuple) of ints, matching the vendor's own
            # README example (`dev.send(msg_data, msg_id)` with
            # `msg_data` built as a plain list).
            #
            # An explicit `timeout=` is REQUIRED, and is only safe
            # because the firmware this project builds patches the
            # module's timeout handling.
            #
            # Without a timeout, mod_can.c takes a
            # `twai_transmit_v2(..., portMAX_DELAY)` branch that waits
            # unboundedly for TX-queue space while holding the
            # MicroPython GIL -- so a bus that never drains (unterminated,
            # nothing ACKing) freezes EVERY thread in the process with no
            # traceback and no recovery short of a power cycle. Confirmed
            # on hardware, twice.
            #
            # Stock, the bounded branch was equally unusable: it compared
            # elapsed MICROseconds against a MILLIsecond timeout, so any
            # value expired ~1000x early and virtually every send raised
            # ETIMEDOUT despite the frame being queued fine (which in turn
            # made the read scheduler retry forever and flooded the
            # console). That units bug is fixed in
            # build/patches/micropython-esp32-twai-fixes.patch, so the
            # value below now means what it says.
            self._dev.send(list(data), arbitration_id, timeout=_TX_TIMEOUT_MS)
            return True
        except OSError as exc:
            # ETIMEDOUT here does NOT mean the frame was not sent.
            #
            # mod_can.c queues the frame first (a bounded
            # twai_transmit_v2 call) and only then spins waiting for a
            # TX-completion flag that a separate FreeRTOS alert task
            # sets. Under GUI load that task does not get scheduled
            # promptly, so the wait expires even though the frame went
            # out fine. Treating that as failure was actively harmful:
            # the read scheduler never recorded its reads as sent, so
            # replies arrived uncorrelated, devices never came online,
            # and every datapoint was retried forever.
            #
            # A genuinely full TX queue surfaces the same errno and is
            # indistinguishable here, so this deliberately errs toward
            # "queued". can_connector._bus_ok() is what catches the case
            # that actually matters (a controller that has stopped
            # transmitting at all).
            if _errno_of(exc) == _ETIMEDOUT:
                self._logger.debug("TWAI send: completion flag not seen (id=0x%X) -- treating as queued",
                                   arbitration_id)
                return True
            self._logger.debug("TWAI send failed (id=0x%X): %s", arbitration_id, exc)
            return False
        except Exception as exc:
            # debug, not warning: can_connector._send_frame() already
            # reports TX failures with rate limiting. Logging every
            # failure here too is what turned a bus fault into a
            # console-I/O flood that slowed the whole system.
            self._logger.debug("TWAI send failed (id=0x%X): %s", arbitration_id, exc)
            return False

    def recv(self):
        if not self._dev.any():
            return None
        try:
            frame_id, extended, rtr, data = self._dev.recv()
        except Exception:
            self._logger.exception("TWAI recv() failed despite any()==True")
            return None
        return CanFrame(arbitration_id=frame_id, data=data, extended=extended, rtr=rtr)

    def get_status(self):
        try:
            info = self._dev.info()
        except Exception:
            self._logger.exception("TWAI info() failed")
            return {"state": "unknown"}
        info = dict(info)
        info["state"] = _STATE_NAMES.get(info.get("state"), "unknown")
        return info


class MockTwaiBus(TwaiDriver):
    """Software-only stand-in for the native TWAI driver, used for:
      * host-side unit tests of can_connector.py / the converters,
      * development/demoing the gateway core without a physical CAN bus.

    Test code drives this via `inject_rx_frame()`; `send()` just records
    what was transmitted in `sent_frames` for assertions.
    """

    def __init__(self, tx_pin, rx_pin, bitrate, extended_id, logger_):
        self._tx_pin = tx_pin
        self._rx_pin = rx_pin
        self._bitrate = bitrate
        self._extended_id = extended_id
        self._logger = logger_
        self._rx_queue = []
        self.sent_frames = []
        self._running = False

    def start(self):
        self._running = True
        self._logger.info(
            "MockTwaiBus started (tx=GPIO%s, rx=GPIO%s, bitrate=%s) -- NOT real hardware",
            self._tx_pin, self._rx_pin, self._bitrate,
        )

    def stop(self):
        self._running = False

    def recover(self):
        # A stopped mock bus stays stopped -- mirrors a real controller
        # that cannot recover while the bus is still faulty, so tests of
        # the bus-health gate exercise the suppressed path for real.
        return self._running

    def send(self, arbitration_id, data, extended=False):
        if not self._running:
            return False
        self.sent_frames.append(CanFrame(arbitration_id, bytes(data), extended))
        return True

    def recv(self):
        if self._rx_queue:
            return self._rx_queue.pop(0)
        return None

    def get_status(self):
        return {
            "state": "running" if self._running else "stopped",
            "bus_error_count": 0, "tx_error_counter": 0, "rx_error_counter": 0,
        }

    # --- test-only helpers, not part of the TwaiDriver interface ---------
    def inject_rx_frame(self, arbitration_id, data, extended=False, rtr=False):
        self._rx_queue.append(CanFrame(arbitration_id, data, extended, rtr))
