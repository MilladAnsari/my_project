import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.gui import presentation as p  # noqa: E402


class TestLightPresentation(unittest.TestCase):
    def test_pole_states_from_none_state_all_unknown(self):
        poles = p.light_pole_states(None, pole_count=6)
        self.assertEqual(len(poles), 6)
        self.assertTrue(all(k["on"] is None for k in poles))

    def test_pole_states_from_partial_state(self):
        state = {"pole1": True, "pole3": False}
        poles = p.light_pole_states(state, pole_count=6)
        by_index = {k["index"]: k["on"] for k in poles}
        self.assertEqual(by_index, {1: True, 2: None, 3: False, 4: None, 5: None, 6: None})

    def test_pole_labels_used_when_given(self):
        poles = p.light_pole_states(None, pole_count=2, pole_labels=["Ceiling", "Lamp"])
        self.assertEqual([k["label"] for k in poles], ["Ceiling", "Lamp"])

    def test_default_labels_when_not_given(self):
        poles = p.light_pole_states(None, pole_count=2)
        self.assertEqual([k["label"] for k in poles], p.DEFAULT_POLE_LABELS[:2])

    def test_default_labels_fall_back_to_generic_beyond_the_default_list(self):
        poles = p.light_pole_states(None, pole_count=7)
        self.assertEqual(poles[6]["label"], "Pole 7")

    def test_build_light_pole_command(self):
        method, params = p.build_light_pole_command(3, True)
        self.assertEqual(method, "setPoleState")
        self.assertEqual(params, {"pole": 3, "state": True})

    def test_build_light_pole_command_coerces_state_to_bool(self):
        method, params = p.build_light_pole_command(1, 1)
        self.assertIs(params["state"], True)


class TestThermostatPresentation(unittest.TestCase):
    def test_display_state_from_none(self):
        display = p.thermostat_display_state(None)
        self.assertIsNone(display["modeActual"])
        self.assertIsNone(display["fanSpeedActual"])
        self.assertIsNone(display["handAutoActual"])
        self.assertIsNone(display["setpointC"])
        self.assertIsNone(display["currentTempC"])

    def test_display_state_passes_decoded_values_through(self):
        # can.json's uplink converter already decodes enums/scales values
        # -- presentation.py does no further conversion, unlike the old
        # byte-offset schema this replaces.
        state = {
            "modeActual": "cooling", "fanSpeedActual": "medium", "handAutoActual": "fullAuto",
            "modeTarget": "heating", "fanSpeedTarget": "low", "handAutoTarget": "fullHand",
            "setpoint": 22.5, "currentTemperature": 21, "guestMode": "comfort",
        }
        display = p.thermostat_display_state(state)
        self.assertEqual(display["modeActual"], "cooling")
        self.assertEqual(display["fanSpeedActual"], "medium")
        self.assertEqual(display["handAutoActual"], "fullAuto")
        self.assertEqual(display["fanControlActual"], "auto")
        self.assertEqual(display["modeTarget"], "heating")
        self.assertEqual(display["fanSpeedTarget"], "low")
        self.assertEqual(display["handAutoTarget"], "fullHand")
        self.assertEqual(display["fanControlTarget"], "1")
        self.assertEqual(display["setpointC"], 22.5)
        self.assertEqual(display["currentTempC"], 21)
        self.assertEqual(display["guestMode"], "comfort")

    def test_build_mode_command(self):
        method, params = p.build_thermostat_mode_command("cooling")
        self.assertEqual(method, "setMode")
        self.assertEqual(params, {"mode": "cooling"})

    def test_build_mode_command_invalid_raises(self):
        with self.assertRaises(ValueError):
            p.build_thermostat_mode_command("off")

    def test_build_fixed_fan_speed_commands_leaves_auto_then_sets_speed(self):
        commands = p.build_thermostat_fan_control_commands("3")
        self.assertEqual(commands, (
            ("setHandAuto", {"handAuto": "fullHand"}),
            ("setFanSpeed", {"fanSpeed": "high"}),
        ))

    def test_build_auto_fan_control_command(self):
        commands = p.build_thermostat_fan_control_commands("auto")
        self.assertEqual(commands, (("setHandAuto", {"handAuto": "fullAuto"}),))

    def test_build_fan_control_command_invalid_raises(self):
        with self.assertRaises(ValueError):
            p.build_thermostat_fan_control_commands("ludicrous")

    def test_build_hand_auto_command(self):
        method, params = p.build_thermostat_hand_auto_command("fullAuto")
        self.assertEqual(method, "setHandAuto")
        self.assertEqual(params, {"handAuto": "fullAuto"})

    def test_build_hand_auto_command_invalid_raises(self):
        with self.assertRaises(ValueError):
            p.build_thermostat_hand_auto_command("turbo")

    def test_build_setpoint_command_passes_through(self):
        method, params = p.build_thermostat_setpoint_command(23.5)
        self.assertEqual(method, "setSetpoint")
        self.assertEqual(params, {"setpoint": 23.5})

    def test_build_setpoint_command_clamped_to_bounds(self):
        _, params = p.build_thermostat_setpoint_command(50, min_setpoint=10, max_setpoint=32)
        self.assertEqual(params["setpoint"], 32)
        _, params = p.build_thermostat_setpoint_command(-5, min_setpoint=10, max_setpoint=32)
        self.assertEqual(params["setpoint"], 10)

    def test_build_setpoint_command_within_bounds_unaffected(self):
        _, params = p.build_thermostat_setpoint_command(22, min_setpoint=10, max_setpoint=32)
        self.assertEqual(params["setpoint"], 22)


class TestBlindPresentation(unittest.TestCase):
    def test_display_state_open(self):
        self.assertEqual(p.blind_display_state({"position": "open"}), "open")

    def test_display_state_closed(self):
        self.assertEqual(p.blind_display_state({"position": "closed"}), "closed")

    def test_display_state_none(self):
        self.assertIsNone(p.blind_display_state(None))

    def test_display_state_unrecognized_value(self):
        self.assertIsNone(p.blind_display_state({"position": "half-open"}))

    def test_build_blind_command(self):
        method, params = p.build_blind_command("open")
        self.assertEqual(method, "setPosition")
        self.assertEqual(params, {"position": "open"})

    def test_build_blind_command_invalid_raises(self):
        with self.assertRaises(ValueError):
            p.build_blind_command("sideways")


class TestStatusSummary(unittest.TestCase):
    def test_all_up(self):
        summary = p.format_status_summary(True, True, True, 5)
        self.assertIn("TB:up", summary)
        self.assertIn("CAN:up", summary)
        self.assertIn("MQTT:up", summary)
        self.assertIn("5 devices", summary)

    def test_mixed_states(self):
        summary = p.format_status_summary(True, False, True, 0)
        self.assertIn("CAN:down", summary)
        self.assertIn("0 devices", summary)

    def test_connection_header_requires_switch_confirmation(self):
        summary = p.format_connection_status(True, True, True, False)
        self.assertIn("WiFi:OK", summary)
        self.assertIn("TB:OK", summary)
        self.assertIn("CAN/SW:OFF", summary)

        summary = p.format_connection_status(True, True, True, True)
        self.assertIn("CAN/SW:OK", summary)


if __name__ == "__main__":
    unittest.main()
