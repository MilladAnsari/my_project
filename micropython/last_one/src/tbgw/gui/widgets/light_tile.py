"""tbgw.gui.widgets.light_tile
================================

Renders one "light" device as a card with one on/off switch per pole
(count and labels from the device's catalog subtype, see
`tbgw.gui.device_catalog`). Follows the "no optimistic update" rule
throughout this package: tapping a switch sends a command and does NOT
keep its own visual state -- `refresh()` is the only thing that's
allowed to change what a switch shows, and it's only ever called with
real state from `DeviceStateStore` (see docs/architecture.md).

**LVGL's `lv.switch` toggles its own CHECKED state internally BEFORE
firing `EVENT.VALUE_CHANGED`** -- there is no way to intercept a tap and
decide the visual state first. The toggle handler below reads that
already-flipped state to know what the user asked for, sends the
command, then immediately reverts the switch back to the last
CONFIRMED state (tracked in `self._last_known`, updated only by
`refresh()`) -- so the switch visibly "bounces back" and stays there
until a real event/reply moves it forward. Earlier versions of this
file skipped the revert step, which meant tapping a switch changed what
it displayed immediately, in direct violation of the rule stated above.
"""

from tbgw.gui import theme
from tbgw.gui.presentation import light_pole_states, build_light_pole_command
from tbgw.utils.logger import get_logger

logger = get_logger("gui")

# Six light poles share one dedicated page.  This is the tallest row that
# keeps all six controls visible while making each switch easy to hit.
_LIGHT_ROW_H = 62
_LIGHT_SWITCH_W = 98
_LIGHT_SWITCH_H = 58


class LightTile:
    def __init__(self, lv, parent, device, gateway):
        self._lv = lv
        self.device = device
        self._gateway = gateway
        pole_count = device.subtype.get("poles", 1)
        pole_labels = device.subtype.get("poleLabels")

        self.card = lv.obj(parent)
        theme.style_card(lv, self.card)

        theme.title_label(lv, self.card, device.display_name)

        self._pole_switches = {}
        # Last CONFIRMED on/off per pole (None = never reported) --
        # the only state a toggle handler is allowed to revert to.
        self._last_known = {}
        for pole in light_pole_states(None, pole_count, pole_labels):
            # One flex row per pole: label on the left, switch on the
            # right. Previously these were absolutely positioned, which
            # truncated the longer labels ("Remote 1"/"Remote 2") once
            # the switch was placed over them.
            row = lv.obj(self.card)
            theme.style_row(lv, row, _LIGHT_ROW_H)

            label = lv.label(row)
            label.set_text(pole["label"])
            label.set_flex_grow(1)

            switch = lv.switch(row)
            switch.set_size(_LIGHT_SWITCH_W, _LIGHT_SWITCH_H)
            switch.add_event_cb(self._make_toggle_handler(pole["index"]), lv.EVENT.VALUE_CHANGED, None)
            self._pole_switches[pole["index"]] = switch
            self._last_known[pole["index"]] = None

    def _make_toggle_handler(self, pole_index):
        def handler(event):
            # NOTHING may escape this function. It is called by LVGL from
            # inside its C event dispatch; an exception that propagates
            # out aborts that dispatch mid-gesture and leaves the input
            # device stuck in a permanently "pressed" state, so the panel
            # never registers another touch for the rest of the boot.
            # That failure is deeply misleading in the field -- the screen
            # keeps rendering and keeps showing live CAN updates, so it
            # looks like a frozen UI rather than a dead input path.
            try:
                # Use the switch we own rather than event.get_target():
                # the target comes back as a generic object whose exact
                # binding shape varies between LVGL versions, and we
                # already know which switch this handler belongs to.
                switch = self._pole_switches.get(pole_index)
                if switch is None:
                    return
                # LVGL already flipped the switch's own CHECKED state by
                # the time this fires -- that flipped value is what the
                # user asked for, not what's confirmed.
                requested_state = switch.has_state(self._lv.STATE.CHECKED)
                method, params = build_light_pole_command(pole_index, requested_state)
                self._gateway.send_local_command(self.device.device_name, method, params)
                self._apply_switch_state(switch, self._last_known.get(pole_index))
            except Exception as exc:
                logger.exception("Light pole %s toggle failed", pole_index, exc=exc)
        return handler

    def _apply_switch_state(self, switch, on):
        if on:
            switch.add_state(self._lv.STATE.CHECKED)
        else:
            switch.remove_state(self._lv.STATE.CHECKED)

    def refresh(self, state):
        """state: DeviceStateStore.get(device.device_name)'s return value
        (a dict, or None if nothing has been reported yet). Called from
        gui_app.py's poll loop -- never call this from a button handler.
        The only place `self._last_known` is updated, and therefore the
        only place a switch's visual state is allowed to actually change.
        """
        pole_count = self.device.subtype.get("poles", 1)
        pole_labels = self.device.subtype.get("poleLabels")
        for pole in light_pole_states(state, pole_count, pole_labels):
            if pole["on"] is None:
                continue  # never reported -- leave the switch as-is rather than guessing
            self._last_known[pole["index"]] = pole["on"]
            switch = self._pole_switches.get(pole["index"])
            if switch is None:
                continue
            self._apply_switch_state(switch, pole["on"])
