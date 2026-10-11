"""tbgw.utils.logger
======================

MicroPython has no `logging` module. This is a small, dependency-free,
leveled logger that:

  * supports named loggers (`get_logger("can_connector")`) the same way
    `logging.getLogger(__name__)` does upstream in thingsboard-gateway,
  * filters by level per-logger (falls back to a global default level),
  * formats a single line: "<ts> - <LEVEL> - [<name>] - <message>",
  * optionally fans every emitted record out to a list of "sinks" -- plain
    callables of the form `sink(name, level, message)` -- so that Phase 3's
    ThingsBoard uplink client can register itself as a remote-log sink
    without this module needing to know anything about MQTT.

It is deliberately not a drop-in `logging` clone: no Handler/Formatter
object graph, no propagation tree, no lazy %-style interpolation beyond a
single `*args` convenience. That machinery isn't worth its RAM cost on a
microcontroller, and the upstream gateway's own log *messages* (not its
Handler architecture) are what actually needs to be preserved for
debugging parity.
"""

try:
    import time
except ImportError:  # pragma: no cover
    time = None

try:
    import _thread
    _lock = _thread.allocate_lock()
except ImportError:  # pragma: no cover - host CPython fallback
    import threading
    _lock = threading.Lock()


LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "EXCEPTION": 50,
}

_LEVEL_NAMES = {v: k for k, v in LEVELS.items()}

# Global default level used by any logger that hasn't had set_level() called
# on it explicitly. Configurable at boot from config/logs.json.
_global_level = LEVELS["INFO"]

# name -> TbLogger instance, so get_logger("x") always returns the same
# object (mirrors logging.getLogger's caching behaviour).
_registry = {}

# list of callables: sink(logger_name, level_name, message)
_sinks = []


def _timestamp():
    if time is None:
        return "0000-00-00 00:00:00"
    try:
        y, mo, d, h, mi, s = time.localtime()[:6]
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(y, mo, d, h, mi, s)
    except Exception:
        return str(time.time())


class TbLogger:
    """A single named logger. Do not instantiate directly -- use get_logger()."""

    __slots__ = ("name", "level")

    def __init__(self, name, level=None):
        self.name = name
        self.level = level  # None => use _global_level

    def set_level(self, level):
        """level may be an int (see LEVELS) or one of the LEVELS keys."""
        if isinstance(level, str):
            level = LEVELS[level.upper()]
        self.level = level

    def _effective_level(self):
        return self.level if self.level is not None else _global_level

    def _log(self, level_name, message, *args):
        level = LEVELS[level_name]
        if level < self._effective_level():
            return
        if args:
            try:
                message = message % args
            except Exception:
                # Never let a bad format string take the whole gateway down.
                message = message + " " + " ".join(str(a) for a in args)
        line = "{} - {} - [{}] - {}".format(_timestamp(), level_name, self.name, message)

        # DO NOT hold _lock across print() or the sink fan-out.
        #
        # An earlier revision did, and it deadlocked the entire gateway on
        # real hardware: silently, with no panic and no output at all,
        # because the wedged lock was the logger's own. The cycle is a
        # textbook ABBA:
        #   * this path took _lock, then called the ThingsBoard remote-log
        #     sink, which enqueues to the uplink worker and so wants the
        #     worker's queue lock;
        #   * the CAN/uplink threads take their own lock first and then
        #     log, wanting _lock.
        # Any global lock held while calling out to arbitrary code invites
        # exactly this, and a logger is called from every thread, so it is
        # the worst possible place to hold one.
        #
        # The lock was only ever protecting against interleaved output.
        # The line is fully formatted above, so a single print() call is
        # already atomic enough under the GIL, and sinks are iterated over
        # a snapshot so concurrent register/unregister cannot disturb it.
        print(line)
        for sink in tuple(_sinks):
            try:
                sink(self.name, level_name, message)
            except Exception:
                # A misbehaving remote-log sink must never crash local logging.
                pass

    def debug(self, message, *args):
        self._log("DEBUG", message, *args)

    def info(self, message, *args):
        self._log("INFO", message, *args)

    def warning(self, message, *args):
        self._log("WARNING", message, *args)

    def error(self, message, *args):
        self._log("ERROR", message, *args)

    def exception(self, message, *args, exc=None):
        """Logs at EXCEPTION level and, when given the exception object,
        prints its traceback the way `logging.exception()` does upstream.

        **Pass `exc=` explicitly** -- e.g.
        `except Exception as e: logger.exception("...", exc=e)`.
        MicroPython has no `sys.exc_info()`, so unlike CPython's
        `logging.exception()` there is no way to recover the active
        exception from inside this call; without `exc` this degrades to a
        bare message. That matters because a line like
        "EXCEPTION - GUI refresh failed" with no further detail looks
        like a logged error while carrying none of the information needed
        to act on it -- Phase 5 bring-up lost real time to exactly that.

        The traceback goes straight to stdout rather than through
        _log()/sinks: it is multi-line, and would otherwise corrupt the
        one-record-per-line format the remote-log sink and any log parser
        rely on.
        """
        self._log("EXCEPTION", message, *args)
        if exc is None:
            return
        try:
            import sys
            if hasattr(sys, "print_exception"):  # MicroPython
                sys.print_exception(exc)
            else:  # host CPython
                import traceback
                traceback.print_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            pass  # logging must never itself raise


def get_logger(name):
    """Returns the (cached) TbLogger instance for `name`."""
    with _lock:
        logger = _registry.get(name)
        if logger is None:
            logger = TbLogger(name)
            _registry[name] = logger
        return logger


def set_global_level(level):
    """Sets the default level used by loggers that have no explicit level."""
    global _global_level
    if isinstance(level, str):
        level = LEVELS[level.upper()]
    _global_level = level


def configure_from_dict(levels_dict):
    """Bulk-configure per-module levels, e.g. the parsed contents of
    config/logs.json: {"default": "INFO", "can_connector": "DEBUG", ...}.
    """
    default = levels_dict.get("default")
    if default is not None:
        set_global_level(default)
    for name, level in levels_dict.items():
        if name == "default":
            continue
        get_logger(name).set_level(level)


def register_sink(sink):
    """Registers a callable(logger_name, level_name, message) invoked for
    every log record, in addition to the local print(). Used by Phase 3 to
    forward ERROR/EXCEPTION records to ThingsBoard's remote-log feature.
    Returns the sink so it can be later passed to unregister_sink().
    """
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)
    return sink


def unregister_sink(sink):
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)
