# Pin map -- Waveshare ESP32-S3-Touch-LCD-5B

Sources:
- https://docs.waveshare.com/ESP32-S3-Touch-LCD-5?variant=ESP32-S3-LCD-5B-touch
- **`ESP32-S3-Touch-LCD-5-Demo.zip`'s ESP-IDF examples** (vendor's own C
  firmware, obtained directly from Waveshare and read at the source level
  during Phase 0 hardware bring-up -- this supersedes the prior revision
  of this file, which relied on a community config for a *different*
  (7") board as its best available source). Specifically:
  `ESP-IDF/06_TWAItransmit`, `ESP-IDF/07_TWAIreceive` (TWAI pins/bitrate),
  `ESP-IDF/05_IO_Test` (I2C pins, CH422G register map),
  `ESP-IDF/08_lvgl_Porting/main/waveshare_rgb_lcd_port.{c,h}` (RGB pins,
  timings, CH422G-driven reset/backlight sequence, GT911 wiring).

All values below are now **confirmed from the vendor's own firmware
source**, not inferred from a sibling board or a datasheet's generic
convention.

## CAN (TWAI)

| Signal | GPIO |
|---|---|
| CAN TX | GPIO15 |
| CAN RX | GPIO16 |

Confirmed twice over: directly by the user against the board's pinout
page, and independently by `ESP-IDF/06_TWAItransmit` and
`.../07_TWAIreceive`'s `sdkconfig.defaults`
(`CONFIG_EXAMPLE_TX_GPIO_NUM=15`, `CONFIG_EXAMPLE_RX_GPIO_NUM=16`).
Wired on-board to a TJA1051T/3/1J high-speed CAN transceiver (classic
CAN 2.0A/B; ESP32-S3's TWAI controller does not support CAN-FD). Values
are set in `tbgw.core.constants.CAN_TX_PIN` /
`tbgw.core.constants.CAN_RX_PIN`. Do not hardcode these pin numbers
anywhere else -- always import from `tbgw.core.constants`.

The vendor's own `06_TWAItransmit` example uses `TWAI_TIMING_CONFIG_50KBITS()`
-- consistent with the xxx bus's actual bitrate (50 kbps, confirmed by
the user for the bench devices this project targets).

## Display (RGB565 parallel bus, ST7262 driver)

Confirmed against `ESP-IDF/08_lvgl_Porting/main/waveshare_rgb_lcd_port.h`
(the canonical touch+LVGL example -- NOT `05_IO_Test`'s plain RGB demo,
whose header has slightly different porch/PCLK values for a non-touch
init path; use the `08_lvgl_Porting` values below).

| Signal | GPIO | Signal | GPIO | Signal | GPIO |
|---|---|---|---|---|---|
| DE    | 5  | B0 (data0) | 14 | R0 (data11) | 1  |
| VSYNC | 3  | B1 (data1) | 38 | R1 (data12) | 2  |
| HSYNC | 46 | B2 (data2) | 18 | R2 (data13) | 42 |
| PCLK  | 7  | B3 (data3) | 17 | R3 (data14) | 41 |
|       |    | B4 (data4) | 10 | R4 (data15) | 40 |
|       |    | G0 (data5) | 39 |              |    |
|       |    | G1 (data6) | 0  |              |    |
|       |    | G2 (data7) | 45 |              |    |
|       |    | G3 (data8) | 48 |              |    |
|       |    | G4 (data9) | 47 |              |    |
|       |    | G5 (data10)| 21 |              |    |

(These happen to be pixel-for-pixel identical to the community 7"-sibling
values `lvgl_setup.py` already used as a placeholder -- no code change
needed for the pin numbers themselves. What DOES need correcting, per
the vendor source, is the **timing/porch values and PCLK frequency**,
listed below -- `lvgl_setup.py`'s current `_RGB_TIMING` was a distinct,
lower-confidence guess and does not match.)

**Timing (800x480 branch, i.e. `ESP_PANEL_USE_1024_600_LCD=0`, which is
this board's resolution)**:

| Parameter | Vendor value | `lvgl_setup.py`'s current (unconfirmed) value |
|---|---|---|
| PCLK frequency | **16 MHz** (`EXAMPLE_LCD_PIXEL_CLOCK_HZ`, `08_lvgl_Porting`) | 13 MHz -- update to 16 MHz |
| HSYNC pulse width | 4 | 2 -- update |
| HSYNC back porch | 8 | 4 -- update |
| HSYNC front porch | 8 | 4 -- update |
| VSYNC pulse width | 4 | 2 -- update |
| VSYNC back porch | 8 | 4 -- update |
| VSYNC front porch | 8 | 4 -- update |
| PCLK polarity | `pclk_active_neg = 1` (data latched/driven relative to PCLK falling edge) | `pclk_active_low=False` -- update to whatever `lvgl_micropython`'s `RGBBus` calls the equivalent of "active_neg"/falling-edge PCLK |
| Data width | 16 (full RGB565 bus) | matches (16 lines already wired) |
| `disp_gpio_num` / dedicated LCD reset GPIO | **not used** (`-1`) -- LCD reset is not a direct ESP32 GPIO at all | n/a, no change needed |

Framebuffer: vendor example uses **2 framebuffers in PSRAM**
(`LVGL_PORT_LCD_RGB_BUFFER_NUMS = 2`, full-size, `fb_in_psram = 1`), not
a fractional bounce buffer -- `lvgl_setup.py`'s `_FRAMEBUFFER_SIZE`
(1/20th of a frame) was a conservative placeholder; revisit in Phase 5
once real PSRAM headroom is measured on this board (8MB PSRAM gives
ample room for two full 800x480x16bpp buffers, which is ~1.5MB total).

Resolution: 800x480 confirmed (`LVGL_PORT_H_RES=800`, `LVGL_PORT_V_RES=480`
for the non-1024x600 branch, which is this board's variant).

## I2C bus (touch + IO expander)

**SCL/SDA pins -- now confirmed** (previously the only unresolved pin
question on this board): `ESP-IDF/05_IO_Test/sdkconfig.defaults` and
`08_lvgl_Porting/main/waveshare_rgb_lcd_port.h` both fix:

| Signal | GPIO |
|---|---|
| I2C SCL | **GPIO9** |
| I2C SDA | **GPIO8** |

(`lvgl_setup.py`'s placeholders, `_I2C_SCL_PIN = 9` / `_I2C_SDA_PIN = 8`,
were exactly right -- no change needed. I2C clock: 400kHz, confirmed.)

| Device | I2C address | Purpose |
|---|---|---|
| CH422G  | `0x24` (mode/system reg), `0x38` (output reg), `0x26` (input reg) -- see below | LCD reset, touch reset, backlight on/off, SD_CS, isolated I/O |
| GT911   | `0x5D` | Capacitive touch (confirmed via Waveshare's docs page directly) |
| PCF85063A | `0x51` | Real-time clock -- **not used by this port** (see note below) |

**PCF85063A RTC note**: unchanged from before -- could resolve the
Unix-epoch timestamp gap in `uplink_client.py`; reading it remains out
of scope, NTP is the simpler fix (see Phase 4 plan in the top-level
working notes).

## CH422G -- register addresses and EXIO map, now fully confirmed

Read directly from the vendor's `ESP-IDF/05_IO_Test/lib/CH422G/CH422G.h`
and `08_lvgl_Porting/main/waveshare_rgb_lcd_port.c`. **Every value
`tbgw/gui/ch422g.py` had marked `# CONFIRM` as a best-guess placeholder
turned out to be exactly correct** -- no register-address or EXIO-index
changes are needed, only the reset/backlight *sequencing* and *hold
timings* (below), which the placeholder code did not have real values
for.

| Register | I2C address | Purpose |
|---|---|---|
| Mode/system | `0x24` | bit0 (`IO_OE`) = 1 enables output mode on IO0-7; must be written before any output-register write |
| Output (IO0-IO7) | `0x38` | one bit per EXIO line, **1 = pin driven high, 0 = pin driven low** (direct, non-inverted logic at the register level) |
| Input | `0x26` | read-back of IO0-7 level (only meaningful with mode's `IO_OE`=0, input mode) |

| EXIO bit | Function | Matches `ch422g.py`'s existing constant |
|---|---|---|
| IO0 (0x01) | Isolated digital input 0 (DI0) -- not used by this port | -- |
| IO1 (0x02) | **Touch reset** | `EXIO_TOUCH_RESET = 1` -- confirmed correct |
| IO2 (0x04) | **Backlight control** | `EXIO_BACKLIGHT = 2` -- confirmed correct |
| IO3 (0x08) | **LCD reset** | `EXIO_LCD_RESET = 3` -- confirmed correct |
| IO4 (0x10) | **SD card CS** | `EXIO_SD_CS = 4` -- already confirmed via Instructions-For-Use page, now double-confirmed via source |
| IO5 (0x20) | Isolated digital input 1 (DI1) -- not used by this port | -- |
| IO6, IO7 | Unused/reserved on this board per the vendor header | -- |

**Polarity -- corrects the port's prior assumption.** The register bit
IS the pin level (1=high, 0=low; no inversion at the CH422G level). The
vendor's own reset/backlight sequence (`waveshare_rgb_lcd_port.c`) then
establishes the *functional* polarity per line:

- **Backlight: active-high.** `wavesahre_rgb_lcd_bl_on()` sets IO2=1;
  `_bl_off()` sets IO2=0. (`ch422g.py`'s `set_backlight(True)` should
  drive the bit high to turn the backlight ON -- confirm this is what
  the current implementation does, since the module docstring flags
  polarity as unconfirmed.)
- **Touch reset: active-low.** `waveshare_esp32_s3_touch_reset()` drives
  IO1=0 (reset asserted) then, after a 100ms hold, IO1=1 (reset
  released), then waits another 200ms before the touch controller is
  used.
- **LCD reset (IO3): held high throughout** in the vendor's own init
  sequence -- it is written as part of the same output-register byte as
  backlight/touch-reset (e.g. `0x2C`, `0x2E`, `0x1E`, `0x1A` -- see the
  source) but is **never toggled low** by the vendor firmware at all.
  This suggests either (a) the LCD panel's reset line doesn't require an
  active pulse from this expander in practice (RGB-interface panels
  often free-run off the timing signals alone), or (b) the vendor
  firmware relies on a power-on-default reset. **Recommendation for
  Phase 5**: mirror the vendor's behavior exactly -- hold LCD reset (IO3)
  high (deasserted) throughout bring-up rather than pulsing it, since
  that's what the one piece of vendor firmware known to actually work on
  this board does. If the display doesn't come up, pulsing IO3 low
  briefly before the RGB panel init is the first thing to try.
- **SD_CS, DI0/DI1**: not used by this port; the vendor default (SD_CS
  driven high initially, e.g. bit4=1 in `0x1E`/`0x2E`) is a reasonable
  no-op default to preserve.

**Sequencing/timing** (replaces `lvgl_setup.py`'s 20ms/120ms
placeholders): set mode register to `0x01` (output mode) once, first;
then hold touch-reset-asserted for **100ms**; then release and wait a
further **200ms** before initializing the GT911 touch driver. There is
no separate LCD-reset pulse in the vendor sequence (see above).

**Touch controller wiring -- also now confirmed**: the vendor's
`esp_lcd_touch_config_t` sets **both `rst_gpio_num` and `int_gpio_num` to
-1** for the GT911 -- i.e. touch reset is handled entirely through the
CH422G (as above) and the GT911's interrupt line is **not used**
(the driver polls over I2C instead). `lvgl_setup.py` should not expect
or wire a direct GPIO for either touch reset or touch interrupt.

## USB / serial ports

Per Waveshare's own docs page: **"the Type-C port labeled UART is used
for program download and log printing."** This board has (at least) two
USB-C ports; the one currently used for bring-up in this project enumerates
as `/dev/ttyACM0` (USB `303a:4001`, the ESP32-S3's **native USB-Serial/JTAG**,
not the UART-labeled port) and has been confirmed to work fine for
flashing (via the system `esptool` v5.4.0 -- note pip's older
`esptool.py` 4.7.0 failed to reset this particular port; prefer the
system `esptool` command if flashing misbehaves). If the native-USB port
ever proves unreliable (e.g. once MicroPython's own USB-CDC takes over
after boot, which can contend with esptool's reset sequence), the
UART-labeled port (likely a CH34x/CP210x adapter, would enumerate as
`/dev/ttyUSB0`) is the vendor-recommended fallback for flashing and for
serial console access once firmware is running.

## Other on-board peripherals -- out of scope for this port

RS485, microSD (beyond noting its CS routes through CH422G EXIO4 above),
CH343 USB-serial, and battery-management pins are not used by this
project and are not enumerated here.
