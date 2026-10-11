# tbgw_esp32 architecture

Port of ThingsBoard IoT Gateway (MQTT + CAN connectors, with telemetry,
attributes and RPC) to MicroPython on an ESP32-S3-WROOM-1-N16R8
(Waveshare ESP32-S3-Touch-LCD-5B), with an LVGL local monitoring/control
GUI.

## Refined architecture: 3 workers, 2 cores

| Core | Worker | Notes |
|---|---|---|
| **Core 0** (`CORE_GUI`) | LVGL GUI thread | Dedicated so display rendering and touch response stay smooth regardless of network/CAN-bus load. |
| **Core 1** (`CORE_GATEWAY` / `CORE_CAN`) | asyncio gateway core (MQTT uplink client, MQTT connector, RPC dispatcher, storage) **+** dedicated CAN RX/TX thread | CAN reception is hardware-ISR-buffered at the driver level regardless of which core polls the queue, so the Python-level CAN thread mostly blocks waiting and coexists fine with the asyncio loop. |

**Open verification item**: MicroPython's public `_thread.start_new_thread()`
on the esp32 port does not currently expose an explicit core-affinity
argument. Whether threads actually land on the intended core depends on
how the firmware build's `xTaskCreatePinnedToCore` calls are parameterized
(stock esp32 port defaults, the vendored TWAI module, and the LVGL
binding used may each behave differently). `tbgw.core.constants` documents
the *intended* assignment (`CORE_GUI = 0`, `CORE_GATEWAY = CORE_CAN = 1`)
as the single place this gets wired in once confirmed on real hardware.

This is FreeRTOS task affinity, not parallel Python execution: MicroPython's
GIL still serializes Python bytecode. The value is isolation of the actual
FreeRTOS tasks and native display/CAN/socket waits; it does not remove the
need to keep callbacks short or make LVGL thread-safe.

## Why CAN needs custom firmware

Mainline MicroPython has no `machine.CAN`/TWAI class for the ESP32 port
(as of the current stable release line, `machine.CAN` exists only for
STM32). This project vendors `straga/micropython-esp32-twai`, a
`USER_C_MODULES` component wrapping ESP-IDF's `twai` driver with
confirmed ESP32-S3 support, and builds custom firmware via
`build/build_firmware.sh`. This is not optional or deferrable: it's a
firmware-build-time dependency for the whole CAN connector, independent
of which Python-level architecture is chosen.

**Verified working, Phase 1 (hardware bring-up)**: the combined
LVGL+MicroPython+TWAI firmware described here has been built, flashed,
and confirmed on real ESP32-S3-Touch-LCD-5B hardware -- `import CAN` and
`import lvgl` both succeed, PSRAM is correctly mapped (~8.3MB free
heap), and the `CAN` object opens the target bus without
error (`state()` reports `TWAI_STATE_RUNNING`). `build/build_firmware.sh`
now reflects the exact, tested recipe (LVGL and TWAI built together via
`lvgl_micropython`'s own build system, not a separate stock-MicroPython
build as earlier drafts of this document assumed), including a small
required patch to straga's module (`build/patches/`, see that script's
comments) and the console-routing flags needed to use a single USB cable
for both flashing and the REPL. See `docs/hardware_bringup_checklist.md`
for what's now confirmed vs. still open (display/touch pin wiring is
verified against vendor source but not yet applied to code -- Phase 5).

## Package layout

