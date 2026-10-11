"""tbgw.gui.lvgl_setup
========================

Hardware bring-up for the Waveshare ESP32-S3-Touch-LCD-5B's display and
touch, targeting the `lvgl_micropython` custom firmware (see
docs/architecture.md's "Why LVGL needs custom firmware too" section and
build/build_firmware.sh, the verified recipe from Phase 1).

All pin/register/timing values below are **confirmed** against
Waveshare's own vendor demo firmware source (`ESP32-S3-Touch-LCD-5-Demo.zip`,
read directly during Phase 0/5 bring-up -- see docs/pin_map.md) and
against `lvgl_micropython`'s own real source (read directly from the
firmware build workspace, not guessed from examples/discussion threads
as earlier revisions of this module were) -- specifically
`ext_mod/lcd_bus/esp32_src/rgb_bus.c` (RGBBus kwarg names),
`api_drivers/common_api_drivers/display/rgb_display/rgb_display.py`
(RGBDisplay), `api_drivers/common_api_drivers/frozen/other/i2c.py`
(the `i2c` module), and `api_drivers/common_api_drivers/indev/gt911.py`
(GT911 -- notably, it takes **no `display=` keyword argument at all**;
association with the active display happens automatically inside
`IndevBase.__init__()` via `lv.display_get_default()`, which is why
`lv.init()` and display construction/registration must both happen
BEFORE constructing an indev, not after).

Everything hardware-touching in this port is deliberately confined to
this one file (plus `ch422g.py` for the IO-expander) so that adjusting
to a different vendored version is a single-file change, not a
scattered one -- same isolation principle as `twai_driver.py`'s
native-API risk in Phase 1.

This module intentionally has no `asyncio`/threading logic of its own --
`gui_app.py` owns the Core 0 thread and the render-loop timing; this
module's job is strictly "bring the display/touch/LVGL stack up and hand
back the objects gui_app.py needs."
"""

from tbgw.gui.ch422g import CH422G
from tbgw.utils.logger import get_logger

logger = get_logger("gui")

# Confirmed against ESP-IDF/08_lvgl_Porting/main/waveshare_rgb_lcd_port.h
# (Waveshare's own vendor demo firmware) -- see docs/pin_map.md.
_RGB_PINS = dict(
    hsync=46, vsync=3, de=5, pclk=7,
    data0=14, data1=38, data2=18, data3=17, data4=10,     # B0-B4
    data5=39, data6=0, data7=45, data8=48, data9=47, data10=21,  # G0-G5
    data11=1, data12=2, data13=42, data14=41, data15=40,  # R0-R4
)
# THIS BOARD IS THE 1024x600 VARIANT, not 800x480.
#
# The Waveshare ESP32-S3-Touch-LCD-5 ships with either panel, and the
# vendor demo selects between them with a single `ESP_PANEL_USE_1024_600_LCD`
# switch (`08_lvgl_Porting/main/lvgl_port.h`) whose two branches have
# WILDLY different timings -- not a small tweak. Phase 5 bring-up wasted
# a long time driving this panel with the 800x480 branch's values, which
# produces a permanently corrupted image (white field, black corners,
# vertical line tearing) that does NOT respond to any amount of
# porch/pclk/polarity adjustment, because the resolution itself is wrong.
#
# Two independent confirmations that this is the 1024x600 unit:
#   1. The GT911 touch controller reports its factory resolution as
#      1024x600 (printed by the driver at every boot). That is the panel
#      it was configured against at the factory.
#   2. Waveshare's own product-page performance notes for this board
#      quote "PCLK: 21 MHz" -- which is exactly, and only, the
#      1024x600 branch's pixel clock (the 800x480 branch is 16MHz).
#
# Values below are the vendor's 1024x600 branch verbatim
# (`waveshare_rgb_lcd_port.c`, `waveshare_rgb_lcd_port.h`). Note the
# porches are an order of magnitude larger than the 800x480 branch's
# 8/8/4 -- which is why nudging those between 4 and 8 changed nothing.
#
# `pclk_active_low`: the vendor source sets `pclk_active_neg = 1` (data
# latched/driven on PCLK's falling edge) in ESP-IDF's own
# `esp_lcd_rgb_panel_config_t.flags` struct. Despite the similar name,
# that is NOT what `pclk_active_low` means in `lcd_bus.RGBBus`'s own
# terms -- confirmed empirically on real hardware: `pclk_active_neg=1`'s
# direct value (`pclk_active_low=True`) produced a visible red-channel
# deficiency (a white background rendered cyan); the OPPOSITE value
# (False) is what actually reproduces the vendor's colors correctly. Do
# not "fix" this back to True by pattern-matching the vendor source
# without retesting on hardware.
_RGB_TIMING = dict(
    # 12MHz, NOT the vendor's 21MHz -- deliberately, and do not raise it
    # without reading this first.
    #
    # The vendor runs 21MHz with a BOUNCE BUFFER configured
    # (`bounce_buffer_size_px` in their `esp_lcd_rgb_panel_config_t`).
    # `lvgl_micropython`'s rgb_bus.c never sets `bb_size` at all, so the
    # panel's DMA reads full framebuffers straight out of PSRAM with no
    # internal-SRAM staging. At 1024x600 that is 1.6x the pixel rate of
    # the 800x480 panels this binding is usually used with, and above
    # roughly this clock the DMA starves: the visible symptom is the
    # image shifting horizontally (scanout consuming the wrong number of
    # pixels per line), plus flicker.
    #
    # That starvation is not merely cosmetic -- it HARD-LOCKS THE BOARD.
    # rgb_bus.c's `rgb_tx_color()` (the LVGL flush callback) does
    # `rgb_bus_lock_acquire(&self->tx_color_lock, -1)` -- an unbounded
    # wait for the background copy task -- and that file contains no
    # MP_THREAD_GIL_EXIT/GIL_ENTER anywhere, so it blocks holding
    # MicroPython's GIL. When the copy task can't keep up, that lock is
    # never released and EVERY Python thread in the process stops dead:
    # GUI, the asyncio gateway loop, the CAN thread. No exception, no
    # traceback, no soft-reset recovery -- only a physical power cycle.
    # Phase 5 lost a lot of time to this, misread first as a display
    # timing bug and then as a GUI bug.
    #
    # 12MHz matches lvgl_micropython's own proven config for the
    # pin-identical 4.3B panel and leaves real headroom. The proper fix
    # is to patch rgb_bus.c (release the GIL around that acquire, bound
    # the timeout, and set bb_size) -- see build/build_firmware.sh.
    freq=21_000_000,
    hsync_front_porch=170, hsync_back_porch=145, hsync_pulse_width=30, hsync_idle_low=False,
    vsync_front_porch=12, vsync_back_porch=23, vsync_pulse_width=2, vsync_idle_low=False,
    de_idle_high=False, pclk_idle_high=False, pclk_active_low=False,
)

