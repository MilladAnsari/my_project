import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.utils.tb_utility import (  # noqa: E402
    generate_id,
    pack_value,
    unpack_value,
    render_template,
    get_nested,
    topic_matches,
)


class TestGenerateId(unittest.TestCase):
    def test_length_and_charset(self):
        for _ in range(20):
            value = generate_id(16)
            self.assertEqual(len(value), 16)
            self.assertTrue(all(c in "0123456789abcdef" for c in value))

    def test_ids_differ(self):
        self.assertNotEqual(generate_id(16), generate_id(16))


class TestPackUnpackValue(unittest.TestCase):
    def test_float_roundtrip(self):
        data = pack_value(21.5, "float")
        self.assertEqual(len(data), 4)
        self.assertAlmostEqual(unpack_value(data, 0, "float"), 21.5, places=3)

    def test_double_roundtrip(self):
        data = pack_value(3.14159265, "double")
        self.assertEqual(len(data), 8)
        self.assertAlmostEqual(unpack_value(data, 0, "double"), 3.14159265, places=6)

    def test_signed_int_roundtrip(self):
        data = pack_value(-42, "int", signed=True, size=2)
        self.assertEqual(unpack_value(data, 0, "int", signed=True, size=2), -42)

    def test_unsigned_int_roundtrip(self):
        data = pack_value(200, "int", signed=False, size=1)
        self.assertEqual(unpack_value(data, 0, "int", signed=False, size=1), 200)

    def test_bool_roundtrip(self):
        data = pack_value(True, "bool")
        self.assertEqual(unpack_value(data, 0, "bool"), True)
        data = pack_value(False, "bool")
        self.assertEqual(unpack_value(data, 0, "bool"), False)

    def test_string_roundtrip_padded(self):
        data = pack_value("hi", "string", encoding="ascii", size=8)
        self.assertEqual(len(data), 8)
        self.assertEqual(unpack_value(data, 0, "string", encoding="ascii", size=8), "hi")

    def test_offset_extraction_from_larger_frame(self):
        # Simulates a CAN frame: [coolantTemp:1][rpm:2][voltage:4]
        frame = pack_value(85, "int", signed=False, size=1)
        frame += pack_value(3200, "int", signed=False, size=2)
        frame += pack_value(12.6, "float")
        self.assertEqual(unpack_value(frame, 0, "int", signed=False, size=1), 85)
        self.assertEqual(unpack_value(frame, 1, "int", signed=False, size=2), 3200)
        self.assertAlmostEqual(unpack_value(frame, 3, "float"), 12.6, places=3)

    def test_little_endian(self):
        data = pack_value(1, "int", signed=False, size=2, byteorder="little")
        self.assertEqual(data, b"\x01\x00")
        self.assertEqual(
            unpack_value(data, 0, "int", signed=False, size=2, byteorder="little"), 1
        )


class TestTemplates(unittest.TestCase):
    def test_whole_expression_placeholder_preserves_type(self):
        self.assertEqual(render_template("${count}", {"count": 42}), 42)

    def test_embedded_placeholder_stringifies(self):
        self.assertEqual(
            render_template("Device ${id} online", {"id": "A1"}), "Device A1 online"
        )

    def test_multiple_placeholders(self):
        self.assertEqual(
            render_template("${a}-${b}", {"a": "x", "b": "y"}), "x-y"
        )

    def test_unresolvable_placeholder_left_verbatim(self):
        self.assertEqual(render_template("${missing}", {}), "${missing}")

    def test_get_nested_dotted_path(self):
        payload = {"sensor": {"id": "S1", "reading": {"temp": 21.5}}}
        self.assertEqual(get_nested(payload, "sensor.id"), "S1")
        self.assertEqual(get_nested(payload, "sensor.reading.temp"), 21.5)
        self.assertIsNone(get_nested(payload, "sensor.missing"))


class TestTopicMatches(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(topic_matches("sensor/data", "sensor/data"))

    def test_single_level_wildcard(self):
        self.assertTrue(topic_matches("sensor/17/data", "sensor/+/data"))
        self.assertFalse(topic_matches("sensor/17/18/data", "sensor/+/data"))

    def test_multi_level_wildcard(self):
        self.assertTrue(topic_matches("sensor/17/data", "sensor/#"))
        self.assertTrue(topic_matches("sensor", "sensor/#"))

    def test_no_match(self):
        self.assertFalse(topic_matches("other/data", "sensor/+/data"))

    def test_too_short_topic(self):
        self.assertFalse(topic_matches("sensor", "sensor/+/data"))


class TestUnixTimeMs(unittest.TestCase):
    def test_returns_int_milliseconds(self):
        from tbgw.utils.tb_utility import unix_time_ms

        ms = unix_time_ms()
        self.assertIsInstance(ms, int)
        self.assertGreater(ms, 0)

    def test_no_offset_applied_when_epoch_is_already_1970(self):
        from tbgw.utils import tb_utility
        import time

        original = tb_utility._epoch_is_2000
        tb_utility._epoch_is_2000 = False
        try:
            ms = tb_utility.unix_time_ms()
            self.assertAlmostEqual(ms / 1000.0, time.time(), delta=2)
        finally:
            tb_utility._epoch_is_2000 = original

    def test_offset_applied_when_epoch_is_2000(self):
        from tbgw.utils import tb_utility
        import time

        original = tb_utility._epoch_is_2000
        tb_utility._epoch_is_2000 = True
        try:
            ms = tb_utility.unix_time_ms()
            expected = (time.time() + tb_utility._MICROPYTHON_EPOCH_OFFSET_S) * 1000
            self.assertAlmostEqual(ms, expected, delta=2000)
        finally:
            tb_utility._epoch_is_2000 = original


if __name__ == "__main__":
    unittest.main()