```
tbgw_esp32/
├── LICENSE, NOTICE, .gitignore
├── build/            build_firmware.sh, partitions.csv
├── config/           tb_gateway.json, mqtt.json, can.json, logs.json, home_devices.json
├── docs/             architecture.md (this file), pin_map.md, testing.md,
│                     hardware_bringup_checklist.md
├── src/
│   ├── boot.py       minimal early init (runs on every hard reset)
│   ├── main.py       integration entry point (wires GUI/CAN/gateway together)
│   └── tbgw/
│       ├── core/         gateway_service, uplink_client, mqtt_client_adapter
│       │                 (adapter + host-testable mock, mirrors twai_driver.py),
│       │                 rpc_dispatcher, device_registry, config_loader, constants
│       ├── connectors/
│       │   ├── base.py   Connector interface
│       │   ├── mqtt/     mqtt_connector, json/bytes uplink converters,
│       │   │             mqtt_downlink_converter (external-broker RPC/attribute writes)
│       │   └── can/      can_connector, twai_driver (adapter + host-testable
│       │                 mock, added in Phase 2), uplink/downlink converters
│       ├── storage/      base, memory_storage
│       ├── gui/          device_catalog, ch422g, lvgl_setup, gui_app,
│       │                 screens/ (home_list, home_detail), widgets/
│       │                 (light_tile, thermostat_tile, blind_tile, status_bar),
│       │                 presentation (pure state->display logic, host-testable)  (Core 0)
│       └── utils/        logger, queue, event, tb_utility, safe_eval
└── tests/host/       CPython-runnable tests for hardware-independent logic
```

## Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton, build tooling, config files, `utils/`, `storage/`, `boot.py`/`main.py` scaffolding, docs, host tests | **Done** |
| 2 | CAN connector (TWAI wrapper, uplink/downlink converters, CAN thread) | **Done** |
| 3 | MQTT connector, ThingsBoard Gateway-API uplink client, `TBGatewayService`, RPC dispatcher, device registry | **Done** |
| 4 | LVGL smart-home GUI (device catalog, state store, local-command path, `gui_app`, screens, widgets), Core 0 bring-up | **Done** |
| 5 | Final integration pass, docs polish, end-to-end test notes | **Done (this checkout)** |

> **This document is now partly historical.** It records the design as
> planned, before hardware bring-up. Bring-up invalidated a number of the
> assumptions below -- most consequentially the panel resolution, the
> vendored CAN/LVGL APIs, and the belief that a dedicated thread isolates
> blocking work under MicroPython's GIL. For the design *as built*, the
> alternatives that were considered and rejected, and the full catalogue
> of what turned out to be wrong, see **`docs/technical_report.md`**.
>
> The phase table above is accurate as a record of the original plan.
> Phases 1-5 were subsequently re-executed against real hardware, and the
> CAN connector in particular was redesigned (byte-offset -> register/
> selector) -- see `docs/can_connector_design.md` and technical report
> section 6.

Project status: **working and validated on real hardware**; development
frozen. CAN events reach ThingsBoard with real timestamps, ThingsBoard
RPCs actuate real relays, and the local GUI monitors and controls the
same devices. Verified by 324 host tests (`docs/testing.md`) plus
end-to-end hardware validation. Quantitative measurements were not
performed -- see technical report section 13 and
`docs/measurement_guide.md`.

Every not-yet-implemented module in this checkout raises
`NotImplementedError` from a clearly labeled docstring describing its
planned design, and `main.py` is written to detect this and keep the
board alive (heartbeat loop) rather than crash, so the skeleton can
already be flashed and observed on real hardware before every phase lands.
As of Phase 4, the GUI hard-depends on a real `TBGatewayService` (for
`send_local_command()`/`device_states`/etc), so `main.py`'s `_start_gui()`
explicitly refuses to start the GUI thread if the gateway itself failed
to start, rather than starting a GUI thread that would crash on first use.

## Connector ownership split (Phase 3)

