"""tbgw.core.ntp
==================

NTP time sync. MicroPython's on-device clock is not Unix-epoch-correct
out of the box -- the ESP32 port's RTC defaults to a 2000-01-01 epoch,
not 1970-01-01 (see tbgw.utils.tb_utility.unix_time_ms(), which corrects
for this once synced). Requires Wi-Fi already up (tbgw.core.wifi).

`sync()` is a one-shot, best-effort call made once at boot; nothing in
this port depends on periodic resync -- a drift of a few seconds over a
day's uptime is immaterial for telemetry timestamps.
"""

try:
    import ntptime
except ImportError:  # pragma: no cover - host CPython, or a MicroPython
    # build that didn't freeze the ntptime module in
    ntptime = None

_synced = False


def is_synced():
    """True once sync() has succeeded at least once this boot. Consulted
    by tbgw.core.gateway_service before attaching a "ts" to an outgoing
    event -- sending a pre-sync, wrong-epoch timestamp would be worse
    than omitting it (ThingsBoard assigns receive time server-side when
    "ts" is absent).
    """
    return _synced


def sync(server=None, retries=3, logger=None):
    """Returns True if the on-device clock now reflects real Unix time.

    Safe to call even when the `ntptime` module isn't available (host
    tests, or a firmware build that didn't freeze it in) -- returns
    False immediately rather than raising, matching this project's
    convention of degrading gracefully when a piece of the stack isn't
    present.
    """
    global _synced
    if ntptime is None:
        if logger is not None:
            logger.warning("ntptime module not available -- skipping NTP sync")
        return False

    if server is not None:
        ntptime.host = server

    for attempt in range(1, retries + 1):
        try:
            ntptime.settime()
            _synced = True
            if logger is not None:
                logger.info("NTP sync OK (attempt %d)", attempt)
            return True
        except Exception as exc:
            if logger is not None:
                logger.warning("NTP sync attempt %d failed: %s", attempt, exc)
    return False
