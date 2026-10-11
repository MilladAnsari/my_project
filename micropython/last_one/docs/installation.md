# Installation and deployment guide

Everything needed to go from a bare board to a running gateway.

Two things are installed separately and must both be done:

1. **Firmware** — a custom MicroPython image containing LVGL *and* the CAN
   module. Neither exists in stock MicroPython for this target, so this is a
   precondition, not an optimisation.
2. **Application** — `tbgw/`, `config/`, `main.py`, `boot.py` copied onto the
   board's filesystem.

---

## 1. Prerequisites

### 1.1 Hardware

| Item | Notes |
|---|---|
| Waveshare ESP32-S3-Touch-LCD-5 | The **1024×600** variant — see §1.2 |
| USB-C cable | To the native-USB port (enumerates as `/dev/ttyACM0`) |
| CAN transceiver wiring | GPIO15 = TX, GPIO16 = RX |
| **Bus termination** | Must be enabled unless another node terminates |

### 1.2 Confirm your panel variant first

This board ships in 800×480 and 1024×600 variants with **completely different**
timings. Driving the wrong branch produces a permanently corrupted image that
does not respond to any porch, clock or polarity adjustment — this cost
significant time during development.

Check by watching the boot log: the GT911 driver prints its own configured
resolution.

```
Touch resolution: width=1024, height=600
```

If yours reports 800×480, change `DISPLAY_WIDTH`/`DISPLAY_HEIGHT` and
`_RGB_TIMING` in `src/tbgw/gui/lvgl_setup.py` to the vendor's 800×480 values
(porches 8/8/4, PCLK 16 MHz) — the file documents both branches.

### 1.3 Host tools

```bash
sudo apt-get install build-essential cmake ninja-build python3 python3-venv \
                     libusb-1.0-0-dev git
pipx install mpremote          # or: pip install --user mpremote
pipx install esptool           # or use the system 'esptool'
```

Python ≥ 3.10 is required by the firmware build system.

> **Prefer the `esptool` command over `esptool.py`.** On this board's native-USB
> port (no DTR/RTS reset lines) older esptool versions fail to reset it.
> v5.4.0+ works.

### 1.4 Disk space

The firmware build needs **5–8 GB** (toolchain plus objects). Point it at a
partition with real headroom:

```bash
export WORKDIR=/opt/tmp/tbgw_firmware_build
export IDF_TOOLS_PATH=/opt/tmp/esp_idf_tools_5.5.1
```

### 1.5 Network note

If large git clones fail mid-transfer with `GnuTLS recv error` or silent
truncation, force HTTP/1.1 — a known mitigation, and one this project needed:

```bash
git config --global http.version HTTP/1.1
git config --global http.postBuffer 1048576000
```

---

## 2. Build the firmware

```bash
./build/build_firmware.sh
```

The script clones both dependencies at pinned commits, applies
`build/patches/`, and invokes the build. Expect **30–60 minutes** on first run.

### 2.1 What the patches fix — do not skip them

| Patch | Without it |
|---|---|
| `micropython-esp32-twai-fixes.patch` | Build fails; requested bitrate is silently ignored (bus appears dead); every transmit times out ~1000× early |
| `lvgl-micropython-rgb-bus-fixes.patch` | The board **hard-locks** after a burst of LVGL redraws — whole-system freeze, no traceback, power cycle only |

### 2.2 If the build fails

| Symptom | Cause | Fix |
|---|---|---|
| Many simultaneous "broken pipe" / "invalid argument" across unrelated files | Memory pressure under parallel compile | `MAKEFLAGS=-j3` (or `-j2`) |
| `include could not find requested file` | `USER_C_MODULE=<git-url>` writes an unresolvable relative path | Use a local absolute path (the script does) |
| `unable to locate python version used in the ESP-IDF` | Post-build step ignores `IDF_TOOLS_PATH` | `ln -s $IDF_TOOLS_PATH/python_env ~/.espressif/python_env` |
| `app partition is too small` | Binary grew | The build auto-resizes and retries — **but see §5.3** |
| `No space left on device` | Build partition full | Move `WORKDIR` and `IDF_TOOLS_PATH` |

---

## 3. Flash the firmware

```bash
cd $WORKDIR/lvgl_micropython
source $IDF_TOOLS_PATH/python_env/idf5.5_py3.14_env/bin/activate

python -m esptool --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  --before default_reset --after hard_reset write_flash \
  --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0x0 build/lvgl_micropy_ESP32_GENERIC_S3-SPIRAM_OCT-16.bin
```

Verify:

```bash
mpremote connect /dev/ttyACM0 exec "import lvgl, CAN, gc; print('ok', gc.mem_free())"
```

