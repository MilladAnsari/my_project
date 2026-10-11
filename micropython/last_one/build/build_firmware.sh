#!/usr/bin/env bash
# build_firmware.sh -- builds the combined MicroPython+LVGL+TWAI firmware
# for the Waveshare ESP32-S3-Touch-LCD-5B (ESP32-S3-WROOM-1-N16R8, 16MB
# quad-SPI flash, 8MB octal PSRAM).
#
# This is the VERIFIED recipe (built, flashed, and confirmed working --
# `import CAN` and `import lvgl` both succeed on real hardware, and the
# CAN driver opens the real 50kbps xxx bus cleanly) as of the Phase 1
# hardware bring-up session. It supersedes any earlier "stock MicroPython
# + straga USER_C_MODULES" recipe: LVGL and TWAI are built together via
# lvgl_micropython's own build system, which natively supports vendoring
# an extra USER_C_MODULE alongside its LVGL/display/touch modules (see
# docs/architecture.md's "Why CAN needs custom firmware" section).
#
# Run this manually (or adapt into CI) after installing the prerequisites
# below. Not invoked automatically by anything else in this repository.
#
# ---- Prerequisites (Ubuntu/Debian) -----------------------------------
#   sudo apt-get install build-essential cmake ninja-build python3 \
#     python3-venv libusb-1.0-0-dev git
# python3 >= 3.10 required (see lvgl_micropython's own README).
# ------------------------------------------------------------------------

set -euo pipefail

# ---- PINNED VERSIONS (verified working combination) --------------------
# lvgl_micropython has no tagged releases (rolling-release repo) -- pin a
# commit for reproducibility. This one builds cleanly against straga's
# TWAI module (below) once the MP_ERROR_TEXT patch (see PATCHES) is
# applied.
LVGL_MICROPYTHON_REF="${LVGL_MICROPYTHON_REF:-d2d26467fa4cb9e99e569d899709043d086f7a6f}"
# Pulls MicroPython 1.27.0, LVGL v9.4.0, and ESP-IDF 5.5.1 as its own
# submodules automatically -- do NOT point IDF_TOOLS_PATH at a
# pre-existing eim/idf.py install unless its version happens to match
# lvgl_micropython's pinned IDF_VER exactly (checked via
# `builder/esp32.py`'s `IDF_VER` constant); a mismatch is silently
# ignored and it clones its own IDF anyway, so there's no point fighting
# this -- just let it manage its own toolchain (see WORKDIR below).
TWAI_MODULE_REF="${TWAI_MODULE_REF:-07aa36e63fe8a982f2f92f8ae12711187ff916a3}"
BOARD="${BOARD:-ESP32_GENERIC_S3}"
BOARD_VARIANT="${BOARD_VARIANT:-SPIRAM_OCT}"   # required for the 8MB octal PSRAM on the N16R8 module
FLASH_SIZE_MB="${FLASH_SIZE_MB:-16}"
# Lower this (e.g. 2) if the build fails with simultaneous "broken pipe" /
# "invalid argument" errors across many unrelated source files -- that's
# a resource-exhaustion signature (RAM/swap pressure under full
# parallelism), not a real compile error. Default matches ninja's
# jobserver default of nproc; override via MAKEFLAGS=-jN in the
# environment if your machine is memory-constrained.
# ------------------------------------------------------------------------

# ---- Workspace -----------------------------------------------------------
# Needs several GB (toolchain + build objects). Defaults to a sibling of
# this repo, but **use a partition with real headroom** -- a full build
# needs on the order of 5-8GB. Override with WORKDIR=/path if your
# checkout's partition is tight (this bit the original bring-up: /home
# was down to ~3GB free from an unrelated pre-existing toolchain cache;
# the fix was pointing both WORKDIR and IDF_TOOLS_PATH at a roomier
# partition, e.g. /opt/tmp/ -- adjust for your own machine).
WORKDIR="${WORKDIR:-$(pwd)/tbgw-firmware-build}"
IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$WORKDIR/esp_idf_tools}"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# ---- Network note ----------------------------------------------------------
# If large clones (lvgl_micropython's `lib/lvgl` submodule, or ESP-IDF's
# own clone) fail with `GnuTLS recv error (9): Error decoding the
# received TLS packet` or similar mid-transfer corruption, that's a
# transport-layer flakiness some networks exhibit specifically on large
# sustained HTTP/2 transfers -- NOT a real error to chase. Mitigate with:
#   git config --global http.version HTTP/1.1
#   git config --global http.postBuffer 1048576000
# then just retry the clone.

