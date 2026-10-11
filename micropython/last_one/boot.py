# boot.py -- run on boot-up
"""boot.py -- runs once on every hard reset, before main.py.

Kept intentionally minimal: MicroPython re-runs this file's contents on
every hard reset, so anything here should be cheap and safe to repeat.
Heavier setup (config loading, connector startup, GUI/CAN thread launch)
belongs in main.py, not here.
"""

import gc
import machine

# Full CPU speed -- the gateway is doing real-time CAN handling plus LVGL
# rendering plus MQTT/TLS on the other core; running at a lower clock to
# save power is not a good trade-off for this project's requirements.

# Run an initial collection so the heap starts from a clean, defragmented
# state before main.py starts allocating the gateway's long-lived objects
# (queues, connector instances, LVGL buffers).
gc.collect()

# NOTE: PSRAM (8MB octal on the N16R8 module) is expected to already be
# configured as part of the custom firmware build (see build/build_firmware.sh,
# MICROPY_BOARD_VARIANT=SPIRAM_OCT) rather than anything this file needs to
# do at runtime -- MicroPython maps it into the GC heap automatically when
# the firmware is built with PSRAM support enabled.

print("tbgw boot.py: CPU @", machine.freq(), "Hz, free heap:", gc.mem_free())
print("alloc:", gc.mem_alloc())