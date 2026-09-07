"""Night-vision mode and infrared illuminator normalization."""

from __future__ import annotations

from typing import Any, Literal, get_args
from urllib.parse import urlencode

from ._normalize import as_exact_flag, as_flag, as_int_in_range, require_zero_result
from .errors import NightVisionConfigurationError

NightVisionMode = Literal["black-white", "starlight", "full-color", "smart"]

_NIGHT_VISION_MODES = get_args(NightVisionMode)


def _as_flag(value: Any, field: str) -> bool:
    return as_flag(value, field, NightVisionConfigurationError)


def build_night_vision_set_paths(mode: NightVisionMode) -> tuple[str, ...]:
    """Build the documented two-step transition for one night-vision mode."""

    if mode not in _NIGHT_VISION_MODES:
        choices = ", ".join(sorted(_NIGHT_VISION_MODES))
        raise NightVisionConfigurationError(f"night-vision mode must be one of: {choices}")

    if mode in {"black-white", "starlight"}:
        values = ((33, 0), (14, 1 if mode == "black-white" else 0))
    else:
        # The vendor workflow selects black-and-white low-light behavior before
        # entering either white-light-assisted color mode.
        values = ((14, 1), (33, 1 if mode == "full-color" else 2))
    return tuple(
        f"/camera_control.cgi?{urlencode((('param', param), ('value', value)))}"
        for param, value in values
    )


def build_infrared_light_set_path(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise NightVisionConfigurationError("infrared-light state must be boolean")
    query = urlencode((("cmd", 2120), ("command", 0), ("InfraredLaser", int(enabled))))
    return f"/trans_cmd_string.cgi?{query}"


def parse_night_vision_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only normalized night-vision fields from get_camera_params.cgi."""

    raw_mode = payload.get("night_vision_mode")
    raw_ircut = payload.get("ircut")
    if raw_mode is None and raw_ircut is None:
        return {"available": False}

    result: dict[str, Any] = {"available": True}
    ircut = None if raw_ircut is None else _as_flag(raw_ircut, "ircut")
    if ircut is not None:
        result["low_light_mode"] = "black-white" if ircut else "starlight"

    if raw_mode is None:
        result["mode"] = result["low_light_mode"]
        return result

    mode = as_int_in_range(
        raw_mode,
        "night_vision_mode",
        0,
        2,
        NightVisionConfigurationError,
    )
    if mode == 1:
        result["mode"] = "full-color"
    elif mode == 2:
        result["mode"] = "smart"
    elif ircut is None:
        result["mode"] = "black-white-or-starlight"
    else:
        result["mode"] = "black-white" if ircut else "starlight"
    return result


def parse_night_vision_set_response(payload: dict[str, Any]) -> dict[str, int]:
    """Validate one generic camera-control transition acknowledgement."""

    require_zero_result(payload, "night-vision", NightVisionConfigurationError)
    return {"result": 0}


def parse_infrared_light_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the command-2120 response without returning vendor extras."""

    for field, expected in (("result", 0), ("cmd", 2120), ("command", 1)):
        if field in payload:
            as_int_in_range(
                payload[field],
                field,
                expected,
                expected,
                NightVisionConfigurationError,
            )
    if "InfraredLaser" not in payload:
        return {"available": False}
    return {
        "available": True,
        "logical_control_enabled": _as_flag(payload["InfraredLaser"], "InfraredLaser"),
    }


def parse_infrared_light_set_response(payload: dict[str, Any], expected: bool) -> dict[str, int]:
    """Validate the exact command-2120 write acknowledgement."""

    if not isinstance(expected, bool):
        raise NightVisionConfigurationError("expected infrared-light state must be boolean")
    for field, value in (("result", 0), ("cmd", 2120), ("command", 0)):
        as_int_in_range(
            payload.get(field),
            field,
            value,
            value,
            NightVisionConfigurationError,
        )
    if "InfraredLaser" not in payload:
        raise NightVisionConfigurationError("InfraredLaser is missing")
    state = as_exact_flag(
        payload["InfraredLaser"],
        "InfraredLaser",
        NightVisionConfigurationError,
    )
    if state is not expected:
        raise NightVisionConfigurationError(
            "infrared-light response state does not match the requested state"
        )
    return {
        "result": 0,
        "cmd": 2120,
        "command": 0,
        "InfraredLaser": int(state),
    }