echo "== 1. Fetch lvgl_micropython (pinned $LVGL_MICROPYTHON_REF) =="
if [ ! -d lvgl_micropython ]; then
  git clone https://github.com/lvgl-micropython/lvgl_micropython.git
fi
( cd lvgl_micropython && git fetch origin "$LVGL_MICROPYTHON_REF" && git checkout "$LVGL_MICROPYTHON_REF" )

echo "== 2. Vendor + patch straga/micropython-esp32-twai ($TWAI_MODULE_REF) =="
TWAI_DIR="lvgl_micropython/ext_mod/micropython-esp32-twai"
if [ ! -d "$TWAI_DIR" ]; then
  git clone https://github.com/straga/micropython-esp32-twai.git "$TWAI_DIR"
fi
( cd "$TWAI_DIR" && git fetch origin "$TWAI_MODULE_REF" && git checkout "$TWAI_MODULE_REF" )

# PATCHES (build/patches/micropython-esp32-twai-fixes.patch) -- two
# independent fixes to straga's src_can_v2/mod_can.c, both required:
#
#  1. BUILD FIX. It was written against an older MicroPython where
#     mp_raise_msg()/mp_raise_ValueError()/etc. took a plain `char *`.
#     MicroPython 1.27.0 (vendored by lvgl_micropython) requires the newer
#     `mp_rom_error_text_t` wrapper -- string literals need
#     MP_ERROR_TEXT(...) around them, or the build fails with "passing
#     argument N of 'mp_raise_*' from incompatible pointer type" at 10
#     call sites.
#
#  2. RUNTIME FIX -- the important one, and silent if you miss it. The
#     module had a "initialize the singleton's timing on first use" guard:
#         if (can_config.timing.brp == 0) {
#             can_config.timing = get_timing_config(500000);
#         }
#     On ESP-IDF >= 5.x every TWAI_TIMING_CONFIG_*() macro sets .brp = 0
#     and expresses the rate via .quanta_resolution_hz instead, so that
#     condition is ALWAYS true. Because the object is a singleton
#     (self->config aliases can_config), it clobbered the caller's
#     requested timing with the 500kbit config on EVERY construction --
#     i.e. `bitrate=` was silently ignored and the controller always ran
#     at 500kbit/s. Symptom: no frames received, no ACKs for anything
#     transmitted, and a bus_error_count climbing into the tens/hundreds
#     of thousands, at any configured bitrate (the giveaway is that
#     `CAN: timing tseg_1/tseg_2/sjw` print IDENTICAL values regardless of
#     the bitrate you asked for). The guard is also redundant --
#     self->config->timing is unconditionally assigned just above it in
#     both branches -- so the patch simply removes it.
#
# Applied idempotently (skipped if already applied, e.g. on a re-run).
if ! grep -q 'MP_ERROR_TEXT("Auto-restart not supported")' "$TWAI_DIR/src_can_v2/mod_can.c"; then
  echo "   Applying tbgw compatibility + timing patches..."
  patch -p1 -d "$TWAI_DIR" < "$(dirname "$0")/patches/micropython-esp32-twai-fixes.patch"
fi

echo "== 2b. Patch lvgl_micropython's RGB display driver =="
# PATCHES (build/patches/lvgl-micropython-rgb-bus-fixes.patch) -- two
# defects in lvgl_micropython's OWN ESP32 RGB driver (not a third-party
# module this time), both of which this project hit on real hardware:
#
#  1. A lost-wakeup race in rgb_bus_copy_task() (rgb_bus_rotation.c):
#     `swap_bufs` was cleared AFTER esp_lcd_panel_draw_bitmap() had
#     already armed the framebuffer swap, so an on_vsync ISR firing in
#     that window had its notification wiped -- and the ISR's own guard
#     (curr_buf != active_fb) stopped it ever setting the event again.
#     The copy task then waited forever.
#
#  2. rgb_tx_color() (rgb_bus.c) held the MicroPython GIL across an
#     unbounded wait for that same copy task. So (1) did not merely
#     freeze the display -- it froze EVERY Python thread in the process
#     (GUI, asyncio gateway loop, CAN thread), with no exception and no
#     traceback, recoverable only by physically power-cycling the board.
#
# Symptom if this patch is ever dropped: the board hard-locks after a
# burst of LVGL redraws (widget animations are the reliable trigger),
# sooner at higher pixel clocks, and the serial console goes completely
# silent -- heartbeat logging included.
#
# Applied idempotently (skipped if already applied, e.g. on a re-run).
if ! grep -q 'tbgw patch' lvgl_micropython/ext_mod/lcd_bus/esp32_src/rgb_bus.c; then
  echo "   Applying tbgw RGB bus deadlock patches..."
  patch -p1 -d lvgl_micropython < "$(dirname "$0")/patches/lvgl-micropython-rgb-bus-fixes.patch"