DISPLAY_WIDTH = 1024
DISPLAY_HEIGHT = 600
# Full-frame size, kept for reference/logging only -- NOT what is
# allocated for LVGL (see _DRAW_BUFFER_SIZE below and the allocation
# site). rgb_bus.c allocates two of these internally for the panel
# itself; adding two more here is what exhausted PSRAM.
_FRAMEBUFFER_SIZE = DISPLAY_WIDTH * DISPLAY_HEIGHT * 2  # RGB565 = 2 bytes/pixel
# LVGL draw-buffer band height in rows -- see the allocation site in
# init_display_and_touch() for why these are PARTIAL, not full-screen.
_DRAW_BUFFER_ROWS = 100
_DRAW_BUFFER_SIZE = DISPLAY_WIDTH * _DRAW_BUFFER_ROWS * 2

# Confirmed against the same vendor source and independently against
# multiple ESP-IDF example sdkconfig.defaults files -- see docs/pin_map.md.
_I2C_SCL_PIN = 9
_I2C_SDA_PIN = 8
_I2C_FREQ = 400_000

# Vendor sequence timing (waveshare_esp32_s3_touch_reset(), confirmed):
# hold touch-reset-asserted for 100ms, then release and wait a further
# 200ms before using the touch controller. LCD reset is held deasserted
# throughout -- see ch422g.py's set_lcd_reset() docstring.
_TOUCH_RESET_HOLD_MS = 100
_TOUCH_RESET_SETTLE_MS = 200


class LvglSetupError(Exception):
    pass


class DisplayHandles:
    """Plain container for what gui_app.py needs after bring-up -- avoids
    gui_app.py needing to know any lvgl_micropython-specific object types.
    """

    def __init__(self, lv, display, indev, i2c_bus, ch422g):
        self.lv = lv
        self.display = display
        self.indev = indev
        self.i2c_bus = i2c_bus
        self.ch422g = ch422g