`TBGatewayService.load_connectors()` only instantiates connector types in
its `SUPPORTED_CONNECTOR_TYPES` registry (currently just `"mqtt"`). The
CAN connector is deliberately NOT loaded there -- it's self-managed by
`main.py`, which starts it directly on its own dedicated thread (see
`can_connector.py`'s stack-size/threading requirements) and passes the
same `TBGatewayService` instance in as `gateway=` so CAN can still call
`send_to_storage()`/`connect_device()`/`disconnect_device()` on it. Any
other connector type present in `tb_gateway.json`'s `connectors` list
that isn't in `SUPPORTED_CONNECTOR_TYPES` is silently skipped by
`load_connectors()` with a debug log, on the assumption it's either
self-managed elsewhere (like CAN) or not yet supported by this port.

## RPC acknowledgement scope (Phase 3)

This port's RPC replies to ThingsBoard confirm only that a request was
successfully routed to a connector for a recognized device+method -- NOT
that the physical device (over CAN or the external MQTT broker) actually
executed it and returned a real result. Both `on_rpc_request()`
implementations (CAN, MQTT) are fire-and-forget. A genuine device-side
request/response round trip is a natural future enhancement, not
attempted in this port -- see can_downlink_converter.py's and
rpc_dispatcher.py's docstrings for the same caveat stated at its two
other touch points.

## GUI architecture (Phase 4)

### Why LVGL needs custom firmware too

Same category of constraint as CAN (see above): mainline MicroPython has
no LVGL binding at all. This port targets **`lvgl_micropython`**
(github.com/lvgl-micropython/lvgl_micropython), a from-scratch
MicroPython+LVGL build system that ships C modules for exactly this
display family -- `rgb_bus` (RGB565 parallel LCD, matches the board's
ST7262 panel) and `gt911` (I2C touch). This is a build-time dependency,
not a pip-installable package.

**Integration risk, flagged explicitly (same treatment as the TWAI
native-API risk in twai_driver.py)**: `lvgl_micropython` is its own
top-level build system (`make.py esp32 BOARD=... DISPLAY=rgb_display
INDEV=gt911 ...`), separate from both stock MicroPython's build and from
how `build_firmware.sh` currently vendors `straga`'s TWAI module via
`USER_C_MODULES` into a stock MicroPython/ESP-IDF build. Combining CAN
support into an `lvgl_micropython` build has NOT been verified as part of
this project -- the two most likely paths are (a) vendoring `straga`'s
TWAI module as an additional `USER_C_MODULES` entry into
`lvgl_micropython`'s own build invocation (it uses the same underlying
ESP-IDF `USER_C_MODULES` mechanism, so this is plausible but unconfirmed),
or (b) vendoring `lvgl_micropython`'s relevant C modules into a stock
MicroPython build instead. `build/build_firmware.sh` documents option
(a) as the assumed path with an explicit `# CONFIRM` marker; resolving
this is the top open item before real hardware bring-up.

### CH422G: reset/backlight, pure Python

The CH422G IO-expander (I2C, addresses `0x20`-`0x27`) gates LCD reset,
backlight on/off, and SD_CS -- unlike CAN/LVGL, this needs no C module at
all, since it's just a handful of I2C register writes. `tbgw.gui.ch422g`
is a small hand-written driver, exercised by host tests against a mock
I2C bus (same adapter-testability pattern used everywhere else in this
project).

### The "no optimistic GUI updates" rule (your three sync scenarios)

All three scenarios you described -- physical change, GUI-issued command,
and ThingsBoard-issued RPC -- converge on exactly one rule: **a command
changes the bus, and only the resulting bus EVENT (the device reporting
its own new state) is allowed to update the GUI or ThingsBoard.** The GUI
never flips a toggle immediately on tap; it shows the command as
in-flight until the real state-change event arrives back through the
same pipeline already built in Phase 2/3. This has two consequences for
the design:

1. **A new local-command path.** GUI-issued commands need to reach a
   connector's `on_rpc_request()` directly, bypassing `RpcDispatcher`'s
   ThingsBoard-ack bookkeeping (there's no ThingsBoard `request_id` and no
   reply topic for a locally-issued command). `RpcDispatcher.dispatch_local_command()`
   and `TBGatewayService.send_local_command()` add this, reusing the
   *same* device_registry lookup and connector interface Phase 2/3 already
   built -- no changes to either connector were needed.
