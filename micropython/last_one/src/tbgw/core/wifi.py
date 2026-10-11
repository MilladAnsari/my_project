"""tbgw.core.wifi
===================

Wi-Fi station bring-up. Unlike CPython/upstream TBGW's host environment,
MicroPython has no network connectivity by default -- something has to
explicitly bring up the WLAN interface before anything network-dependent
(the ThingsBoard uplink, NTP) can work. This was a documented gap in
earlier phases of this port: neither boot.py nor main.py touched
`network.WLAN` at all.

Credentials live in config/wifi.json (checked in with placeholder
values) or, preferably, config/wifi.local.json (gitignored -- see
.gitignore's pre-existing "config/*.local.json" rule) with real values;
see tbgw.core.config_loader.load_wifi_config().

`WifiManager` isolates the real `network.WLAN` calls behind a small
interface (mirroring twai_driver.py/mqtt_client_adapter.py's
adapter+mock pattern) so `connect_with_retry()`'s actual LOGIC --
backoff timing, per-attempt timeout, give-up behavior -- is host-testable
without a real radio.
"""

try:
    import network
except ImportError:  # pragma: no cover - host CPython fallback
    network = None

try:
    import time
except ImportError:  # pragma: no cover
    time = None


class WifiError(Exception):
    pass


def _sleep_ms(ms):
    if time is None:
        return
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


def _now_ms():
    if time is None:
        return 0
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _elapsed_ms(start_ms):
    if time is None:
        return 0
    now = _now_ms()
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(now, start_ms)
    return now - start_ms


class WifiManager:
    """Public interface used by connect_with_retry() and main.py. Construct
    via `WifiManager.create(...)`, which picks the native backend when
    `network` is importable, or a mock otherwise.
    """

    @staticmethod
    def create(logger_=None):
        if network is None:
            return _MockWlan(logger_)
        return _RealWlan(logger_)

    def is_connected(self):
        raise NotImplementedError

    def connect(self, ssid, password):
        raise NotImplementedError

    def ifconfig(self):
        raise NotImplementedError


class _RealWlan(WifiManager):
    def __init__(self, logger_=None):
        self._logger = logger_
        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)

    def is_connected(self):
        return self._wlan.isconnected()

    def connect(self, ssid, password):
        self._wlan.connect(ssid, password)

    def ifconfig(self):
        return self._wlan.ifconfig()


class _MockWlan(WifiManager):
    """Host-test / no-`network`-module stand-in. By default simulates a
    connection succeeding immediately after connect() is called, so
    connect_with_retry()'s logic can be exercised without a real radio --
    set `should_connect = False` to simulate a network that never comes
    up (for testing the retry/give-up path).
    """

    def __init__(self, logger_=None):
        self._logger = logger_
        self._connected = False
        self.should_connect = True
        self.connect_calls = 0

    def is_connected(self):
        return self._connected

    def connect(self, ssid, password):
        self.connect_calls += 1
        if self.should_connect:
            self._connected = True

    def ifconfig(self):
        return ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")


def connect_with_retry(wlan, ssid, password, timeout_s=20, max_attempts=0,
                        backoff_s=2.0, logger=None):
    """Blocking. Repeatedly calls wlan.connect() and polls is_connected()
    for up to `timeout_s` per attempt, backing off `backoff_s` between
    attempts.

    `max_attempts=0` means retry forever. Intended for main.py's initial
    boot-time attempt, use a BOUNDED `max_attempts` there and let the
    board carry on regardless (the CAN connector and local GUI are
    supposed to keep working even with no internet at all -- only the
    ThingsBoard uplink genuinely needs Wi-Fi, and its own worker thread
    retries connecting independently and indefinitely once Wi-Fi is up).

    Returns True once connected, False if `max_attempts` was exceeded
    without connecting.
    """
    attempt = 0
    while True:
        attempt += 1
        if logger is not None:
            logger.info("Wi-Fi: connecting to %r (attempt %d)...", ssid, attempt)
        try:
            wlan.connect(ssid, password)
        except Exception:
            if logger is not None:
                logger.exception("Wi-Fi: connect() raised")

        start = _now_ms()
        while _elapsed_ms(start) < timeout_s * 1000:
            if wlan.is_connected():
                if logger is not None:
                    logger.info("Wi-Fi: connected, ifconfig=%s", wlan.ifconfig())
                return True
            _sleep_ms(250)

        if logger is not None:
            logger.warning("Wi-Fi: attempt %d timed out after %ss", attempt, timeout_s)
        if max_attempts and attempt >= max_attempts:
            return False
        _sleep_ms(int(backoff_s * 1000))
