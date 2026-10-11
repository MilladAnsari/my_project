# Measurement guide

Executable procedure for populating the placeholder tables in
[`technical_report.md` §13](technical_report.md#13-measurements).

Each section here maps 1:1 onto a table there. Work through them in order —
later sections assume the instrumentation set up in earlier ones.

---

## 0. Before you start

### 0.1 Preconditions

- [ ] Board flashed with the firmware from `build/build_firmware.sh`
      (includes both patches in `build/patches/`)
- [ ] `tbgw/` and `config/` deployed (see `docs/installation.md`)
- [ ] `config/tb_gateway.json` on the board has the **real** ThingsBoard host
      and access token
- [ ] `config/wifi.local.json` on the board has real credentials
- [ ] **CAN bus terminated** and at least one other node powered
- [ ] ThingsBoard reachable: `docker compose up -d` in the stack directory
- [ ] Both devices reach `-> online` in the boot log

> **Termination is not optional.** CAN transmission requires acknowledgement by
> another node. An unterminated bus drives the error counters to BUS_OFF and
> every measurement below becomes meaningless. This cost real debugging time
> during development.

### 0.2 The two rules that keep results valid

**Rule 1 — hard-reset between experiments.** `mpremote run script.py` does
**not** reset the board; it executes in whatever interpreter state already
exists. Re-initialising live display hardware on top of itself produces hard
faults that look like evidence about the parameter under test and are not.

```bash
mpremote connect /dev/ttyACM0 exec "import machine; machine.reset()"
sleep 8
```

**Rule 2 — do not touch the serial port during a live run.** `mpremote exec`
sends Ctrl-C, which raises `KeyboardInterrupt` inside `main.py` and stops the
gateway; `esp_idf_monitor` resets the board on connect unless given
`--no-reset`. For anything longer than a few seconds, capture **passively**
(§0.3) or verify over the network via ThingsBoard's REST API.

### 0.3 Passive capture harness

Records the console without disturbing the running system, and reconnects across
the USB re-enumeration a reset causes.

```bash
cat > /tmp/capture.py <<'EOF'
import serial, sys, time
path, seconds = sys.argv[1], float(sys.argv[2])
f = open(path, 'ab', buffering=0)
t0 = time.time()
while time.time() - t0 < seconds:
    try:
        s = serial.Serial('/dev/ttyACM0', 115200, timeout=1)
        f.write(b'\n--- connected ---\n')
        while time.time() - t0 < seconds:
            d = s.read(400)
            if d:
                f.write(d)
        s.close()
    except Exception:
        time.sleep(0.5)
f.close()
EOF

python3 /tmp/capture.py /tmp/run.log 600 &     # 10 minutes
```

### 0.4 Raising log verbosity

`config/logs.json` sets `"gui": "WARNING"`, so INFO-level GUI logs are invisible
by default. For §4 and §6 raise it, and **put it back afterwards** — GUI logging
is chatty enough to perturb timing.

```bash
mpremote connect /dev/ttyACM0 exec "
import json
c = json.load(open('config/logs.json'))
c['gui'] = 'INFO'
json.dump(c, open('config/logs.json','w'))
print(c)"
```

### 0.5 Recording template

For every run record: firmware build date, git commit (`git rev-parse --short HEAD`),
PCLK setting, whether the GUI was on a detail screen or the home list, ambient
temperature if thermal effects are suspected, and ThingsBoard reachability.
Results without this context are not comparable across runs.

---

## 1. Live introspection

`main.py` publishes the running gateway at module scope specifically for this:

```bash
mpremote connect /dev/ttyACM0 exec "
import sys
g = getattr(sys.modules.get('__main__'), 'gateway', None) or \
    getattr(sys.modules.get('main'), 'gateway', None)
print('devices  :', g.device_registry.count())
print('states   :', g.device_states.snapshot())
print('uplink   :', g.uplink_client.is_connected())
print('can      :', g.can_connector.is_connected())
print('can stat :', g.can_connector._driver.get_status())
"
```

None of these calls touch LVGL, so they are safe while the GUI runs. **They do
stop `main.py`** (Rule 2) — treat this as a terminal action for that run.

---

## 2. Boot timing → §13.1

Timestamps come from the log itself; no instrumentation needed.

```bash
rm -f /tmp/boot.log
python3 /tmp/capture.py /tmp/boot.log 60 &
sleep 1
mpremote connect /dev/ttyACM0 exec "import machine; machine.reset()" 2>/dev/null
wait
grep -E "boot.py|tbgw starting|Wi-Fi: connected|NTP sync|TWAI started|-> online|GUI running" /tmp/boot.log
```

Take **five** resets and report median and spread; Wi-Fi association varies most.

| Phase | Derive from |
|---|---|
| Reset → `boot.py` | ROM banner → `tbgw boot.py:` line |
| Wi-Fi associate | `connecting to` → `Wi-Fi: connected` |
| NTP sync | `Wi-Fi: connected` → `NTP sync OK` |
| CAN → first device online | `TWAI started` → first `-> online` |
| GUI init → first frame | `GUI thread started` → `GUI running` |
| Total | ROM banner → last of the above |

> NTP corrects the clock mid-boot, so timestamps **before** sync are on the
> 2000-01-01 epoch. Measure pre-NTP phases by their deltas, not absolute values.

**Offline variant (requirement F7).** Repeat with Wi-Fi unavailable (wrong SSID
in `wifi.local.json`) and confirm CAN and GUI still reach operational state.
Record the added delay from the bounded Wi-Fi retry.

---

## 3. Memory → §13.2

### 3.1 Checkpoints

`boot.py` already prints free heap at start. For the rest:

```bash
mpremote connect /dev/ttyACM0 exec "
import gc
gc.collect()
print('free', gc.mem_free(), 'alloc', gc.mem_alloc())"
```

Always `gc.collect()` first — without it you measure garbage, not usage. During
development an uncollected reading of 111 KB became 402 KB after a collect.

### 3.2 Attribution

To attribute consumption per subsystem, boot with parts disabled via
`config/tb_gateway.json` (`gui.enabled`, and by removing connector entries), and
difference the readings:

| Configuration | Free heap |
|---|---|
| Gateway only, no CAN, no GUI | ☐ |
| \+ CAN connector | ☐ |
| \+ GUI | ☐ |

The GUI delta should be dominated by LVGL draw buffers:
`1024 × _DRAW_BUFFER_ROWS × 2` bytes each, ×2 buffers ≈ **410 KB** at the
current 100 rows. If the delta is closer to 2.4 MB, full-screen buffers have
been reintroduced — that is defect A18 and it will eventually kill the display
pump.

### 3.3 Leak test

Sample hourly for 24 h. Any monotonic downward trend is a leak.

```bash
mpremote connect /dev/ttyACM0 exec "
import gc, time
for _ in range(60):
    gc.collect()
    print(time.time(), gc.mem_free())
    time.sleep(60)"
```

Note this holds the serial port and stops `main.py`; prefer deriving the trend
from a §8 soak log instead if you want the gateway running.

---

## 4. Display → §13.3

### 4.1 Frame rate

LVGL's own counter is the least invasive source:

```python
import lvgl as lv
d = lv.display_get_default()
# sample lv.timer_handler() call counts / elapsed ms over a fixed window
```

Alternatively, and often more honestly: film the panel at a known camera frame
rate and count distinct rendered frames during a switch animation.

Measure three conditions:

1. **Static** — home list, nothing animating.
2. **Animating** — repeatedly toggling a switch (this is the burst-of-flushes
   case that historically triggered failures).
3. **Transition** — navigating home list ↔ detail.

### 4.2 Touch latency

Film the panel and the finger together at ≥ 120 fps and count frames from
contact to first visual change. Repeat 10×, report median.

### 4.3 Artifacts

Inspect at the working pixel clock (21 MHz) for: horizontal image shift,
ghosting/smearing, flicker, colour scrambling. Record a photograph for any
artifact seen.

> Interpretation: ghosting previously appeared at **12 MHz**, not at 21 MHz —
> the panel misbehaves *below* its designed clock. If ghosting appears at
> 21 MHz, suspect something other than clock rate.

### 4.4 Clock sweep (optional)

Vary `_RGB_TIMING["freq"]` in `lvgl_setup.py` across 12/16/21 MHz, one value per
power cycle (Rule 1), and record artifacts and FPS at each.

| PCLK | FPS static | FPS animating | Artifacts | Stable? |
|---|---|---|---|---|
| 12 MHz | ☐ | ☐ | ☐ | ☐ |
| 16 MHz | ☐ | ☐ | ☐ | ☐ |
| 21 MHz | ☐ | ☐ | ☐ | ☐ |

---

## 5. CAN → §13.4

### 5.1 Controller counters

```bash
mpremote connect /dev/ttyACM0 exec "
import sys
g = getattr(sys.modules.get('__main__'), 'gateway', None) or sys.modules['main'].gateway
print(g.can_connector._driver.get_status())"
```

Fields: `state`, `bus_error_count`, `tx_error_counter`, `rx_error_counter`,
`tx_failed_count`, `rx_missed_count`, `arb_lost_count`, `msgs_to_tx`.

Sample at boot, +1 h, +24 h. On a terminated bus with healthy nodes all error
counters should stay at 0; `state` must remain `running`.

### 5.2 Read round-trip

Enable `can_connector` DEBUG (it already is in `config/logs.json`) and time the
read → reply pair in the captured log. Measure twice:

- **Idle** — GUI on the home list, no interaction.
- **Active** — continuously tapping switches on a detail screen.

The delta is the GUI's interference with the CAN thread and is the direct test
of the 33 ms pump choice. A large gap argues for revisiting
`_TIMER_HANDLER_INTERVAL_MS`.

### 5.3 Standalone bus probe

xxx

> Always pass `timeout=`. Omitting it takes a `portMAX_DELAY` path that blocks
> **holding the GIL** and can freeze the whole board (defect D1).

---

## 6. End-to-end latency → §13.5

Five paths, each timed independently.

### 6.1 Physical change → GUI

Toggle the light at its physical switch; film panel and switch together. Count
frames to the on-screen change.

### 6.2 Physical change → ThingsBoard

Toggle physically, then poll telemetry timestamps:

```bash
# Substitute your own tenant-admin credentials; do not commit them.
TB_HOST=xxx
TB_USER='<tenant-admin-email>'
TB_PASS='<tenant-admin-password>'

TOKEN=$(curl -s -X POST http://$TB_HOST:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$TB_USER\",\"password\":\"$TB_PASS\"}" | jq -r .token)

curl -s -H "X-Authorization: Bearer $TOKEN" \
  "http://$TB_HOST:8080/api/plugins/telemetry/DEVICE/<deviceId>/values/timeseries?keys=pole1" | jq
```

Compare the reported `ts` against wall-clock time of the physical action. This
also validates that NTP-derived timestamps are correct, not merely present.

### 6.3 GUI tap → relay

Film the panel and the physical light together; count frames from tap to the
light changing.

### 6.4 GUI tap → GUI confirmation

From the same footage, count to the switch settling in its new position. This is
necessarily longer than §6.3 by one event/poll interval — that gap is the
designed cost of the no-optimistic-update rule, not a defect.

### 6.5 ThingsBoard RPC → relay

```bash
curl -s -X POST -H "X-Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://$TB_HOST:8080/api/plugins/rpc/oneway/<deviceId>" \
  -d '{"method":"setPoleState","params":{"pole":1,"state":true}}'
```

Time from request to the light changing.

---

## 7. CPU utilisation → §13.6

MicroPython exposes no per-thread CPU accounting, so use proxies.

### 7.1 Scheduling-health proxy (cheap, sensitive)

The gateway heartbeat is configured at 30 s (`_HEARTBEAT_INTERVAL_S`). Extract
its actual interval:

```bash
grep "gateway heartbeat" /tmp/run.log | awk '{print $2}'
```

| Observed interval | Interpretation |
|---|---|
| ~30 s | Loop healthy |
| ~60 s | **2× starvation** — this is exactly what a blocking MQTT connect produced |
| irregular | Intermittent blocking; correlate with GUI activity |

### 7.2 Loop-iteration counters

For a quantitative figure, add temporary counters to the CAN thread loop and the
LVGL refresh timer, print them once per minute, and compare idle vs. GUI-active
rates. **Remove the instrumentation afterwards** — it perturbs what it measures.

### 7.3 FreeRTOS-level accounting (optional)

`CONFIG_FREERTOS_USE_TRACE_FACILITY` + `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS`
give real per-task numbers, including the `rgb_bus` copy task and the TWAI alert
task that are invisible from Python. This requires a firmware rebuild and will
grow the binary — check partition headroom first (§9.4 of the report).

---

## 8. Stability soak → §13.7

24 hours, gateway running, **GUI exercised**.

```bash
python3 /tmp/capture.py /tmp/soak.log 86400 &
```

An idle soak does **not** exercise the flush path that produced the original
freezes. Interact with the GUI periodically — or automate it by scripting
periodic ThingsBoard RPCs (§6.5), which drives the CAN and uplink paths even
without a person present.

Afterwards:

```bash
grep -c "gateway heartbeat" /tmp/soak.log          # expect ~2880 at 30 s
grep -cE "Traceback|MemoryError"  /tmp/soak.log    # expect 0
grep -c "CAN TX failed"           /tmp/soak.log    # expect 0
grep -c "Wi-Fi"                   /tmp/soak.log    # reconnect count
grep -c "Uplink connected"        /tmp/soak.log    # reconnect count
grep -c "storage.*full\|drop"     /tmp/soak.log    # expect 0
ls -la /tmp/soak.log                               # size is itself a signal
```

> **Log volume is a health metric.** A bus fault once produced **33 MB** in
> minutes and blocked the board on serial I/O. A soak log far larger than
> expected indicates a retry storm even if no single line looks alarming.

Record any freeze with: time since boot, what was on screen, what was being
touched, and whether the heartbeat continued (whole-VM freeze vs. GUI-only —
this distinction is what separates defect classes; see report §11.1).

---

## 9. Power → §13.8

Inline USB power meter, or a bench supply with current readout, on the 5 V feed.

| State | How to reach it |
|---|---|
| Idle, backlight on | Boot, leave on home list, do not touch |
| Idle, backlight off | `ch422g.set_backlight(False)` via REPL |
| GUI interaction | Continuous switch tapping |
| Peak | Wi-Fi transmit during a telemetry burst plus a screen transition |

Report mean and peak over ≥ 60 s per state. Note whether Wi-Fi power-save is
active — it materially affects idle draw.

---

## 10. Reporting

For each populated table in report §13, replace every ☐ and rewrite the
*"Analysis placeholder"* paragraph beneath it with real interpretation. State
plainly:

- which targets were met and which were missed;
- for each miss, whether the cause is understood;
- any measurement whose result contradicts a claim in this report — those are
  the most valuable outcomes of the campaign, and §11 exists precisely because
  several earlier claims were wrong.

Attach the raw logs alongside the report; the tables are summaries, not
evidence.
