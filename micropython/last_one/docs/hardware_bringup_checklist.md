# Hardware bring-up checklist

Every item below is scattered across a docstring or config comment
somewhere in this codebase, each marked with the same `# CONFIRM` /
`⚠️ Not verified` convention. This file exists purely to collect them
into one place so bring-up on real hardware has a single checklist to
work through, instead of needing to grep the whole tree for warnings.
None of this project's *logic* is in question here (that's what
`tests/host/`'s 320 tests are for) -- everything below is specifically
"does this match the real, physical board," which cannot be verified
without the board in hand.

## 1. Firmware build (blocks everything else)

- [x] **RESOLVED, Phase 1 (2026-09-05)**: Combine CAN
      (`straga/micropython-esp32-twai`) and GUI (`lvgl_micropython`)
      into one buildable firmware image -- built, flashed, and verified
      on real hardware (`import CAN`, `import lvgl` both succeed;
      `CAN(...)` opens the real 50kbps bus cleanly, `state()` reports
      `TWAI_STATE_RUNNING`). `build/build_firmware.sh` now documents the
      exact working recipe: LVGL and TWAI are built together via
      `lvgl_micropython`'s own build system (`USER_C_MODULE=`, passed as
      a local absolute path -- the git-URL form has a real bug, see the
      script's comments), not a separate stock-MicroPython build as
      earlier drafts of this document assumed. Required a small patch to
      straga's module (`build/patches/`, a MicroPython-1.27 API
      compatibility fix, unrelated to ESP-IDF version) and specific
      console-routing build flags to get a working REPL over the single
      native-USB cable (`--enable-jtag-repl=y --enable-cdc-repl=n
      CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`) -- see the script for the
      full explanation of each.
- [x] **RESOLVED, Phase 1**: the vendored TWAI module's compatibility
      window turned out to be a MicroPython-core API change (not an
      ESP-IDF version issue as originally guessed) -- straga's
      `mp_raise_*` calls needed `MP_ERROR_TEXT(...)` wrapping for
      MicroPython 1.27.0 (the version `lvgl_micropython` vendors).
      Patched, see above.
- [ ] Confirm `lvgl_micropython`'s exact current module/class names
      (`lcd_bus.RGBBus`, `rgb_display.RGBDisplay`, `gt911.GT911`,
      `i2c.I2C`, `task_handler`) against the version actually vendored --
      see `tbgw/gui/lvgl_setup.py`'s module docstring. Adjust that one
      file if names differ; nothing else in `tbgw/gui/` should need
      changes since every other GUI file goes through it.

## 2. Pin verification (before first power-up)

- [x] **RGB LCD data/sync pins** -- **RESOLVED, Phase 0 (2026-09-05)**:
      read directly from the vendor's own `ESP-IDF/08_lvgl_Porting`
      example in `ESP32-S3-Touch-LCD-5-Demo.zip` (obtained from
      Waveshare, not the community 7"-sibling-board guess this checklist
      item originally referred to). Pin numbers matched the prior
      placeholder exactly; the **timing/porch values and PCLK frequency**
      did not and have been corrected in `docs/pin_map.md` -- still need
      to be applied to `tbgw/gui/lvgl_setup.py`'s `_RGB_TIMING` in Phase 5.
- [x] **I2C SCL/SDA pins** -- **RESOLVED, Phase 0 (2026-09-05)**: GPIO9
      (SCL) / GPIO8 (SDA), confirmed via the vendor demo's
      `sdkconfig.defaults` across multiple examples. Matches
      `lvgl_setup.py`'s existing placeholders exactly -- no code change
      needed.
- [x] CAN TX/RX pins (GPIO15/GPIO16) -- confirmed directly by the user
      against the board's pinout page, and independently re-confirmed via
      the vendor demo's own `06_TWAItransmit`/`07_TWAIreceive`
      `sdkconfig.defaults` in Phase 0.

## 3. CH422G IO-expander (before first power-up)

- [x] **RESOLVED, Phase 0 (2026-09-05)**: all three CH422G I2C
      sub-addresses (`0x24` mode, `0x38` output, `0x26` input) and all
      four EXIO bit assignments (`EXIO_TOUCH_RESET=1`,
      `EXIO_BACKLIGHT=2`, `EXIO_LCD_RESET=3`, `EXIO_SD_CS=4`) confirmed
      **exactly correct** against the vendor's own
      `05_IO_Test/lib/CH422G/CH422G.h` and
      `08_lvgl_Porting/main/waveshare_rgb_lcd_port.c` -- no code changes
      needed for addresses/bit indices.
- [x] **RESOLVED, Phase 0 (2026-09-05)**: reset/backlight polarity and
      timing now confirmed from the vendor's actual init sequence --
      see `docs/pin_map.md`'s "CH422G -- register addresses and EXIO
      map" section for the full detail (backlight active-high, touch
      reset active-low with 100ms/200ms holds, LCD reset held high
      throughout rather than pulsed). Still needs to be **applied** to
      `tbgw/gui/lvgl_setup.py`'s init sequence in Phase 5 (this checklist
      item tracked discovery, not yet the code change).
- [x] **New in Phase 0**: the GT911 touch controller's `rst_gpio_num` and
      `int_gpio_num` are both `-1` in the vendor's own config -- touch
      reset goes through CH422G (see above) and the touch driver polls
      over I2C rather than using an interrupt pin. `lvgl_setup.py` should
      not expect/wire a direct GPIO for either.

## 4. Behavioral checks once firmware boots (no code changes expected, just verification)

- [ ] Flash the combined firmware, copy `config/` and `src/` per
      `README.md`'s "Deploying to the board" section, and confirm the
      board boots to the home list screen showing "Main House".
- [ ] xxx

- [ ] Confirm actual LVGL frame rate against the vendor's own benchmark
      (26-41 FPS at 21MHz PCLK per the Instructions-For-Use page) with
      this project's more conservative 13MHz `_RGB_TIMING["freq"]`
      setting in `lvgl_setup.py` -- raise it if there's headroom, lower
      the GUI's `_REFRESH_INTERVAL_MS` further if there's more to spare.
- [ ] Confirm actual `_thread` core placement (see
      `tbgw/core/constants.py`'s core-affinity caveat) -- e.g. by logging
      `esp32.idf_task_info()` or similar from within the CAN thread and
      the GUI thread and checking they land on the cores this project's
      design assumes (GUI: core 0, gateway+CAN: core 1).

## 5. Not blocking, but worth knowing about

- A PCF85063A RTC is on-board (I2C `0x51`) and unused by this port --
  see `docs/pin_map.md`'s note. Reading it would resolve the Unix-epoch
  timestamp gap noted in `tbgw/core/uplink_client.py`.
- RPC acknowledgements to ThingsBoard confirm hand-off to a connector,
  not real device execution (see `docs/architecture.md`'s "RPC
  acknowledgement scope" section) -- expected behavior, not a bug to
  chase during bring-up.
