"""tbgw.core.constants
========================

Central place for values shared across core/, connectors/, and gui/ so
Phase 2-5 modules don't hardcode magic strings independently.
"""

# --- Gateway MQTT API topics (ThingsBoard side) -----------------------------
# Confirmed against https://thingsboard.io/docs/reference/gateway-mqtt-api/
GATEWAY_TOPIC_CONNECT = "v1/gateway/connect"
GATEWAY_TOPIC_DISCONNECT = "v1/gateway/disconnect"
GATEWAY_TOPIC_TELEMETRY = "v1/gateway/telemetry"
GATEWAY_TOPIC_ATTRIBUTES = "v1/gateway/attributes"
GATEWAY_TOPIC_ATTRIBUTES_REQUEST = "v1/gateway/attributes/request"
GATEWAY_TOPIC_ATTRIBUTES_RESPONSE = "v1/gateway/attributes/response"
GATEWAY_TOPIC_RPC = "v1/gateway/rpc"

# --- Defaults ----------------------------------------------------------------
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_KEEPALIVE_S = 120
DEFAULT_RPC_TIMEOUT_S = 10
DEFAULT_QOS = 1
DEFAULT_STORAGE_MAXSIZE = 1000
DEFAULT_MIN_PACK_SEND_DELAY_MS = 50
DEFAULT_MAX_PACK_SIZE = 50

# --- Core assignment ----------------------------------------------------------
# See docs/architecture.md, "Refined architecture: 3 workers, 2 cores".
# NOTE: MicroPython's public `_thread.start_new_thread()` on the esp32 port
# does not currently accept an explicit core-affinity argument; whichever
# core a new thread actually lands on depends on the firmware build
# (stock esp32 port vs. the vendored straga TWAI module vs. the LVGL
# binding). These constants document *intent* and are threaded through
# main.py / gui_app.py / can_connector.py so that if/when a firmware-level
# pinning hook is available (e.g. a patched _thread, or creating the task
# directly against the IDF API from a C module) there is exactly one place
# to wire it in. TREAT AS UNVERIFIED until confirmed on real hardware.
# A build without the project firmware patch fails explicitly at startup on
# ESP32 rather than silently running all workers on the default core.
CORE_GUI = 0
CORE_GATEWAY = 1
CORE_CAN = 1  # shares Core 1 with the asyncio gateway loop, see architecture.md

# --- CAN bus (Waveshare ESP32-S3-Touch-LCD-5B) --------------------------------
# https://docs.waveshare.com/ESP32-S3-Touch-LCD-5?variant=ESP32-S3-LCD-5B-touch
CAN_TX_PIN = 15
CAN_RX_PIN = 16