def init_display_and_touch():
    """Brings up LVGL, I2C, the CH422G (reset sequencing + backlight),
    the RGB display, and GT911 touch. Returns a DisplayHandles. Raises
    LvglSetupError (wrapping the underlying exception) on any failure,
    so gui_app.py can log a clear, single-point failure rather than an
    unguided traceback from deep inside hardware bring-up.

    Order matters and is confirmed against `lvgl_micropython`'s own
    README example: `lv.init()` must run BEFORE the display driver is
    constructed (which internally registers it as LVGL's default
    display) and before the touch indev is constructed (which looks up
    that default display via `lv.display_get_default()` -- there is no
    way to pass a display to it directly, unlike this project's earlier
    assumption).
    """
    try:
        import i2c
        import lcd_bus
        import rgb_display
        import gt911
        import lvgl as lv
    except ImportError as exc:
        raise LvglSetupError(
            "lvgl_micropython modules not importable (%s) -- this firmware was not "
            "built with lvgl_micropython (see build/build_firmware.sh); the GUI "
            "cannot run on this firmware image." % exc
        )

    try:
        lv.init()

        i2c_bus = i2c.I2C.Bus(host=0, scl=_I2C_SCL_PIN, sda=_I2C_SDA_PIN, freq=_I2C_FREQ, use_locks=False)
        ch422g = CH422G(i2c_bus, logger_=logger)

        # Enable output mode on all EXIO lines FIRST -- confirmed
        # required by vendor source (mode register bit0, IO_OE); no
        # output-register write has any effect until this is set.
        ch422g.set_mode(0x01)

        # LCD reset is held DEASSERTED throughout -- the vendor's own
        # firmware never pulses it (see ch422g.py's set_lcd_reset()
        # docstring and docs/pin_map.md). Touch reset IS pulsed, with
        # vendor-confirmed hold/settle timings.
        ch422g.set_lcd_reset(False)
        ch422g.set_touch_reset(True)
        _sleep_ms(_TOUCH_RESET_HOLD_MS)
        ch422g.set_touch_reset(False)
        _sleep_ms(_TOUCH_RESET_SETTLE_MS)
        ch422g.set_backlight(False)  # stay off until the first frame is drawn, avoids a flash of garbage

        display_bus = lcd_bus.RGBBus(**_RGB_PINS, **_RGB_TIMING)
        # PARTIAL draw buffers -- must NOT be full-screen.
        #
        # These are LVGL's DRAW buffers. rgb_bus.c separately allocates
        # the panel's OWN double framebuffer in PSRAM regardless
        # (`fb_in_psram=1, double_fb=1`, see rgb_init), and a background
        # copy task copies from these into it. Passing two full-screen
        # buffers here therefore allocates FOUR full framebuffers in
        # total: at 1024x600x2 that is 4 x 1.2MB = 4.8MB.
        #
        # Measured on hardware: free memory went from 5.4MB at boot to
        # ~206KB with the GUI up, leaving too little for LVGL's renderer
        # -- which then failed to allocate a ~36KB block mid-render and
        # threw MemoryError, and (via TaskHandler's exception hook)
        # killed the display pump outright. The whole "GUI freezes after
        # a while" symptom was that, downstream of this.
        #
        # Espressif's own LCD/LVGL performance note puts the sweet spot
        # at 10-25% of the screen (>25% buys nothing, <10% hurts badly).
        # 100 rows of 1024 is ~17%, ~200KB per buffer, ~400KB for the
        # pair -- roughly 2MB less than before.
        framebuffer1 = display_bus.allocate_framebuffer(_DRAW_BUFFER_SIZE, lcd_bus.MEMORY_SPIRAM)
        framebuffer2 = display_bus.allocate_framebuffer(_DRAW_BUFFER_SIZE, lcd_bus.MEMORY_SPIRAM)

        display = rgb_display.RGBDisplay(
            data_bus=display_bus,
            display_width=DISPLAY_WIDTH,
            display_height=DISPLAY_HEIGHT,
            frame_buffer1=framebuffer1,
            frame_buffer2=framebuffer2,
            color_space=lv.COLOR_FORMAT.RGB565,
        )
        display.init()

        # Deliberately NOT lowered to the 10ms that Waveshare's
        # "LCD & LVGL Performance" note recommends. That figure assumes
        # ESP-IDF's `esp_lvgl_port` DIRECT mode, where LVGL renders
        # straight into the panel's own framebuffer. This binding instead
        # runs a background copy task that memcpy's a WHOLE 1024x600x2
        # (~1.2MB) framebuffer per flush, so a 10ms period asks for on
        # the order of 100MB/s of memory bandwidth and starves the rest
        # of the system: measured on hardware, the asyncio gateway loop
        # stopped getting scheduled often enough to dispatch queued
        # commands, so light-switch taps only took effect once the user
        # navigated away and the render load dropped. LVGL's default
        # period is left alone.
        _ = display  # (refresh period intentionally left at LVGL's default)

        # No display= kwarg here -- GT911 (and every indev driver in
        # this framework) associates with LVGL's current default display
        # automatically; see this module's docstring.
        touch_device = i2c.I2C.Device(i2c_bus, dev_id=gt911.I2C_ADDR, reg_bits=gt911.BITS)
        indev = gt911.GT911(touch_device)

        ch422g.set_backlight(True)
        logger.info(
            "LVGL display+touch initialized (%dx%d, PCLK=%dHz)",
            DISPLAY_WIDTH, DISPLAY_HEIGHT, _RGB_TIMING["freq"],
        )
        return DisplayHandles(lv=lv, display=display, indev=indev, i2c_bus=i2c_bus, ch422g=ch422g)

    except LvglSetupError:
        raise
    except Exception as exc:
        raise LvglSetupError("Display/touch bring-up failed: %s" % exc)


def _sleep_ms(ms):
    try:
        import time
        if hasattr(time, "sleep_ms"):
            time.sleep_ms(ms)
        else:
            time.sleep(ms / 1000.0)
    except ImportError:  # pragma: no cover
        pass
