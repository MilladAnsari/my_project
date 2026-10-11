"""Static, credential-safe connection summary for the local GUI."""

from tbgw.gui import theme


class SettingsScreen:
    def __init__(self, lv, on_back, wifi_ssid=None,
                 thingsboard_host=None, thingsboard_port=None,
                 on_reconnect=None):
        self._lv = lv
        self.screen = lv.obj()
        theme.apply_screen_style(lv, self.screen)

        back_btn = lv.button(self.screen)
        back_btn.set_size(110, theme.TOUCH_MIN)
        back_btn.align(lv.ALIGN.TOP_LEFT, 6, theme.STATUS_BAR_H + 4)
        lv.label(back_btn).set_text("< Thermostat")
        back_btn.add_event_cb(lambda event: on_back(), lv.EVENT.CLICKED, None)

        title = theme.title_label(lv, self.screen, "Settings")
        title.align(lv.ALIGN.TOP_MID, 0, theme.STATUS_BAR_H + 10)

        info_label = lv.label(self.screen)
        info_label.align(lv.ALIGN.TOP_LEFT, theme.PAD, theme.CONTENT_TOP + theme.GAP)
        info_label.set_text(
            "WiFi SSID: %s\n\nThingsBoard IP: %s\n\nThingsBoard port: %s" % (
                wifi_ssid or "Not configured",
                thingsboard_host or "Not configured",
                thingsboard_port if thingsboard_port is not None else "Not configured",
            )
        )

        if on_reconnect is not None:
            reconnect_btn = lv.button(self.screen)
            reconnect_btn.set_size(260, theme.TOUCH_MIN)
            reconnect_btn.align(
                lv.ALIGN.TOP_LEFT, theme.PAD,
                theme.CONTENT_TOP + theme.GAP + 150,
            )
            lv.label(reconnect_btn).set_text("Reconnect ThingsBoard")
            reconnect_btn.add_event_cb(
                lambda event: on_reconnect(), lv.EVENT.CLICKED, None
            )
