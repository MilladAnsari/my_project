"""tbgw.core.config_loader
============================

Loads and lightly validates the gateway's JSON configuration files
(config/tb_gateway.json plus each connector's own config file, e.g.
config/mqtt.json, config/can.json). Mirrors upstream's
`_load_general_config` / connector-config-loading pair of responsibilities,
minus:

  * the dynamic "scan a directory for connector implementations" plugin
    loader (this port uses a small static registry instead -- see
    gateway_service.py, Phase 3),
  * the old/new config-format BackwardCompatibilityAdapter (out of scope:
    this port defines its own config schema from scratch rather than
    inheriting upstream's format history, so there is no "old format" to
    be compatible with here).
"""

import json

from tbgw.utils.logger import get_logger

logger = get_logger("config_loader")

DEFAULT_GENERAL_CONFIG = {
    "thingsboard": {
        "host": "localhost",
        "port": 1883,
        "security": {
            "type": "accessToken",
            "accessToken": "YOUR_GATEWAY_ACCESS_TOKEN",
        },
        "qos": 1,
        "minPackSendDelayMs": 50,
        "checkConnectorsConfigurationInSeconds": 60,
    },
    "storage": {
        "type": "memory",
        "maxRecordsCount": 1000,
        "overflowPolicy": "drop_oldest",
    },
    "connectors": [],
    "logging": "config/logs.json",
}


def _deep_merge_defaults(config, defaults):
    """Fills in any keys missing from `config` using `defaults`, recursively
    for nested dicts. Does not overwrite values the user did provide, even
    if falsy (0, "", False are all valid user choices).
    """
    for key, default_value in defaults.items():
        if key not in config:
            config[key] = default_value
        elif isinstance(default_value, dict) and isinstance(config[key], dict):
            _deep_merge_defaults(config[key], default_value)
    return config


def load_json_file(path):
    with open(path, "r") as f:
        return json.load(f)


def load_general_config(path="config/tb_gateway.json"):
    """Loads the main gateway config, filling in defaults for anything not
    explicitly present. Raises OSError if the file is missing (fail fast --
    a gateway with no idea what its ThingsBoard credentials are should not
    silently start).
    """
    config = load_json_file(path)
    config = _deep_merge_defaults(config, DEFAULT_GENERAL_CONFIG)
    logger.info("Loaded general configuration from %s", path)
    return config


def load_connector_config(path):
    """Loads a single connector's own configuration file (e.g. mqtt.json,
    can.json). No defaulting is applied here -- each connector module is
    responsible for validating/defaulting its own schema, since the shape
    differs meaningfully between connector types.
    """
    config = load_json_file(path)
    logger.info("Loaded connector configuration from %s", path)
    return config


def load_logging_config(path="config/logs.json"):
    try:
        return load_json_file(path)
    except OSError:
        logger.warning("No logging config found at %s, using defaults", path)
        return {"default": "INFO"}


def load_wifi_config(path="config/wifi.json", local_path="config/wifi.local.json"):
    """Prefers `local_path` (gitignored, real credentials -- see
    .gitignore's pre-existing "config/*.local.json" rule) over `path`
    (checked in with placeholder values, so the repo documents the
    expected shape without ever containing a real SSID/password).
    Raises OSError if neither file exists.
    """
    try:
        config = load_json_file(local_path)
        logger.info("Loaded Wi-Fi configuration from %s", local_path)
        return config
    except OSError:
        pass
    config = load_json_file(path)
    logger.info("Loaded Wi-Fi configuration from %s", path)
    return config
