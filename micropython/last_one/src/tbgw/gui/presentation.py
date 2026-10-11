"""tbgw.gui.presentation
==========================

Pure functions mapping device state (as read from `DeviceStateStore`, in
the shape `config/can.json`'s datapoint sets produce -- see
docs/can_connector_design.md) to human-friendly display values, and
mapping user actions back to the `(method, params)` command tuples
`TBGatewayService.send_local_command()` expects. Method names and
`params` keys here are chosen to match `can.json`'s `writes` section
exactly (`setPoleState`, `setMode`, `setFanSpeed`, `setHandAuto`,
`setSetpoint`) -- a locally-issued command and a ThingsBoard-issued RPC
for the same action must resolve to the same downlink mapping.

Deliberately has NO `lvgl`/`lv` import. Every widget file in
`tbgw.gui.widgets` should push as much logic as possible into functions
here rather than inline, specifically so it can be unit-tested on a
desktop host -- there is no practical way to unit-test real LVGL widget
rendering without the native LVGL library available, so this module is
where the actual "is the state->display mapping correct" testing lives.
"""

# ---------------------------------------------------------------------------
# Light -- 6 poles (this project's bench light: 4 local relays + 2 poles
# configurable to drive a remote light -- confirmed with the user; the
# device's own type-register subtype byte reports 6, matching pole count
# exactly). State keys are "pole1".."pole6", per can.json's onOffLight6
# datapoint set.
# ---------------------------------------------------------------------------

DEFAULT_POLE_LABELS = ["Local 1", "Local 2", "Local 3", "Local 4", "Remote 1", "Remote 2"]


def light_pole_states(state, pole_count, pole_labels=None):
    """state: dict from DeviceStateStore.get(device_name), or None if
    nothing has been reported yet. pole_count: device.subtype["poles"].
    Returns a list of `pole_count` dicts: {"index": 1-based pole number,
    "label": str, "on": True/False/None (None = never reported)}.
    """
    pole_labels = pole_labels or DEFAULT_POLE_LABELS
    result = []
    for i in range(1, pole_count + 1):
        label = pole_labels[i - 1] if i - 1 < len(pole_labels) else "Pole %d" % i
        on = None
        if state is not None:
            on = state.get("pole%d" % i)
        result.append({"index": i, "label": label, "on": on})
    return result


def build_light_pole_command(pole_index, new_state):
    """pole_index: 1-based pole number (matches light_pole_states()'s
    "index" field and can.json's "pole1".."pole6" wire fields, and its
    setPoleState write mapping's params shape exactly). Returns
    (method, params) for TBGatewayService.send_local_command().
    """
    return "setPoleState", {"pole": pole_index, "state": bool(new_state)}


# ---------------------------------------------------------------------------
# xxx
# bits are the device's own control loop's ACTUAL values, which can
# legitimately differ in auto modes (confirmed with the user) -- both
# are surfaced, under separate keys, rather than picking one.
# ---------------------------------------------------------------------------

MODES = ("cooling", "heating")
# These are the five choices presented to the user.  The xxx register
# keeps its historical names (low/medium/high), so the conversion to/from
# those wire-facing values is deliberately kept here rather than leaking
# protocol terminology into the UI.
FAN_SPEEDS = ("off", "1", "2", "3", "auto")
_FAN_SPEED_TO_WIRE = {
    "off": "off",
    "1": "low",
    "2": "medium",
    "3": "high",
}
_FAN_SPEED_FROM_WIRE = {
    "off": "off",
    "low": "1",
    "medium": "2",
    "high": "3",
}
HAND_AUTO_MODES = ("fullHand", "fanHandValveAuto", "fullAuto")
HAND_AUTO_LABELS = {
    "fullHand": "Manual",
    "fanHandValveAuto": "Fan manual / valve auto",
    "fullAuto": "Auto",
}


def thermostat_display_state(state):
    """Converts raw wire-level thermostat state (already-decoded enum
    strings and scaled temperatures, per can.json's datapoint set -- no
    further scaling needed here, unlike the earlier byte-offset schema)
    into a flat dict for widgets to render. Any field not yet reported
    is None rather than a guess.
    """
    state = state or {}
    fan_actual = _fan_control_value(
        state.get("fanSpeedActual"), state.get("handAutoActual"),
    )
    fan_target = _fan_control_value(
        state.get("fanSpeedTarget"), state.get("handAutoTarget"),
    )
    return {
        "currentTempC": state.get("currentTemperature"),
        "setpointC": state.get("setpoint"),
        "modeActual": state.get("modeActual"),
        "fanSpeedActual": state.get("fanSpeedActual"),
        "handAutoActual": state.get("handAutoActual"),
        "fanControlActual": fan_actual,
        "modeTarget": state.get("modeTarget"),
        "fanSpeedTarget": state.get("fanSpeedTarget"),
        "handAutoTarget": state.get("handAutoTarget"),
        "fanControlTarget": fan_target,
        "guestMode": state.get("guestMode"),
    }


