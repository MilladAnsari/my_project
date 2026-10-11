"""tbgw.connectors.can -- CAN (TWAI) connector.

Implemented in Phase 2. Wraps the vendored `straga/micropython-esp32-twai`
C module (via twai_driver.py, which also provides a MockTwaiBus fallback
for host-side testing) behind CANConnector, CanUplinkConverter, and
CanDownlinkConverter. Runs its RX/TX thread on Core 1
(tbgw.core.constants.CORE_CAN), fed by the driver's ISR-buffered frame
queue, using GPIO15 (TX) / GPIO16 (RX) per docs/pin_map.md.
"""
