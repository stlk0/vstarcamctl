"""Night-vision mode and infrared illuminator normalization."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlencode

from ._normalize import as_flag
from .errors import NightVisionConfigurationError

NightVisionMode = Literal["black-white", "starlight", "full-color", "smart"]

_NIGHT_VISION_MODES = {
    "black-white",
    "starlight",
    "full-color",
    "smart",
}


def _as_flag(value: Any, field: str) -> bool:
    return as_flag(value, field, NightVisionConfigurationError)


def _as_mode(value: Any) -> int:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise NightVisionConfigurationError("night_vision_mode must be 0, 1, or 2") from exc
    if value not in (0, 1, 2):
        raise NightVisionConfigurationError("night_vision_mode must be 0, 1, or 2")
    return value


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

    mode = _as_mode(raw_mode)
    if mode == 1:
        result["mode"] = "full-color"
    elif mode == 2:
        result["mode"] = "smart"
    elif ircut is None:
        result["mode"] = "black-white-or-starlight"
    else:
        result["mode"] = "black-white" if ircut else "starlight"
    return result


def parse_infrared_light_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the command-2120 response without returning vendor extras."""

    if "InfraredLaser" not in payload:
        return {"available": False}
    return {
        "available": True,
        "enabled": _as_flag(payload["InfraredLaser"], "InfraredLaser"),
    }
