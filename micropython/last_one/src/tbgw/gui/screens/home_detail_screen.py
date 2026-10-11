"""tbgw.gui.screens.home_detail_screen
=========================================

Shows one or more devices belonging to one home as tiles
(light/thermostat/blind, per `tbgw.gui.widgets`). The production navigator
opens one device per screen so its controls can use the full available width;
the optional ``devices`` argument keeps this class reusable for a multi-device
overview if one is needed later.
`refresh_devices()` is called from `gui_app.py`'s poll loop with the
current `DeviceStateStore` snapshot and just forwards each device's state
to its tile's own `refresh()` -- this screen has no state-vs-display logic
of its own (see `tbgw.gui.presentation` for where that lives).

**⚠️ Not verified against lvgl_micropython's exact current API** -- see
`tbgw.gui.lvgl_setup`'s module docstring for the caveat that applies to
every LVGL-touching file in this package.
"""

from tbgw.gui import theme
from tbgw.gui.widgets.status_bar import StatusBar
from tbgw.gui.widgets.light_tile import LightTile
from tbgw.gui.widgets.thermostat_tile import ThermostatTile
from tbgw.gui.widgets.blind_tile import BlindTile

_TILE_CLASSES = {
    "light": LightTile,
    "thermostat": ThermostatTile,
    "blind": BlindTile,
}


class HomeDetailScreen:
    def __init__(self, lv, home, gateway, on_back=None, devices=None, title=None,
                 on_navigate=None, navigate_label=None, on_settings=None):
        self._lv = lv
        self.home = home
        self.devices = list(devices) if devices is not None else list(home.devices)
        self.screen = lv.obj()
        theme.apply_screen_style(lv, self.screen)
        self.status_bar = StatusBar(lv, self.screen)

        # Dedicated device pages replace the old home-list navigation.
        # They expose a direct switch to the other device and Settings in
        # the header, so neither control page needs a tiny shared overview.
        if on_navigate is not None and navigate_label:
            navigate_btn = lv.button(self.screen)
            navigate_btn.set_size(150, theme.TOUCH_MIN)
            navigate_btn.align(lv.ALIGN.TOP_LEFT, 6, theme.STATUS_BAR_H + 4)
            lv.label(navigate_btn).set_text(navigate_label)
            navigate_btn.add_event_cb(lambda event: on_navigate(), lv.EVENT.CLICKED, None)
        elif on_back is not None:
            back_btn = lv.button(self.screen)
            back_btn.set_size(110, theme.TOUCH_MIN)
            back_btn.align(lv.ALIGN.TOP_LEFT, 6, theme.STATUS_BAR_H + 4)
            lv.label(back_btn).set_text("< Back")
            back_btn.add_event_cb(lambda event: on_back(), lv.EVENT.CLICKED, None)

        if on_settings is not None:
            settings_btn = lv.button(self.screen)
            settings_btn.set_size(130, theme.TOUCH_MIN)
            settings_btn.align(lv.ALIGN.TOP_RIGHT, -6, theme.STATUS_BAR_H + 4)
            lv.label(settings_btn).set_text("Settings")
            settings_btn.add_event_cb(lambda event: on_settings(), lv.EVENT.CLICKED, None)

        title_label = theme.title_label(lv, self.screen, title or home.name)
        title_label.align(lv.ALIGN.TOP_MID, 0, theme.STATUS_BAR_H + 10)

        # Tiles are laid out left-to-right and wrap onto a second row.
        # The container is explicitly positioned below the header rather
        # than sized as a percentage: the old version used a percentage
        # height anchored to the bottom, which left most of the screen
        # empty while still clipping tall tiles.
        self._container = lv.obj(self.screen)
        self._container.set_size(theme.SCREEN_W - 8, theme.CONTENT_H)
        self._container.align(lv.ALIGN.TOP_LEFT, 4, theme.CONTENT_TOP)
        self._container.set_style_pad_all(theme.GAP, 0)
        self._container.set_style_pad_row(theme.GAP, 0)
        self._container.set_style_pad_column(theme.GAP, 0)
        self._container.set_flex_flow(lv.FLEX_FLOW.ROW_WRAP)

        self._tiles = {}  # device_name -> tile instance
        single_device_page = len(self.devices) == 1
        for device in self.devices:
            tile_cls = _TILE_CLASSES.get(device.type)
            if tile_cls is None:
                continue  # unknown device type -- skip rather than crash the screen
            tile = tile_cls(lv, self._container, device, gateway)
            if single_device_page:
                # Cards normally use TILE_W so a multi-device overview can
                # wrap them. On a dedicated device page, stretch the one
                # card across the usable screen width: buttons become much
                # easier to hit and labels no longer have to be cramped.
                tile.card.set_size(theme.DETAIL_TILE_W, theme.TILE_H)
            self._tiles[device.device_name] = tile

    def refresh_devices(self, state_snapshot):
        """state_snapshot: DeviceStateStore.snapshot()'s return value
        ({device_name: {values...}}).
        """
        for device_name, tile in self._tiles.items():
            tile.refresh(state_snapshot.get(device_name))

    def refresh_status(self, wifi_connected, thingsboard_connected,
                       can_connected, smart_switch_connected):
        self.status_bar.refresh(
            wifi_connected, thingsboard_connected,
            can_connected, smart_switch_connected,
        )
