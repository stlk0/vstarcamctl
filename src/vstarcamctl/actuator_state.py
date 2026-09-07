"""Siren, white-light, and alarm-indicator state validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from ._normalize import as_flag, as_int, require_zero_result
from .errors import ActuatorStateConfigurationError

ActuatorStateField = Literal["sirenStatus", "lightStatus", "alarmLedStatus"]

ACTUATOR_STATUS_PATH = "/trans_cmd_string.cgi?cmd=2109&command=2"


def build_alarm_led_set_path(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise ActuatorStateConfigurationError("alarm-indicator state must be boolean")
    return f"/trans_cmd_string.cgi?cmd=2109&command=0&alarmLed={int(enabled)}"


def parse_actuator_set_response(payload: Mapping[str, Any]) -> dict[str, int]:
    """Validate the observed generic siren/white-light acknowledgement."""

    require_zero_result(payload, "actuator", ActuatorStateConfigurationError)
    return {"result": 0}


def _parse_state(
    payload: Mapping[str, Any],
    *,
    command: int,
    field: ActuatorStateField,
) -> bool:
    require_zero_result(payload, "actuator", ActuatorStateConfigurationError)
    command_id = as_int(payload.get("cmd"), "cmd", ActuatorStateConfigurationError)
    operation = as_int(payload.get("command"), "command", ActuatorStateConfigurationError)
    state = as_int(payload.get(field), field, ActuatorStateConfigurationError)
    if command_id != 2109 or operation != command:
        raise ActuatorStateConfigurationError(
            f"actuator response must identify cmd=2109 and command={command}"
        )
    return as_flag(state, field, ActuatorStateConfigurationError)


def parse_siren_state(payload: Mapping[str, Any]) -> bool:
    return _parse_state(payload, command=2, field="sirenStatus")


def parse_light_state(payload: Mapping[str, Any]) -> bool:
    return _parse_state(payload, command=2, field="lightStatus")


def parse_alarm_led_state(payload: Mapping[str, Any]) -> bool:
    return _parse_state(payload, command=2, field="alarmLedStatus")


def parse_alarm_led_set_response(payload: Mapping[str, Any], expected: bool) -> bool:
    """Validate a command-2109 setter acknowledgement and its echoed state."""

    if not isinstance(expected, bool):
        raise ActuatorStateConfigurationError("expected alarm-indicator state must be boolean")
    state = _parse_state(payload, command=0, field="alarmLedStatus")
    if state is not expected:
        raise ActuatorStateConfigurationError(
            "alarm-indicator response state does not match the requested state"
        )
    return state
