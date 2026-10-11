"""tbgw.gui.ch422g
====================

Driver for the CH422G I2C IO-expander that the Waveshare
ESP32-S3-Touch-LCD-5B uses for LCD reset, backlight on/off, SD_CS, and
isolated I/O (see docs/pin_map.md's "CH422G -- register addresses and
EXIO map" section).

All register addresses and EXIO bit assignments below are **confirmed**
against the vendor's own firmware source
(`ESP-IDF/05_IO_Test/lib/CH422G/CH422G.h` and
`08_lvgl_Porting/main/waveshare_rgb_lcd_port.c` from Waveshare's
`ESP32-S3-Touch-LCD-5-Demo.zip`, read directly during Phase 0/5 bring-up)
-- every value that was a best-guess placeholder turned out to be
exactly right. Polarity (backlight active-high, touch reset active-low)
is likewise confirmed and already matches this driver's `set_backlight()`/
`set_touch_reset()` below.

**Important, and easy to miss**: the mode/system register (`_ADDR_MODE`)
must be written with `IO_OE` (bit 0) set to 1 -- output mode -- before
ANY output-register write has an effect; the vendor's own sequence does
this exactly once, first. `tbgw.gui.lvgl_setup.init_display_and_touch()`
calls `set_mode(0x01)` for this; this driver does not do it implicitly,
since it doesn't know when bring-up starts.
"""

from tbgw.utils.logger import get_logger

logger = get_logger("gui")

_ADDR_MODE = 0x24       # system/IO-mode register (bit0 = IO_OE, output enable)
_ADDR_OUTPUT = 0x38     # output register (EXIO0-EXIO7)
_ADDR_INPUT = 0x26      # input register

# EXIO bit assignment -- confirmed against vendor source, see module docstring.
EXIO_LCD_RESET = 3
EXIO_BACKLIGHT = 2
EXIO_SD_CS = 4          # also directly confirmed via Waveshare's Instructions-For-Use page
EXIO_TOUCH_RESET = 1


class CH422GError(Exception):
    pass


class CH422G:
    """`i2c`: any object exposing MicroPython's `machine.I2C` subset used
    here -- `writeto(addr, buf)` and `readfrom(addr, nbytes)`. Passing in
    the bus (rather than owning it) lets `tbgw.gui.lvgl_setup` share one
    I2C bus between this driver and the GT911 touch controller, and lets
    tests pass in a fake/mock bus.
    """

    def __init__(self, i2c, logger_=None):
        self._i2c = i2c
        self._logger = logger_ or logger
        self._output_state = 0x00  # cached byte, all EXIO low/off initially

    def _write_output(self):
        try:
            self._i2c.writeto(_ADDR_OUTPUT, bytes([self._output_state]))
        except Exception as exc:
            raise CH422GError("CH422G output write failed: %s" % exc)

    def set_mode(self, mode_byte):
        """Writes the raw mode/system-configuration register. See this
        module's docstring -- exact bit meanings are CONFIRM pending the
        datasheet; exposed as a raw passthrough so lvgl_setup.py's
        init sequence can be adjusted without changing this driver.
        """
        try:
            self._i2c.writeto(_ADDR_MODE, bytes([mode_byte]))
        except Exception as exc:
            raise CH422GError("CH422G mode write failed: %s" % exc)

    def set_exio(self, index, value):
        """Sets EXIO<index> (0-7) high (True) or low (False) and writes
        the whole output byte (this chip family has no per-bit write --
        the full byte must be resent each time).
        """
        if not 0 <= index <= 7:
            raise ValueError("EXIO index must be 0-7, got %r" % index)
        if value:
            self._output_state |= (1 << index)
        else:
            self._output_state &= ~(1 << index) & 0xFF
        self._write_output()

    def get_exio(self, index):
        """Reads the cached output state for an EXIO configured as an
        output. For a true input read (isolated I/O configured as input),
        use read_inputs() instead.
        """
        if not 0 <= index <= 7:
            raise ValueError("EXIO index must be 0-7, got %r" % index)
        return bool(self._output_state & (1 << index))

    def read_inputs(self):
        """Reads the input register byte (isolated I/O configured as
        inputs). Returns an int (bit N = EXIO N's current level).
        """
        try:
            data = self._i2c.readfrom(_ADDR_INPUT, 1)
            return data[0]
        except Exception as exc:
            raise CH422GError("CH422G input read failed: %s" % exc)

    # -- high-level convenience methods, matching the board's documented uses --

    def set_lcd_reset(self, asserted):
        """asserted=True holds the LCD panel in reset; False releases it.
        Active-low, confirmed against vendor source. **The vendor's own
        init sequence never actually pulses this line -- it holds it
        deasserted (released) throughout bring-up** (see
        docs/pin_map.md); `lvgl_setup.py` mirrors that rather than
        pulsing it. Kept as an assert/deassert primitive here in case a
        real pulse ever proves necessary.
        """
        self.set_exio(EXIO_LCD_RESET, not asserted)

    def set_backlight(self, on):
        """Active-high, confirmed against vendor source."""
        self.set_exio(EXIO_BACKLIGHT, on)

    def set_touch_reset(self, asserted):
        """Active-low, confirmed against vendor source. Vendor sequence:
        assert, hold 100ms, deassert, then wait a further 200ms before
        using the touch controller -- see lvgl_setup.py.
        """
        self.set_exio(EXIO_TOUCH_RESET, not asserted)

    def set_sd_cs(self, value):
        self.set_exio(EXIO_SD_CS, value)
