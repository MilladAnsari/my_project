"""tbgw.connectors.base
=========================

Common interface implemented by both connectors in this port (MQTTConnector,
Phase 3; CANConnector, Phase 2). Mirrors upstream's `Connector` base class,
using the `raise NotImplementedError` convention in place of `abc`
(see storage/base.py for the same rationale).

Also carries a few simple statistics counters shared by every connector,
since both the GUI (Phase 4, "status bar") and the gateway core's periodic
stats log want the same shape of data regardless of which protocol is
underneath.
"""


class Connector:
    def __init__(self, name, connector_type, config, gateway=None, logger=None):
        self.name = name
        self.connector_type = connector_type
        self.config = config
        self.gateway = gateway  # back-reference to TBGatewayService, Phase 3
        self.logger = logger
        self.bytes_received = 0
        self.bytes_sent = 0
        self.messages_received = 0
        self.messages_sent = 0

    def open(self):
        """Starts the connector (typically: spins up its own thread and/or
        registers asyncio tasks, and returns immediately -- must not block
        the caller). Raises NotImplementedError if not overridden.
        """
        raise NotImplementedError

    def close(self):
        """Stops the connector and releases any underlying resources
        (sockets, driver handles, threads).
        """
        raise NotImplementedError

    def is_connected(self):
        raise NotImplementedError

    def get_name(self):
        return self.name

    def get_type(self):
        return self.connector_type

    def get_statistics(self):
        return {
            "bytesReceived": self.bytes_received,
            "bytesSent": self.bytes_sent,
            "messagesReceived": self.messages_received,
            "messagesSent": self.messages_sent,
        }

    def on_attribute_update(self, device_name, attribute_update):
        """Called by the RPC dispatcher (Phase 3) when a shared attribute
        this connector's device is subscribed to changes. Default is a
        no-op; connectors that support downlink attribute writes (CAN)
        override this.
        """
        pass

    def on_rpc_request(self, device_name, rpc_request):
        """Called by the RPC dispatcher (Phase 3) when ThingsBoard sends an
        RPC command for a device owned by this connector. Default is a
        no-op that connectors supporting downlink RPC (CAN, and MQTT if
        forwarding to the external broker) override.
        """
        pass
