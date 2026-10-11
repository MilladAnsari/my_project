"""tbgw.gui.widgets.status_bar
================================

Persistent top status bar showing Wi-Fi, ThingsBoard, and the combined
CAN/smart-switch link. Display formatting logic lives in
`tbgw.gui.presentation.format_connection_status()` (host-testable); this
file only creates/updates the LVGL label.

**⚠️ Not verified against lvgl_micropython's exact current API** -- see
`tbgw.gui.lvgl_setup`'s module docstring for the general caveat that
applies to every LVGL-touching file in this package.
"""

from tbgw.gui import theme
from tbgw.gui.presentation import format_connection_status


class StatusBar:
    def __init__(self, lv, parent):
        self._lv = lv
        self.bar = lv.obj(parent)
        self.bar.set_size(lv.pct(100), theme.STATUS_BAR_H)
        self.bar.align(lv.ALIGN.TOP_MID, 0, 0)
        self.bar.set_style_pad_all(4, 0)
        self.bar.set_style_border_width(0, 0)

        self.label = lv.label(self.bar)
        self.label.align(lv.ALIGN.LEFT_MID, 4, 0)
        self.label.set_text("Starting...")

    def refresh(self, wifi_connected, thingsboard_connected,
                can_connected, smart_switch_connected):
        self.label.set_text(format_connection_status(
            wifi_connected, thingsboard_connected,
            can_connected, smart_switch_connected,
        ))