Expect `ok` and roughly **5.4 MB** free. A much lower figure means PSRAM is not
correctly mapped.

### 3.1 If the board does not respond after flashing

1. **Physically unplug and replug.** The ESP32-S3's USB-Serial-JTAG peripheral
   can latch a "stay in download mode" flag in RTC memory that survives every
   *software* reset. No tool invocation can clear it.
2. If it still fails, the app may be crashing on boot — read the console
   passively (see `docs/measurement_guide.md` §0.3).

---

## 4. Prepare the filesystem

A freshly flashed board may have a corrupt or missing filesystem. Format it:

```bash
mpremote connect /dev/ttyACM0 exec "
import os, esp32
bdev = esp32.Partition.find(esp32.Partition.TYPE_DATA, label='vfs')[0]
os.VfsLfs2.mkfs(bdev)
os.mount(bdev, '/')
print('formatted:', os.listdir('/'))"
```

---

## 5. Deploy the application

### 5.1 Code and shared config

```bash
cd /path/to/tbgw_esp32

mpremote connect /dev/ttyACM0 mkdir :config
mpremote connect /dev/ttyACM0 cp src/boot.py :boot.py
mpremote connect /dev/ttyACM0 cp -r src/tbgw :tbgw

for f in can.json home_devices.json logs.json mqtt.json wifi.json; do
  mpremote connect /dev/ttyACM0 cp config/$f :config/$f
done
```

Note `main.py` is deliberately deployed **last** (§5.4).

### 5.2 Device-only secrets

Two files exist only on the board and are **never committed**.

`config/wifi.local.json`:

```json
{ "ssid": "YourSSID", "password": "YourPassword" }
```

`config/tb_gateway.json` — start from the committed placeholder and substitute
the real host and token:

```bash
python3 - <<'EOF'
import json
c = json.load(open("config/tb_gateway.json"))
c["thingsboard"]["host"] = "xxx"          # the board's view of the server
c["thingsboard"]["security"]["accessToken"] = "<GATEWAY_ACCESS_TOKEN>"
json.dump(c, open("/tmp/tb_gateway.device.json", "w"), indent=2)
EOF

mpremote connect /dev/ttyACM0 cp /tmp/wifi.local.json :config/wifi.local.json
mpremote connect /dev/ttyACM0 cp /tmp/tb_gateway.device.json :config/tb_gateway.json
```

> `host` must be the **server's LAN address**, never `localhost` — the board is
> a separate machine. Re-check it if the server's IP is assigned by DHCP.

### 5.3 The MQTT-broker connector

`config/tb_gateway.json` lists two connectors. The **MQTT Broker Connector is a
known hazard**: its `connect()` is a blocking socket call on the asyncio event
loop, so an unreachable broker stalls the *entire gateway* for the TCP timeout
(~60 s) on every retry — which manifests as GUI commands taking a minute to
reach the bus.

Unless you have a reachable broker, remove it:

```bash
python3 - <<'EOF'
import json
c = json.load(open("/tmp/tb_gateway.device.json"))
c["connectors"] = [x for x in c["connectors"] if x.get("type") != "mqtt"]
json.dump(c, open("/tmp/tb_gateway.device.json", "w"), indent=2)
EOF
```

### 5.4 Enable the gateway

Deploying `main.py` last means the board boots to a REPL until you are ready —
useful, because a misconfigured system can freeze fast enough to make the board
hard to reach.

```bash
mpremote connect /dev/ttyACM0 cp src/main.py :main.py
mpremote connect /dev/ttyACM0 exec "import machine; machine.reset()"
```

---

## 6. Verify

Watch the boot log passively (`docs/measurement_guide.md` §0.3). A healthy boot shows the expected gateway, GUI and network startup messages.

xxx

Checklist:

- [ ] Both devices reach `-> online`
- [ ] `heartbeat` appears every **30 s** (60 s indicates loop starvation)
- [ ] No `Traceback`, no `MemoryError`
- [ ] No `CAN TX failed` / `ETIMEDOUT` storm
- [ ] Screen shows the home list; tapping navigates
- [ ] Toggling a physical device updates the on-screen control
- [ ] Tapping a control actuates the physical device

### 6.1 Runtime watchdog

On ESP32 builds that provide `machine.WDT`, the gateway arms a 30-second
hardware watchdog after startup. It is fed only while the Gateway asyncio
loop, CAN thread, and enabled LVGL GUI all emit recent heartbeats. A frozen
worker therefore causes an automatic reboot; a disconnected CAN cable,
Wi-Fi outage, or ThingsBoard outage does **not** cause a reboot by itself.

The first 90 seconds are a bounded startup/calibration grace period. Normal
boot logging includes the reset source and WDT activation, for example:

