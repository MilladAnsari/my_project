"""tbgw.gui.widgets.blind_tile
================================

Renders one "blind" device: current position label + open/close buttons.
Same "no optimistic update" rule as light_tile.py -- see that module's
docstring.

**⚠️ Not verified against lvgl_micropython's exact current API** -- see
`tbgw.gui.lvgl_setup`'s module docstring for the caveat that applies to
every LVGL-touching file in this package.
"""

from tbgw.gui import theme
from tbgw.utils.logger import get_logger
from tbgw.gui.presentation import blind_display_state, build_blind_command

logger = get_logger("gui")


class BlindTile:
    def __init__(self, lv, parent, device, gateway):
        self._lv = lv
        self.device = device
        self._gateway = gateway

        self.card = lv.obj(parent)
        theme.style_card(lv, self.card)

        theme.title_label(lv, self.card, device.display_name)

        self.position_label = lv.label(self.card)
        self.position_label.set_text("Position: unknown")

        # Open/Close shared a fixed-width row before and overlapped each
        # other once card padding was taken into account; flex-grow lets
        # them split whatever width the card actually has.
        row = lv.obj(self.card)
        theme.style_button_row(lv, row)

        self.open_btn = lv.button(row)
        self.open_btn.set_flex_grow(1)
        self.open_btn.set_height(theme.TOUCH_MIN)
        lv.label(self.open_btn).set_text("Open")
        self.open_btn.add_event_cb(self._make_handler("open"), lv.EVENT.CLICKED, None)

        self.close_btn = lv.button(row)
        self.close_btn.set_flex_grow(1)
        self.close_btn.set_height(theme.TOUCH_MIN)
        lv.label(self.close_btn).set_text("Close")
        self.close_btn.add_event_cb(self._make_handler("closed"), lv.EVENT.CLICKED, None)

    def _make_handler(self, position):
        def handler(event):
            # Must not raise -- see light_tile.py's toggle handler for what
            # an escaping exception does to LVGL's input handling.
            try:
                method, params = build_blind_command(position)
                self._gateway.send_local_command(self.device.device_name, method, params)
            except Exception as exc:
                logger.exception("Blind command %r failed", position, exc=exc)
        return handler

    def refresh(self, state):
        position = blind_display_state(state)
        if position is not None:
            self.position_label.set_text("Position: %s" % position)