fi

echo "== 3. Build (this downloads/builds the ESP-IDF 5.5.1 toolchain on first run -- expect it to take a while) =="
cd lvgl_micropython
TWAI_MODULE_PATH="$(pwd)/ext_mod/micropython-esp32-twai/src_can_v2/micropython.cmake"
# NOTE: pass the LOCAL path here, not `USER_C_MODULE=https://github.com/...:...`.
# lvgl_micropython's git-URL handling for USER_C_MODULE writes a
# repo-root-relative include() path into the generated
# ext_mod/esp32_components.cmake, which CMake cannot resolve from deep
# inside the ESP-IDF build tree (`CMake Error ... include could not find
# requested file`). An absolute local path sidesteps that code path
# entirely and just works, which is why step 2 above vendors the module
# ourselves rather than letting `make.py` clone it.
env IDF_TOOLS_PATH="$IDF_TOOLS_PATH" python3 make.py esp32 \
  BOARD="$BOARD" BOARD_VARIANT="$BOARD_VARIANT" \
  DISPLAY=rgb_display INDEV=gt911 \
  USER_C_MODULE="$TWAI_MODULE_PATH" \
  --flash-size="$FLASH_SIZE_MB" \
  --enable-jtag-repl=y --enable-cdc-repl=n \
  CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y \
  CONFIG_IDF_EXPERIMENTAL_FEATURES=y \
  CONFIG_SPIRAM_SPEED_120M=y \
  CONFIG_SPIRAM_FETCH_INSTRUCTIONS=y \
  CONFIG_SPIRAM_RODATA=y \
  CONFIG_ESP32S3_DATA_CACHE_LINE_64B=y \
  CONFIG_ESPTOOLPY_FLASHMODE_QIO=y \
  CONFIG_ESPTOOLPY_FLASHFREQ_120M=y \
  CONFIG_FREERTOS_HZ=1000 \
  CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y \
  CONFIG_COMPILER_OPTIMIZATION_PERF=y

# ABOUT the PSRAM/cache CONFIG_* block above (added during Phase 5 GUI
# bring-up): the RGB display renders through DMA reading full framebuffers
# directly out of PSRAM -- lvgl_micropython's rgb_bus.c never allocates a
# bounce buffer (its bb_size field is declared but never set), so the panel
# refresh is entirely at the mercy of sustained PSRAM read bandwidth. At
# this build's previous defaults (CONFIG_SPIRAM_SPEED_80M,
# CONFIG_ESP32S3_DATA_CACHE_LINE_32B) the display showed persistent
# corruption -- a white field with black corners and vertical line tearing
# -- that did NOT change with ANY RGB bus timing parameter (pixel clock
# 12/13/16MHz, both pclk polarities, several porch combinations), which is
# the signature of DMA starvation rather than a sync-timing mismatch.
# These values replicate lvgl_micropython's own proven-working
# display_configs/Waveshare-ESP32-S3-Touch-LCD-4.3B.toml, which drives a
# panel with byte-for-byte identical data0-15/hsync/vsync/de/pclk pin
# assignments to this board. 120MHz PSRAM/flash requires
# CONFIG_IDF_EXPERIMENTAL_FEATURES=y, which is why it is set alongside.
#
# Waveshare's own product-page "Performance Notes (ESP-IDF & LVGL)" for
# this board independently prescribes this exact same set as mandatory
# pre-compilation menuconfig settings, and additionally CONFIG_FREERTOS_HZ
# =1000 (this build's default was 100, a 10x coarser scheduler tick that
# directly limits LVGL frame pacing). Their quoted achievable numbers on
# this panel are 26 FPS average / 41 FPS interface at PCLK 21MHz -- note
# that PCLK is a runtime RGBBus argument (tbgw/gui/lvgl_setup.py's
# _RGB_TIMING), not a build setting, so it can be raised toward 21MHz
# after confirming a clean image at the conservative 12MHz first.
# CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240 and CONFIG_COMPILER_OPTIMIZATION_PERF
# were already correct in this build; they are passed explicitly anyway so
# the full required set is visible in one place rather than depending on
# defaults that could change with a lvgl_micropython bump.

