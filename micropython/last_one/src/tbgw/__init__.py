"""tbgw -- ThingsBoard IoT Gateway port for MicroPython on ESP32-S3.

Sub-packages:
    core        gateway orchestration, config, RPC, ThingsBoard uplink
    connectors  protocol connectors (mqtt/, can/)
    storage     local event buffering
    gui         LVGL local monitoring/control UI (Core 0)
    utils       hardware-independent helpers (logger, queue, event, ...)

See docs/architecture.md for the overall design and docs/pin_map.md for
the target board's GPIO assignments.
"""

__version__ = "0.1.0-phase1"
