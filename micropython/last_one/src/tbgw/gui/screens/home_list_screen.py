"""tbgw.gui.screens.home_list_screen
=======================================

Top-level screen: one button per configured device, plus a "Settings"
button. Tapping a device opens its own full-width control page; this keeps
the six-light controls and the thermostat controls finger-sized even when
they belong to the same ThingsBoard Asset/home.

**⚠️ Not verified against lvgl_micropython's exact current API** -- see
`tbgw.gui.lvgl_setup`'s module docstring for the caveat that applies to
every LVGL-touching file in this package.
"""

from tbgw.gui import theme
from tbgw.gui.widgets.status_bar import StatusBar


class HomeListScreen:
    def __init__(self, lv, catalog, on_device_selected, on_settings_selected):
        self._lv = lv
        self.screen = lv.obj()
        theme.apply_screen_style(lv, self.screen)
        self.status_bar = StatusBar(lv, self.screen)

        list_widget = lv.list(self.screen)
        list_widget.set_size(theme.SCREEN_W - 40, theme.CONTENT_H - theme.TOUCH_MIN - theme.GAP)
        list_widget.align(lv.ALIGN.TOP_MID, 0, theme.CONTENT_TOP - theme.HEADER_H + 8)

        for home in catalog.homes:
            for device in home.devices:
                btn = list_widget.add_button(None, device.display_name)
                btn.set_height(theme.TOUCH_MIN + 12)
                btn.add_event_cb(
                    self._make_handler(device.device_name, on_device_selected),
                    lv.EVENT.CLICKED,
                    None,
                )

        settings_btn = lv.button(self.screen)
        settings_btn.set_size(180, theme.TOUCH_MIN)
        settings_btn.align(lv.ALIGN.BOTTOM_MID, 0, -10)
        lv.label(settings_btn).set_text("Settings")
        settings_btn.add_event_cb(lambda event: on_settings_selected(), lv.EVENT.CLICKED, None)

    def _make_handler(self, device_name, on_device_selected):
        def handler(event):
            on_device_selected(device_name)
        return handler

    def refresh_status(self, uplink_connected, can_connected, mqtt_connected, device_count):
        self.status_bar.refresh(uplink_connected, can_connected, mqtt_connected, device_count)
