"""tbgw.gui.widgets.thermostat_tile
======================================

xxx
current temperature, a setpoint slider, a cooling/heating selector, and a
simplified five-state fan row: off, 1, 2, 3 and auto.  The field bus stores
manual/automatic state in a separate register; selecting a fixed speed sends
the required full-manual write before the speed write, while selecting auto
sends full-auto.  That protocol detail is intentionally not exposed as a
third row of controls.

Same "no optimistic update" rule as light_tile.py -- see that module's
docstring for why button/switch state is only ever changed by
`refresh()`, never by a tap handler.
"""

from tbgw.gui import theme
from tbgw.utils.logger import get_logger
from tbgw.gui.presentation import (
    thermostat_display_state,
    build_thermostat_mode_command,
    build_thermostat_fan_control_commands,
    build_thermostat_setpoint_command,
    MODES,
    FAN_SPEEDS,
)

logger = get_logger("gui")

# The CAN register uses half-degree *encoding* (raw 44 is 22.0 C), but the
# xxx
# product constraint exactly: 16, 17, ... 31, never tenths or halves.
_SETPOINT_TICKS_PER_C = 1

# Dedicated thermostat pages have the full width and height of the display.
# Use that space for generous finger targets without affecting controls on
# the lights or settings screens.
_SETPOINT_SLIDER_H = 44
_CONTROL_ROW_H = 140
_CONTROL_BUTTON_H = 132