2. **A live device-state cache the GUI can read.** `TBGatewayService.send_to_storage()`
   (called by every connector on every uplink event, already existing
   since Phase 3) now ALSO updates `self.device_states`, a new
   `DeviceStateStore` (thread-safe, mirrors `DeviceRegistry`'s locking
   pattern) that merges each event's `attributes`/`telemetry` into a
   per-device latest-known-state dict. The GUI thread (Core 0) polls this
   via a thread-safe snapshot; it never touches connector or storage
   internals directly.

Cross-thread command flow: GUI thread (Core 0) -> `gateway.send_local_command()`
(thread-safe enqueue, same pattern as `connect_device()`/`disconnect_device()`
from Phase 3) -> `_local_command_loop` asyncio task (Core 1, gateway
thread) -> `RpcDispatcher.dispatch_local_command()` -> connector's
`on_rpc_request()`. This keeps every connector/MQTT-client/CAN-bus touch
confined to the Core 1 gateway thread, consistent with the concurrency
rules established in Phase 3.

### Device catalog (`config/home_devices.json`)

A new config file, separate from `can.json`/`mqtt.json`'s wire-level
byte/topic mapping, describing what the GUI should render: homes (mapped
1:1 to ThingsBoard Assets) containing devices, each with a `type`
(`light`/`thermostat`/`blind`), a `subtype` (e.g. a light's gang/key
count), which bus it's on (`can`/`mqtt`), and a `deviceName` that MUST
match the corresponding `deviceName` used in that connector's
`dataMapping`/`requestsMapping` entries, so `DeviceStateStore` lookups by
name just work. `tbgw.gui.device_catalog` loads and validates this file;
it has no dependency on LVGL and is fully host-testable.

### Device control model

- **Light** (N-gang/key): each key is an independent on/off toggle.
  Local command: `{"method": "setKeyState", "params": {"key": i, "state": bool}}`.
- **Thermostat**: `mode` is `"manual"` or `"auto"`. In manual mode, `fanSpeed`
  is one of `off`/`low`/`medium`/`high`. In auto mode, `setpoint` is a
  numeric target temperature. Local commands: `{"method": "setMode", ...}`,
  `{"method": "setFanSpeed", ...}`, `{"method": "setSetpoint", ...}`.
- **Blind**: `{"method": "setPosition", "params": {"position": "open"|"close"}}`.

These method names are a convention this port defines (not part of the
ThingsBoard Gateway API itself); they must match the `method` field
connector-side `serverSideRpc` mapping entries in `can.json`/`mqtt.json`
key off of, so a locally-issued command and a ThingsBoard-issued RPC for
the same action resolve to the same downlink converter.

## MicroPython gaps this port works around

See the module docstrings in `tbgw/utils/` and `tbgw/storage/` for the
specifics; summarized:

| CPython/upstream dependency | Replacement in this port |
|---|---|
| `logging` | `tbgw.utils.logger.TbLogger` |
| `queue.Queue` | `tbgw.utils.queue.Queue` |
| `threading.Event` | `tbgw.utils.event.Event` |
| `abc.ABC`/`abstractmethod` | `raise NotImplementedError` convention |
| `uuid` | `tbgw.utils.tb_utility.generate_id()` |
| `re.findall` / lookaround (used by upstream for topic/device-name extraction) | `tbgw.utils.tb_utility.topic_matches()` / `render_template()` (no general regex engine needed for the cases this project actually requires) |
| `python-can` (SocketCAN) | vendored `straga/micropython-esp32-twai` + `tbgw.connectors.can` (Phase 2) |
| `paho-mqtt` | `umqtt`-based client, wrapped by `tbgw.core.uplink_client` (Phase 3) |
| file/SQLite persistent storage | `tbgw.storage.memory_storage.MemoryEventStorage` only (persistence is an explicit non-goal for v1) |
| LVGL / GUI toolkit | vendored `lvgl_micropython` (`rgb_bus` + `gt911` C modules), `tbgw.gui` (Phase 4) |
| IO expander access (CH422G) | hand-written `tbgw.gui.ch422g` (plain I2C register writes, no C module needed) |
