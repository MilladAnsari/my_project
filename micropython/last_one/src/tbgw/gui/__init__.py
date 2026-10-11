"""tbgw.gui -- LVGL-based smart-home monitoring/control UI.

Implemented in Phase 4. Runs on Core 0 (tbgw.core.constants.CORE_GUI) in
its own dedicated thread, independent of the Core 1 asyncio gateway loop
+ CAN thread, so display rendering and touch response stay smooth
regardless of network/bus load. See docs/architecture.md's "GUI
architecture (Phase 4)" section, including the important caveat that
LVGL itself requires custom firmware (vendored `lvgl_micropython`), same
as CAN requires the vendored TWAI module.
"""
