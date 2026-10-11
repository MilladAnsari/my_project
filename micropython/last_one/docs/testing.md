# Testing guide

## Running the tests

```bash
cd tests/host  # or run from repo root, both work -- see conftest.py
python3 -m unittest discover -s tests/host -v
# or, if you have pytest available (not required):
python3 -m pytest tests/host -v
```

**324 tests, all passing on host CPython, zero real hardware/broker/bus
required.** This is possible because every module that would normally
need real hardware follows the same pattern established in Phase 2:
isolate the hardware-specific API behind a small adapter class, and
provide a software mock that activates automatically whenever the real
module isn't importable (which is always true on a desktop interpreter).

| Adapter | Real backend | Mock (auto-selected on host) |
|---|---|---|
| `tbgw.connectors.can.twai_driver.TwaiDriver` | `straga/micropython-esp32-twai`'s native `twai` module | `MockTwaiBus` |
| `tbgw.core.mqtt_client_adapter.MqttClientAdapter` | `umqtt.robust`/`umqtt.simple` | `MockMqttClient` |
| `tbgw.gui.ch422g.CH422G` | real I2C bus | any object with `.writeto()`/`.readfrom()` (tests pass a `FakeI2C`) |

## What's covered, by area

| Area | Test file(s) | What's exercised |
|---|---|---|
| Core utils (logger, queue, event, tb_utility, safe_eval) | `test_logger.py`, `test_queue.py`, `test_event.py`, `test_tb_utility.py` | Every public function, including edge cases (malformed input, overflow policies, blocking timeouts) |
| Storage | `test_memory_storage.py` | Put/get-pack/ack lifecycle, both overflow policies, retry-after-failure via `discard_pending_pack()` |
| CAN connector | `test_can_uplink_converter.py`, `test_can_downlink_converter.py`, `test_twai_driver_mock.py`, `test_can_connector.py` | Byte-offset extraction/packing, `eval()`-based downlink expressions (both strict and non-strict), `setFanLevel` Knob RPC sequencing, confirmed target and physical `fanLevelActual` derivation, full connector lifecycle against `MockTwaiBus` including malformed-frame resilience |
| MQTT connector | `test_json_uplink_converter.py`, `test_bytes_uplink_converter.py`, `test_mqtt_downlink_converter.py`, `test_mqtt_client_adapter.py`, `test_mqtt_connector.py` | JSON/bytes payload conversion, topic-template device-name extraction, full connector lifecycle against `MockMqttClient` |
| ThingsBoard uplink | `test_uplink_client.py` | Every Gateway-API topic (connect/disconnect/telemetry/attributes/rpc/attribute-request+response), inbound message dispatch |
| Gateway orchestration | `test_device_registry.py`, `test_rpc_dispatcher.py`, `test_gateway_service.py` | RPC/attribute-update routing (including the local-command path), connector loading (correctly skipping self-managed "can"), the full asyncio task set running together |
| GUI-adjacent, LVGL-independent | `test_device_catalog.py`, `test_device_state_store.py`, `test_ch422g.py`, `test_presentation.py` | Config validation, state merging, CH422G register logic, every state->display and command-building function |
| Cross-file / end-to-end | xxx |

## What's deliberately NOT covered, and why

- **Real LVGL rendering.** There is no way to run LVGL on a desktop host
  without the native library. Everything with a plausible desktop
  equivalent (state formatting, command building) was pulled out into
  `tbgw.gui.presentation` specifically so it COULD be tested; the
  remaining LVGL-touching files (`lvgl_setup.py`, `gui_app.py`,
  `screens/`, `widgets/`) are not unit-tested and say so in their own
  docstrings.
- **The native TWAI/umqtt/lvgl_micropython APIs' exact current shape.**
  Every adapter's "real backend" branch (`_NativeTwaiDriver`,
  `_NativeMqttClientAdapter`, `lvgl_setup.py`'s hardware calls) is
  exercised only in the sense that the mock fallback path is proven to
  work identically at the call-site level; the actual native calls are
  marked `# CONFIRM`/`⚠️` and are covered by
  `docs/hardware_bringup_checklist.md` instead of a test.
- **Timing/performance under real bus or network load.** Host tests use
  generous `asyncio.sleep()` windows and a software mock that never
  blocks the way a real socket or bus does; they prove *correctness* of
  the logic, not real-world latency or throughput.
- **Concurrency correctness under real OS thread scheduling.** Host
  tests run against CPython's `_thread` (which exists and behaves
  similarly, but isn't the ESP32 FreeRTOS scheduler); race conditions
  that only manifest under FreeRTOS's specific preemption behavior
  wouldn't necessarily show up here. The locking discipline itself
  (documented per-module: `DeviceRegistry`, `DeviceStateStore`,
  `MemoryEventStorage`, `Queue`) was designed to be correct regardless of
  scheduler, but "designed correct" isn't the same claim as "tested under
  real scheduling."

## Adding a test for new functionality

Follow the existing pattern: if what you're adding touches real hardware
or an external service, look for (or add) an adapter + mock pair first
(see `twai_driver.py` or `mqtt_client_adapter.py` as templates), then
test against the mock. If it's pure logic (formatting, parsing,
validation, command building), it doesn't need an adapter at all --
follow `tb_utility.py`/`presentation.py`'s pattern of keeping it
free of any hardware import in the first place.