```text
Boot reset cause: PWRON_RESET (1)
Runtime WDT armed: timeout=30000ms workers=['gateway', 'can', 'gui'] grace=90000ms
```

After a watchdog recovery, `Boot reset cause: WDT_RESET (...)` confirms the
previous boot stopped feeding it. Do not deliberately test this on a live
deployment: withholding feeds necessarily reboots the board and can disturb
the Windows/WSL serial-port mapping.

---

## 7. ThingsBoard server setup

Reference stack (kept outside the repo):

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: thingsboard
      POSTGRES_PASSWORD: postgres
    volumes: [tb-postgres-data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
  thingsboard-ce:
    image: thingsboard/tb-node:4.3.1.4
    depends_on:
      postgres: { condition: service_healthy }
    ports: ["8080:8080", "1883:1883"]
    environment:
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/thingsboard
volumes: { tb-postgres-data: }
```

```bash
docker compose up -d
```

Then, in the UI (`http://<host>:8080`) or via REST:

1. Create a tenant and tenant admin.
2. Create a device, e.g. `tbgw-esp32s3-bench`.
3. Set `additionalInfo.gateway = true` — **required**; without it sub-device
   auto-provisioning does not work.
4. Copy its access token into `config/tb_gateway.json`.

xxx

---

## 8. Configuration reference

| File | Purpose | Committed |
|---|---|---|
| `tb_gateway.json` | Server host/token, connectors, storage, `gui.enabled` | Placeholder only |
| `can.json` | Frame profile, devices, datapoint sets, writes, polling | Yes |
| `mqtt.json` | MQTT-broker connector mappings | Yes |
| `home_devices.json` | GUI catalogue: homes → devices → subtype | Yes |
| `logs.json` | Per-module log levels | Yes |
| `wifi.json` | Placeholder credentials | Yes |
| `wifi.local.json` | **Real** credentials | **Never** (gitignored) |

Adding a CAN device is a **configuration** change: add it to `can.json`
(address, datapoint set, polling, writes) and to `home_devices.json` for the
GUI. No Python changes. See `docs/can_connector_design.md`.

---

## 9. Recovery

### 9.1 Filesystem corrupted after a firmware update

**Symptom:** boot prints `The filesystem appears to be corrupted`.

**Cause:** the build auto-resized the app partition, which shifted the `vfs`
offset. The old filesystem no longer aligns.

**Fix:** reformat (§4) and redeploy (§5) — including both device-only files
from §5.2. There is no path that preserves the old contents.

### 9.2 Board frozen and unreachable

**Symptom:** no serial output; `mpremote` reports `could not enter raw repl`;
`/dev/ttyACM0` may disappear entirely.

**Cause:** a native module blocking while holding the GIL — the whole
interpreter is stopped, so no software reset can help.

**Fix:** physically unplug and replug.

If it freezes again immediately on boot, break the cycle by invalidating the
filesystem so `main.py` cannot run, then redeploy:

```bash
esptool --chip esp32s3 --port /dev/ttyACM0 --after hard_reset \
        erase-region 0x2E8000 0x10000
```

### 9.3 Completely erased flash

**Symptom:** console repeats `invalid header: 0xffffffff`.

**Fix:** reflash (§3), reformat (§4), redeploy (§5).

### 9.4 Devices never come online

Work through in order:

1. **Bus termination enabled?** CAN needs another node to acknowledge; an
   unterminated bus drives the controller to BUS_OFF.
2. Check controller state — `state` must be `running`:
   ```bash
   mpremote connect /dev/ttyACM0 exec "
   import sys; g = sys.modules['main'].gateway
   print(g.can_connector._driver.get_status())"
   ```
3. **Bitrate actually applied?** If the driver prints identical `CAN: timing`
   values regardless of the configured bitrate, the TWAI patch is missing.
4. xxx

### 9.5 GUI commands are slow or only land on screen change

Almost always the blocking MQTT-broker connector (§5.3). Confirm by checking the
heartbeat interval: 60 s instead of 30 s indicates the event loop is stalling.

---

## 10. Development workflow

Host tests need no hardware:

```bash
python3 -m pytest tests/host -q      # 320 tests
```

Redeploy a single module:

```bash
mpremote connect /dev/ttyACM0 cp src/tbgw/gui/theme.py :tbgw/gui/theme.py
mpremote connect /dev/ttyACM0 exec "import machine; machine.reset()"
```

Two rules that keep hardware results trustworthy:

- **`mpremote run` does not reset the board.** Hard-reset before every hardware
  experiment, one variable per power cycle.
- **Any serial contact disturbs a running deployment.** `mpremote exec` sends
  Ctrl-C, stopping `main.py`; `esp_idf_monitor` resets on connect unless given
  `--no-reset`. For live observation, capture passively or verify over the
  network.