# ABOUT --enable-jtag-repl=y --enable-cdc-repl=n CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y:
# This board's "UART"-labeled USB-C port is ESP-IDF's default console
# target (CONFIG_ESP_CONSOLE_UART_DEFAULT=y) -- if you have that second
# cable connected, you can drop all three of these and use that port for
# the REPL instead (matches Waveshare's own intended wiring). If you're
# using only the single native-USB port (the one esptool flashes over,
# `/dev/ttyACM0`-style), you need all three: --enable-jtag-repl=y sets
# MicroPython's own HAL to talk over the native USB-Serial-JTAG
# peripheral; --enable-cdc-repl=n is REQUIRED alongside it (the board's
# own default already enables native USB-CDC, and MicroPython's
# mpconfigport.h hard-errors if both peripherals are enabled
# simultaneously -- "Can't enable both native USB and USB Serial/JTAG
# peripheral"); CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y is a SEPARATE,
# ESP-IDF-level (not MicroPython-level) console-routing setting that
# --enable-jtag-repl alone does NOT touch -- without it, the firmware
# still boots to a raw ESP-IDF console on UART0 with no interactive
# output at all on the native-USB port.

echo "== 4. Flash =="
echo "   The build prints an exact 'python -m esptool ...' command ending in"
echo "   'To flash firmware: ...' -- use that verbatim (it references a merged"
echo "   single .bin under lvgl_micropython/build/), replacing (PORT) with your"
echo "   device, e.g. /dev/ttyACM0. Example:"
echo ""
echo "   $IDF_TOOLS_PATH/python_env/idf5.5_py3.14_env/bin/python -m esptool \\"
echo "     --chip esp32s3 --port /dev/ttyACM0 -b 460800 \\"
echo "     --before default_reset --after hard_reset write_flash \\"
echo "     --flash_mode dio --flash_size ${FLASH_SIZE_MB}MB --flash_freq 80m --erase-all \\"
echo "     0x0 build/lvgl_micropy_${BOARD}-${BOARD_VARIANT}-${FLASH_SIZE_MB}.bin"
echo ""
echo "== 5. If the board doesn't respond after flashing (native-USB-only setup) =="
echo "   ESP32-S3's native USB-Serial-JTAG peripheral can latch a persistent"
echo "   'stay in ROM download mode' flag in RTC memory that survives software"
echo "   resets (esptool, mpremote, esp_idf_monitor 'reset' all fail to clear"
echo "   it -- you'll see 'rst:0x15 (USB_UART_CHIP_RESET),boot:0x0"
echo "   (DOWNLOAD(USB/UART0))' / 'waiting for download' on every attempt)."
echo "   A genuine power cycle (physically unplug/replug the USB cable) is the"
echo "   only fix -- it clears RTC memory, which a soft EN-reset does not."
echo ""
echo "== 6. Verify =="
echo "   mpremote connect /dev/ttyACM0 exec \"import CAN; import lvgl; print('OK')\""
echo ""
echo "== 7. Deploy the tbgw source tree =="
echo "   mpremote connect /dev/ttyACM0 fs cp -r ../../config :config"
echo "   mpremote connect /dev/ttyACM0 fs cp -r ../../src/. :"
echo "   mpremote connect /dev/ttyACM0 reset"

# ---------------------------------------------------------------------------
# Known limitations / follow-ups (tracked here, not yet acted on)
# ---------------------------------------------------------------------------
# - The application partition this build produces is EXTREMELY tight:
#   as built, only a few hundred bytes free out of ~2.9MB. LVGL's default
#   config compiles in a lot of optional library support this project
#   doesn't use (barcode, BMP, expat/XML, FFmpeg, FreeType, FT800/FT813,
#   frogfs, ...) -- trimming those via a custom lv_conf.h / LV_CFLAGS, or
#   growing the app partition via a custom partition table
#   (build/partitions.csv sketches one, but is NOT currently wired into
#   this build -- lvgl_micropython auto-generates its own partition table
#   from --flash-size), is a real near-term need once more Python source
#   gets frozen in or more LVGL features get used, not yet done.
# - Display/touch pin wiring (RGB timing, CH422G reset/backlight
#   sequencing) is documented in docs/pin_map.md from the vendor's own
#   demo firmware, but not yet applied to tbgw/gui/lvgl_setup.py -- that's
#   Phase 5 (GUI bring-up), not this build script's job.