def _fan_control_value(fan_speed, hand_auto):
    """Return the simplified UI fan choice for decoded device state.

    ``fullAuto`` represents automatic control.
    In every other state a fixed fan register value is shown as off/1/2/3.
    ``fanHandValveAuto`` deliberately has no separate UI choice anymore.
    """
    if hand_auto == "fullAuto":
        return "auto"
    return _FAN_SPEED_FROM_WIRE.get(fan_speed)


def build_thermostat_mode_command(mode_label):
    """mode_label: one of MODES ("cooling"/"heating") -- matches
    can.json's setMode valueExpression dict keys exactly.
    """
    if mode_label not in MODES:
        raise ValueError("Unknown thermostat mode: %r" % mode_label)
    return "setMode", {"mode": mode_label}


def build_thermostat_fan_control_commands(speed_label):
    """Build the CAN commands for one simplified fan-control selection.

    A fixed speed must first put the xxx into full manual mode; otherwise
    a previous full-auto selection remains in charge of the physical fan.
    The auto choice only needs to set the full-auto register.  Returning a
    sequence lets the LVGL tile issue both required, ordered commands while
    keeping each individual CAN write declarative in ``can.json``.
    """
    if speed_label not in FAN_SPEEDS:
        raise ValueError("Unknown fan speed: %r" % speed_label)
    if speed_label == "auto":
        return (("setHandAuto", {"handAuto": "fullAuto"}),)
    return (
        ("setHandAuto", {"handAuto": "fullHand"}),
        ("setFanSpeed", {"fanSpeed": _FAN_SPEED_TO_WIRE[speed_label]}),
    )


def build_thermostat_hand_auto_command(mode_label):
    """mode_label: one of HAND_AUTO_MODES ("fullHand"/"fanHandValveAuto"/
    "fullAuto")."""
    if mode_label not in HAND_AUTO_MODES:
        raise ValueError("Unknown hand/auto mode: %r" % mode_label)
    return "setHandAuto", {"handAuto": mode_label}


def build_thermostat_setpoint_command(setpoint_c, min_setpoint=None, max_setpoint=None):
    """setpoint_c: target temperature in degrees C, as the user set it
    via e.g. an LVGL arc/slider. Clamped to [min_setpoint, max_setpoint]
    (the device's config/home_devices.json subtype bounds) if given, so
    a UI rounding glitch can't send an out-of-range command. can.json's
    own downlink converter handles the half-degree scaling and rounding
    -- this function just passes degrees C through.
    """
    if min_setpoint is not None:
        setpoint_c = max(setpoint_c, min_setpoint)
    if max_setpoint is not None:
        setpoint_c = min(setpoint_c, max_setpoint)
    return "setSetpoint", {"setpoint": setpoint_c}


# ---------------------------------------------------------------------------
# Blind
# ---------------------------------------------------------------------------

def blind_display_state(state):
    """Returns "open", "closed", or None (never reported / unrecognized
    value) -- widgets decide how to render None (e.g. a neutral icon).
    """
    state = state or {}
    position = state.get("position")
    return position if position in ("open", "closed") else None


def build_blind_command(position):
    """position: "open" or "closed"."""
    if position not in ("open", "closed"):
        raise ValueError("Unknown blind position: %r" % position)
    return "setPosition", {"position": position}


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def format_status_summary(uplink_connected, can_connected, mqtt_connected, device_count):
    """Returns a short one-line status string for the GUI's top status bar."""
    parts = [
        "TB:" + ("up" if uplink_connected else "down"),
        "CAN:" + ("up" if can_connected else "down"),
        "MQTT:" + ("up" if mqtt_connected else "down"),
        "%d devices" % device_count,
    ]
    return "  ".join(parts)


def format_connection_status(wifi_connected, thingsboard_connected,
                             can_connected, smart_switch_connected):
    """Compact, three-part header for the production control pages.

    ``CAN/SW`` is OK only when both the local CAN driver is running and
    the configured smart switch has actually been seen on the bus. This
    avoids presenting an idle/unwired bus as a successful switch link.
    """
    can_switch_ok = bool(can_connected and smart_switch_connected)
    return "WiFi:%s  |  TB:%s  |  CAN/SW:%s" % (
        "OK" if wifi_connected else "OFF",
        "OK" if thingsboard_connected else "OFF",
        "OK" if can_switch_ok else "OFF",
    )