class ThermostatTile:
    def __init__(self, lv, parent, device, gateway):
        self._lv = lv
        self.device = device
        self._gateway = gateway
        self._min_setpoint = device.subtype.get("minSetpoint", 10.0)
        self._max_setpoint = device.subtype.get("maxSetpoint", 32.0)
        # Do not let the 200-ms refresh overwrite a live drag with the
        # previous CAN value before its command is submitted.
        self._setpoint_dragging = False
        self._setpoint_last_sent_raw = None

        self.card = lv.obj(parent)
        theme.style_card(lv, self.card)

        theme.title_label(lv, self.card, device.display_name)

        self.current_temp_label = lv.label(self.card)
        self.current_temp_label.set_text("Current: --")

        self.setpoint_label = lv.label(self.card)
        self.setpoint_label.set_text("Setpoint: --")

        self.setpoint_slider = lv.slider(self.card)
        self.setpoint_slider.set_size(lv.pct(100), _SETPOINT_SLIDER_H)
        self.setpoint_slider.set_range(
            int(self._min_setpoint * _SETPOINT_TICKS_PER_C),
            int(self._max_setpoint * _SETPOINT_TICKS_PER_C),
        )
        self.setpoint_slider.add_event_cb(self._on_setpoint_pressed, lv.EVENT.PRESSED, None)
        self.setpoint_slider.add_event_cb(self._on_setpoint_value_changed, lv.EVENT.VALUE_CHANGED, None)
        self.setpoint_slider.add_event_cb(self._on_setpoint_released, lv.EVENT.RELEASED, None)
        # A fallback for touch drivers that lose RELEASED at the display edge.
        self.setpoint_slider.add_event_cb(self._on_setpoint_clicked, lv.EVENT.CLICKED, None)
        press_lost = getattr(lv.EVENT, "PRESS_LOST", None)
        if press_lost is not None:
            self.setpoint_slider.add_event_cb(self._on_setpoint_released, press_lost, None)

        # Two selector rows. These used to place buttons at manual
        # x-offsets inside a fixed-width row, which overflowed the card
        # and drew clipped/overlapping buttons. Flex with equal grow lets
        # each row size its own buttons to whatever width is available.
        self._mode_buttons = self._make_button_row(MODES, build_thermostat_mode_command)
        self._fan_buttons = self._make_button_row(FAN_SPEEDS, build_thermostat_fan_control_commands)

    def _make_button_row(self, values, command_builder, labels=None):
        lv = self._lv
        row = lv.obj(self.card)
        theme.style_button_row(lv, row, _CONTROL_ROW_H)
        buttons = {}
        for value in values:
            btn = lv.button(row)
            btn.set_flex_grow(1)
            btn.set_height(_CONTROL_BUTTON_H)
            btn.set_style_pad_all(2, 0)
            label = lv.label(btn)
            label.set_text(labels[value] if labels else value)
            # Selector labels are long relative to a third of a card
            # ("fanHandValveAuto"); a smaller face keeps them inside the
            # button rather than overflowing it.
            face = theme.font(lv, 16)
            if face is not None:
                label.set_style_text_font(face, 0)
            label.center()
            btn.add_event_cb(self._make_button_handler(value, command_builder), lv.EVENT.CLICKED, None)
            buttons[value] = btn
        return buttons

    def _make_button_handler(self, value, command_builder):
        def handler(event):
            # Must not raise -- see light_tile.py's toggle handler for
            # what an escaping exception does to LVGL's input handling.
            try:
                command_or_commands = command_builder(value)
                # Mode selection returns one (method, params) pair.  A fan
                # selection returns a short sequence because fixed fan speeds
                # must also leave full-auto mode before writing the speed.
                if isinstance(command_or_commands[0], str):
                    commands = (command_or_commands,)
                else:
                    commands = command_or_commands
                for method, params in commands:
                    self._gateway.send_local_command(self.device.device_name, method, params)
            except Exception as exc:
                logger.exception("Thermostat command %r failed", value, exc=exc)
        return handler

    def _highlight(self, buttons, selected_value):
        for value, btn in buttons.items():
            if value == selected_value:
                btn.add_state(self._lv.STATE.CHECKED)
            else:
                btn.remove_state(self._lv.STATE.CHECKED)

    def _on_setpoint_pressed(self, event):
        self._setpoint_dragging = True
        self._setpoint_last_sent_raw = None

    def _on_setpoint_value_changed(self, event):
        if self._setpoint_dragging:
            value = self.setpoint_slider.get_value() / _SETPOINT_TICKS_PER_C
            self.setpoint_label.set_text("Setpoint: %.1f C" % value)

    def _on_setpoint_released(self, event):
        self._submit_setpoint()

    def _on_setpoint_clicked(self, event):
        self._submit_setpoint()

    def _submit_setpoint(self):
        """Queue one final command for a completed slider interaction."""
        # Must not raise -- see light_tile.py's toggle handler.
        try:
            raw_value = self.setpoint_slider.get_value()  # whole degrees, as accepted by the switch UI
            if raw_value == self._setpoint_last_sent_raw:
                return
            self._setpoint_last_sent_raw = raw_value
            setpoint_c = raw_value / _SETPOINT_TICKS_PER_C
            method, params = build_thermostat_setpoint_command(setpoint_c, self._min_setpoint, self._max_setpoint)
            self._gateway.send_local_command(self.device.device_name, method, params)
        except Exception as exc:
            logger.exception("Thermostat setpoint change failed", exc=exc)
        finally:
            self._setpoint_dragging = False

    def refresh(self, state):
        display = thermostat_display_state(state)

        if display["currentTempC"] is not None:
            self.current_temp_label.set_text("Current: %.1f C" % display["currentTempC"])

        if display["setpointC"] is not None and not self._setpoint_dragging:
            # LVGL v9: lv_anim_enable_t is a plain bool (LV_ANIM_OFF == false),
            # so there is no lv.ANIM enum to reference here.
            self.setpoint_slider.set_value(
                int(display["setpointC"] * _SETPOINT_TICKS_PER_C), False
            )
            self.setpoint_label.set_text("Setpoint: %.1f C" % display["setpointC"])

        # Show only confirmed CAN state. This includes changes from the
        # physical panel and avoids optimistic button highlights.
        if display["modeActual"] is not None:
            self._highlight(self._mode_buttons, display["modeActual"])
        if display["fanControlActual"] is not None:
            self._highlight(self._fan_buttons, display["fanControlActual"])
