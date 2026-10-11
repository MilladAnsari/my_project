"""tbgw.storage.base
======================

MicroPython has no `abc` module (no `ABC`/`@abstractmethod`). Upstream's
`EventStorage` base class is reproduced here using the plain-Python
convention of raising `NotImplementedError` from stub methods, which is
what the prior port's report also settled on for the same reason.
"""


class EventStorage:
    """Common interface every storage backend (currently only
    MemoryEventStorage) must implement. A "pack" is a list of previously
    `put()` events retrieved together via get_event_pack(), so the caller
    (the ThingsBoard uplink client) can batch multiple events into a single
    MQTT publish and only remove them from storage once ThingsBoard has
    acknowledged receipt (event_pack_processing_done()).
    """

    def put(self, event):
        """Stores one converted event (a dict; shape defined by the
        connector's uplink converter, ultimately compatible with the
        Gateway MQTT API's telemetry/attributes payload shape).
        Raises Full-like errors per implementation on overflow, depending
        on the backend's configured overflow policy.
        """
        raise NotImplementedError

    def get_event_pack(self, max_size):
        """Returns (without removing) up to `max_size` pending events, as a
        list, in FIFO order. Calling this again before
        event_pack_processing_done() must return the SAME pack (i.e. the
        pack is "checked out", not consumed) so a failed uplink attempt can
        be retried without data loss.
        """
        raise NotImplementedError

    def event_pack_processing_done(self):
        """Confirms the most recently checked-out pack (via
        get_event_pack()) was delivered, permanently removing it from
        storage.
        """
        raise NotImplementedError

    def len(self):
        """Number of events currently stored (including any checked-out
        pack not yet confirmed done).
        """
        raise NotImplementedError
