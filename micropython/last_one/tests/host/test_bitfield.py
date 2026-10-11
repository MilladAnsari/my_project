import unittest

from tbgw.utils.bitfield import extract, insert, BitfieldError


class TestExtract(unittest.TestCase):
    def test_whole_byte_unsigned(self):
        self.assertEqual(extract(b"\x1d", {"bit": 0, "width": 8}), 29)

    def test_scale_round_trip(self):
        # generic example
        value = extract(b"\x2c", {"bit": 0, "width": 8, "scale": 0.5})
        self.assertEqual(value, 22.0)

    def test_bool_type(self):
        self.assertIs(extract(b"\x01", {"bit": 0, "width": 1, "type": "bool"}), True)
        self.assertIs(extract(b"\x00", {"bit": 0, "width": 1, "type": "bool"}), False)

    def test_multiple_fields_in_one_byte(self):
        # 0x24 = 0b00100100 -> bits[0:2]=00(0), bits[2:2..4)=01(1), bits[4:6)=10(2)
        data = b"\x24"
        self.assertEqual(extract(data, {"bit": 0, "width": 2}), 0)
        self.assertEqual(extract(data, {"bit": 2, "width": 2}), 1)
        self.assertEqual(extract(data, {"bit": 4, "width": 2}), 2)

    def test_enum_mapping(self):
        field = {"bit": 0, "width": 2, "enum": {"0": "off", "1": "cooling", "2": "heating"}}
        self.assertEqual(extract(b"\x01", field), "cooling")

    def test_enum_unmapped_value_falls_back_to_raw(self):
        field = {"bit": 0, "width": 2, "enum": {"0": "off"}}
        self.assertEqual(extract(b"\x03", field), 3)

    def test_signed(self):
        # 0xFF as an 8-bit signed field is -1
        self.assertEqual(extract(b"\xff", {"bit": 0, "width": 8, "signed": True}), -1)
        self.assertEqual(extract(b"\x7f", {"bit": 0, "width": 8, "signed": True}), 127)

    def test_offset(self):
        self.assertEqual(extract(b"\x0a", {"bit": 0, "width": 8, "offset": -40}), -30)

    def test_multi_byte_payload_big_endian(self):
        # 0x0102 as a 16-bit field
        self.assertEqual(extract(b"\x01\x02", {"bit": 0, "width": 16}), 0x0102)

    def test_out_of_range_raises(self):
        with self.assertRaises(BitfieldError):
            extract(b"\x00", {"bit": 4, "width": 8})

    def test_negative_bit_raises(self):
        with self.assertRaises(BitfieldError):
            extract(b"\x00", {"bit": -1, "width": 1})


class TestInsert(unittest.TestCase):
    def test_round_trip_unsigned(self):
        byte = insert(0x00, {"bit": 0, "width": 8}, 29)
        self.assertEqual(byte, 29)
        self.assertEqual(extract(bytes([byte]), {"bit": 0, "width": 8}), 29)

    def test_preserves_other_bits(self):
        # set bits[2:4) to 3 (0b11) in a byte that already has bit 0 set
        byte = insert(0b00000001, {"bit": 2, "width": 2}, 3)
        self.assertEqual(byte, 0b00001101)

    def test_bool(self):
        self.assertEqual(insert(0x00, {"bit": 0, "width": 1, "type": "bool"}, True), 1)
        self.assertEqual(insert(0x01, {"bit": 0, "width": 1, "type": "bool"}, False), 0)

    def test_scale_round_trip(self):
        byte = insert(0x00, {"bit": 0, "width": 8, "scale": 0.5}, 22.0)
        self.assertEqual(byte, 44)

    def test_enum_reverse_lookup(self):
        field = {"bit": 0, "width": 2, "enum": {"0": "off", "1": "cooling", "2": "heating"}}
        self.assertEqual(insert(0x00, field, "heating"), 2)

    def test_multi_byte_field_rejected(self):
        with self.assertRaises(BitfieldError):
            insert(0x00, {"bit": 4, "width": 8}, 1)

    def test_out_of_range_value_raises(self):
        with self.assertRaises(BitfieldError):
            insert(0x00, {"bit": 0, "width": 2}, 7)


if __name__ == "__main__":
    unittest.main()
