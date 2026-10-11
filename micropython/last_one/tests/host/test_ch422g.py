import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.gui.ch422g import CH422G, CH422GError, EXIO_SD_CS  # noqa: E402
from tbgw.utils.logger import get_logger  # noqa: E402


class FakeI2C:
    def __init__(self):
        self.writes = []  # list of (addr, bytes)
        self.read_response = b"\x00"
        self.fail_writes = False
        self.fail_reads = False

    def writeto(self, addr, buf):
        if self.fail_writes:
            raise OSError("simulated I2C failure")
        self.writes.append((addr, bytes(buf)))

    def readfrom(self, addr, n):
        if self.fail_reads:
            raise OSError("simulated I2C failure")
        return self.read_response


class TestCH422G(unittest.TestCase):
    def setUp(self):
        self.i2c = FakeI2C()
        self.ch422g = CH422G(self.i2c, logger_=get_logger("test"))

    def test_set_exio_high_writes_correct_bit(self):
        self.ch422g.set_exio(3, True)
        addr, data = self.i2c.writes[-1]
        self.assertEqual(data[0] & (1 << 3), 1 << 3)

    def test_set_exio_low_clears_bit(self):
        self.ch422g.set_exio(3, True)
        self.ch422g.set_exio(3, False)
        addr, data = self.i2c.writes[-1]
        self.assertEqual(data[0] & (1 << 3), 0)

    def test_multiple_exio_bits_independent(self):
        self.ch422g.set_exio(0, True)
        self.ch422g.set_exio(2, True)
        addr, data = self.i2c.writes[-1]
        self.assertEqual(data[0], 0b00000101)

    def test_get_exio_reflects_cached_state(self):
        self.ch422g.set_exio(5, True)
        self.assertTrue(self.ch422g.get_exio(5))
        self.ch422g.set_exio(5, False)
        self.assertFalse(self.ch422g.get_exio(5))

    def test_set_exio_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            self.ch422g.set_exio(8, True)
        with self.assertRaises(ValueError):
            self.ch422g.set_exio(-1, True)

    def test_write_failure_raises_ch422g_error(self):
        self.i2c.fail_writes = True
        with self.assertRaises(CH422GError):
            self.ch422g.set_exio(1, True)

    def test_read_inputs_returns_byte(self):
        self.i2c.read_response = b"\x2a"
        self.assertEqual(self.ch422g.read_inputs(), 0x2A)

    def test_read_failure_raises_ch422g_error(self):
        self.i2c.fail_reads = True
        with self.assertRaises(CH422GError):
            self.ch422g.read_inputs()

    def test_set_backlight_on_off(self):
        self.ch422g.set_backlight(True)
        self.assertTrue(self.ch422g.get_exio(2))  # EXIO_BACKLIGHT placeholder index
        self.ch422g.set_backlight(False)
        self.assertFalse(self.ch422g.get_exio(2))

    def test_set_sd_cs_uses_documented_exio4(self):
        self.ch422g.set_sd_cs(True)
        self.assertTrue(self.ch422g.get_exio(EXIO_SD_CS))
        self.assertEqual(EXIO_SD_CS, 4)  # confirmed by Waveshare's Instructions-For-Use page

    def test_set_lcd_reset_asserted_then_released(self):
        self.ch422g.set_lcd_reset(True)
        reset_state_asserted = self.ch422g.get_exio(3)
        self.ch422g.set_lcd_reset(False)
        reset_state_released = self.ch422g.get_exio(3)
        self.assertNotEqual(reset_state_asserted, reset_state_released)

    def test_set_mode_writes_raw_byte(self):
        self.ch422g.set_mode(0x01)
        addr, data = self.i2c.writes[-1]
        self.assertEqual(data, b"\x01")


if __name__ == "__main__":
    unittest.main()
