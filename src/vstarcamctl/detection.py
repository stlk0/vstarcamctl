"""Motion and human-detection normalization and request construction."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ._normalize import as_flag, as_int_in_range
from .errors import DetectionConfigurationError


def _as_flag(value: Any, field: str) -> bool:
    return as_flag(value, field, DetectionConfigurationError)


def _as_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    return as_int_in_range(value, field, minimum, maximum, DetectionConfigurationError)


def parse_motion_detection_status(params: dict[str, Any]) -> dict[str, Any]:
    """Extract motion settings without returning the broad, secret-bearing snapshot."""

    armed = params.get("alarm_motion_armed")
    sensitivity = params.get("alarm_motion_sensitivity")
    result: dict[str, Any] = {
        "available": armed in (0, 1, "0", "1", False, True),
        "sensitivity_available": sensitivity is not None,
    }
    if result["available"]:
        result["enabled"] = _as_flag(armed, "alarm_motion_armed")
    if sensitivity is not None:
        result["sensitivity"] = _as_integer(sensitivity, "alarm_motion_sensitivity", 0, 9)
        result["sensitivity_scale"] = "0 (highest) to 9 (lowest)"
    return result


def build_motion_detection_set_path(
    enabled: bool,
    sensitivity: int,
    *,
    alarm_audio_enabled: bool,
) -> str:
    """Build the motion write while preserving alarm-audio state."""

    if not isinstance(enabled, bool):
        raise DetectionConfigurationError("motion enabled state must be boolean")
    if not isinstance(alarm_audio_enabled, bool):
        raise DetectionConfigurationError("alarm-audio state must be boolean")
    sensitivity = _as_integer(sensitivity, "motion sensitivity", 0, 9)
    query = urlencode(
        [
            ("enable_alarm_audio", int(alarm_audio_enabled)),
            ("motion_armed", int(enabled)),
            ("motion_sensitivity", sensitivity),
        ]
    )
    return f"/set_alarm.cgi?{query}"


def build_human_detection_set_path(
    enabled: bool,
    sensitivity: int,
    distance: int,
) -> str:
    if not isinstance(enabled, bool):
        raise DetectionConfigurationError("human detection enabled state must be boolean")
    sensitivity = _as_integer(sensitivity, "human detection sensitivity", 1, 3)
    distance = _as_integer(distance, "human detection distance", 1, 3)
    query = urlencode(
        [
            ("cmd", 2106),
            ("command", 4),
            ("humanDetection", sensitivity),
            ("DistanceAdjust", distance),
            ("HumanoidDetection", int(enabled)),
        ]
    )
    return f"/trans_cmd_string.cgi?{query}"


def build_human_sensitivity_set_path(sensitivity: int) -> str:
    sensitivity = _as_integer(sensitivity, "human sensitivity", 0, 3)
    return "/trans_cmd_string.cgi?" + urlencode(
        [("cmd", 2126), ("command", 0), ("sensitive", sensitivity)]
    )


def build_human_frame_set_path(enabled: bool) -> str:
    return _build_flag_command(2126, "bHumanoidFrame", enabled, "human frame")


def build_human_tracking_set_path(enabled: bool) -> str:
    return _build_flag_command(2127, "enable", enabled, "human tracking")


def build_human_zoom_tracking_set_path(enabled: bool) -> str:
    return _build_flag_command(2126, "humanoid_zoom", enabled, "human zoom tracking")


def _build_flag_command(command_id: int, field: str, enabled: bool, label: str) -> str:
    if not isinstance(enabled, bool):
        raise DetectionConfigurationError(f"{label} enabled state must be boolean")
    return "/trans_cmd_string.cgi?" + urlencode(
        [("cmd", command_id), ("command", 0), (field, int(enabled))]
    )


def parse_human_detection_status(
    payload: dict[str, Any],
    *,
    tracking_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize the fields returned by the 2126/2127 getters."""

    result: dict[str, Any] = {}
    _copy_flag(payload, result, "enabled", ("HumanoidDetection", "humanoid_detection"))
    if "sensitive" in payload:
        result["sensitivity"] = _as_integer(payload["sensitive"], "sensitive", 0, 3)
        result["sensitivity_scale"] = "device-specific 0 to 3"
    elif "HumanSensitivity" in payload:
        result["sensitivity"] = _as_integer(payload["HumanSensitivity"], "HumanSensitivity", 0, 3)
        result["sensitivity_scale"] = "device-specific 0 to 3"
    else:
        _copy_integer(
            payload,
            result,
            "sensitivity",
            ("humanDetection",),
            1,
            3,
        )
    _copy_integer(payload, result, "distance", ("DistanceAdjust",), 1, 3)
    _copy_flag(payload, result, "frame_enabled", ("bHumanoidFrame",))
    _copy_flag(payload, result, "zoom_tracking_enabled", ("humanoid_zoom",))
    if tracking_payload is not None:
        _copy_flag(tracking_payload, result, "tracking_enabled", ("enable",))
    return {"available": bool(result), **result}


def _first(payload: dict[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    for name in names:
        if name in payload:
            return name, payload[name]
    return None


def _copy_flag(
    payload: dict[str, Any],
    result: dict[str, Any],
    output: str,
    names: tuple[str, ...],
) -> None:
    item = _first(payload, names)
    if item is not None:
        source, value = item
        result[output] = _as_flag(value, source)


def _copy_integer(
    payload: dict[str, Any],
    result: dict[str, Any],
    output: str,
    names: tuple[str, ...],
    minimum: int,
    maximum: int,
) -> None:
    item = _first(payload, names)
    if item is not None:
        source, value = item
        result[output] = _as_integer(value, source, minimum, maximum)
