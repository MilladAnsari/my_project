"""tbgw.connectors.can.can_downlink_converter
================================================

Builds an outbound register write from either a ThingsBoard server-side
RPC request or a shared-attribute update, per the "writes" section of
can.json (see docs/can_connector_design.md). This is a rewrite of the
Phase 2 byte-offset downlink converter for the register/selector schema.

Per project decision, `eval()`-based expressions are accepted as-is
(tbgw.utils.safe_eval, gated by the same `strictEval` flag upstream
documents) for both `selectorExpression`/`selector` (which register to
write) and `valueExpression` (what to write there).

xxx
"""

from tbgw.utils.safe_eval import evaluate


class CanDownlinkConverterError(Exception):
    pass


class CanDownlinkConverter:
    """Built once per CAN connector from can.json's "writes" list. Pure
    and hardware-free -- build_for_rpc()/build_for_attribute_update()
    take plain Python values and do no I/O, so this is fully
    host-testable without a driver, gateway, or board.
    """

    def __init__(self, writes_config, payload_length=1, strict_eval=True, logger=None):
        self._writes = writes_config
        self._payload_length = payload_length
        self._strict_eval = strict_eval
        self._logger = logger

    def build_for_rpc(self, device, method, params):
        """device: the device config dict (as returned by
        CanUplinkConverter.device_by_key()) -- needs "deviceProfile" and
        "deviceName". Returns (selector: int, payload: bytes, op: "write"),
        or None if no "writes" entry matches this device+method.
        """
        return self._build(device, method=method, attribute=None, params=params, value=None)

    def build_for_attribute_update(self, device, attribute, value):
        """Same contract as build_for_rpc(), for a shared-attribute push
        from ThingsBoard instead of an RPC call.
        """
        return self._build(device, method=None, attribute=attribute, params=None, value=value)

    def _find(self, device, method, attribute):
        for entry in self._writes:
            if entry.get("deviceProfile") != device["deviceProfile"]:
                continue
            entry_device_name = entry.get("deviceName")
            if entry_device_name is not None and entry_device_name != device["deviceName"]:
                continue
            if method is not None:
                if entry.get("method") == method:
                    return entry
            else:
                if entry.get("attribute") == attribute:
                    return entry
        return None

    def _build(self, device, method, attribute, params, value):
        entry = self._find(device, method, attribute)
        if entry is None:
            return None

        scope = {"params": params, "value": value}
        try:
            if "selector" in entry:
                selector = entry["selector"]
            else:
                selector = evaluate(entry["selectorExpression"], scope, strict=self._strict_eval)
            raw_value = evaluate(entry["valueExpression"], scope, strict=self._strict_eval)
        except ValueError as exc:
            if self._logger is not None:
                self._logger.error(
                    "Downlink expression evaluation failed for %s (method=%s attribute=%s): %s",
                    device["deviceName"], method, attribute, exc,
                )
            return None

        try:
            # round(), not a plain int() truncation: valueExpression is
            # expected to compute a value in device units (e.g. "the
            # setpoint times 2" for a half-degree-resolution register),
            # not to round it itself -- strictEval=true (this project's
            # default) denies the expression's own scope access to
            # round()/int() along with every other builtin, so rounding
            # is done here, in code we control, rather than asking every
            # config author to either write scale-perfect integer math or
            # turn strictEval off.
            raw_int = int(round(raw_value))
        except (TypeError, ValueError):
            if self._logger is not None:
                self._logger.error(
                    "Downlink valueExpression for %s did not produce a number: %r",
                    device["deviceName"], raw_value,
                )
            return None

        max_value = (1 << (8 * self._payload_length)) - 1
        if not (0 <= raw_int <= max_value):
            if self._logger is not None:
                self._logger.error(
                    "Downlink value %d out of range for a %d-byte payload (device=%s selector=%s)",
                    raw_int, self._payload_length, device["deviceName"], selector,
                )
            return None

        payload = raw_int.to_bytes(self._payload_length, "big")
        return selector, payload, "write"
