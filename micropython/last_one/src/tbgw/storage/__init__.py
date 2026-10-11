"""tbgw.storage -- pluggable local buffering of converted telemetry/attribute
events between a connector and the ThingsBoard uplink client.

Only MemoryEventStorage ships in this port (see architecture.md, section
"Storage/backpressure" -- flash-backed persistence is a deliberate stretch
goal, not an oversight).
"""
