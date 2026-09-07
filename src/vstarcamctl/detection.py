"""Motion and human-detection normalization and request construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from ._normalize import as_exact_flag, as_flag, as_int_in_range
from .errors import DetectionConfigurationError

MOTION_REGIONS_STATUS_PATH = "/trans_cmd_string.cgi?cmd=2123&command=1&sensor=0"
MOTION_REGION_ROWS = 18
MOTION_REGION_COLUMNS = 22

_ALARM_SCHEDULE_FIELDS = tuple(
    f"schedule_{day}_{slot}"
    for day in ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    for slot in range(3)
)
_ALARM_PRESERVED_FIELDS = (
    "input_armed",
    "ioin_level",
    "iolinkage",
    "ioout_level",
    "preset",
    "mail",
    "snapshot",
    "record",
    "upload_interval",
    "schedule_enable",
    *_ALARM_SCHEDULE_FIELDS,
    *(f"defense_plan{index}" for index in range(1, 22)),
)
_CLOUD_VIDEO_DURATION_FIELD = "CloudVideoDuration"


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


def parse_motion_detection_regions(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the exact 18-by-22 motion-detection cell grid."""

    if not isinstance(payload, Mapping):
        raise DetectionConfigurationError("motion-region response must be a mapping")
    _as_integer(payload.get("result"), "result", 0, 0)
    _as_integer(payload.get("cmd"), "cmd", 2123, 2123)
    _as_integer(payload.get("command"), "command", 1, 1)

    maximum = (1 << MOTION_REGION_COLUMNS) - 1
    masks = [
        _as_integer(payload.get(f"md_reign{row}"), f"md_reign{row}", 0, maximum)
        for row in range(MOTION_REGION_ROWS)
    ]
    return {
        "row_count": MOTION_REGION_ROWS,
        "column_count": MOTION_REGION_COLUMNS,
        "detection_enabled": [
            [
                bool(mask & (1 << (MOTION_REGION_COLUMNS - column - 1)))
                for column in range(MOTION_REGION_COLUMNS)
            ]
            for mask in masks
        ],
    }


def build_motion_detection_set_path(
    enabled: bool,
    sensitivity: int,
    *,
    params: dict[str, Any],
) -> str:
    """Build an aggregate alarm write only from exact wire-named adjacent values."""

    if not isinstance(enabled, bool):
        raise DetectionConfigurationError("motion enabled state must be boolean")
    if not isinstance(params, dict):
        raise DetectionConfigurationError("alarm settings snapshot must be a mapping")
    sensitivity = _as_integer(sensitivity, "motion sensitivity", 0, 9)
    missing = [
        field for field in ("enable_alarm_audio", *_ALARM_PRESERVED_FIELDS) if field not in params
    ]
    if missing:
        raise DetectionConfigurationError(
            "camera did not report the complete exact wire-named alarm profile; no "
            "getter-to-wire mapping is proven for prefixed alarm or motion-plan fields, "
            "so refusing set_alarm.cgi (missing: " + ", ".join(missing) + ")"
        )
    alarm_audio = int(_as_flag(params["enable_alarm_audio"], "enable_alarm_audio"))
    preserved: list[tuple[str, Any]] = []
    for field in _ALARM_PRESERVED_FIELDS:
        value = params[field]
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (str, int)):
            raise DetectionConfigurationError(
                f"camera reported a non-scalar {field}; refusing to rewrite the alarm profile"
            )
        preserved.append((field, value))
    query_fields: list[tuple[str, Any]] = [
        ("enable_alarm_audio", alarm_audio),
        ("motion_armed", int(enabled)),
        ("motion_sensitivity", sensitivity),
        *preserved,
    ]
    if _CLOUD_VIDEO_DURATION_FIELD in params:
        cloud_duration = params[_CLOUD_VIDEO_DURATION_FIELD]
        if not isinstance(cloud_duration, int) or isinstance(cloud_duration, bool):
            raise DetectionConfigurationError(
                "camera reported a non-integer CloudVideoDuration; refusing to rewrite "
                "the alarm profile"
            )
        if cloud_duration != -1:
            query_fields.append((_CLOUD_VIDEO_DURATION_FIELD, cloud_duration))
    query = urlencode(query_fields)
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


def parse_human_sensitivity_set_response(payload: dict[str, Any], expected: int) -> dict[str, int]:
    """Validate the observed command-2126 sensitivity acknowledgement."""

    expected = _as_integer(expected, "expected human sensitivity", 0, 3)
    _as_integer(payload.get("result"), "result", 0, 0)
    actual = _as_integer(payload.get("sensitive"), "sensitive", 0, 3)
    if actual != expected:
        raise DetectionConfigurationError(
            "human sensitivity response does not match the requested level"
        )
    return {"result": 0, "sensitive": actual}


def parse_human_frame_set_response(payload: dict[str, Any], expected: bool) -> dict[str, int]:
    """Validate the observed command-2126 frame acknowledgement."""

    if not isinstance(expected, bool):
        raise DetectionConfigurationError("expected human frame state must be boolean")
    _as_integer(payload.get("result"), "result", 0, 0)
    actual = as_exact_flag(
        payload.get("bHumanoidFrame"),
        "bHumanoidFrame",
        DetectionConfigurationError,
    )
    if actual is not expected:
        raise DetectionConfigurationError("human frame response does not match the requested state")
    return {"result": 0, "bHumanoidFrame": int(actual)}


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
    """Normalize only fields returned by the proven 2126/2127 getters."""

    for field, expected in (("result", 0), ("cmd", 2126), ("command", 1)):
        if field in payload:
            _as_integer(payload[field], field, expected, expected)
    result: dict[str, Any] = {}
    _copy_flag(payload, result, "enabled", ("HumanoidDetection", "humanoid_detection"))
    if "sensitive" in payload:
        result["sensitivity"] = _as_integer(payload["sensitive"], "sensitive", 0, 3)
        result["sensitivity_scale"] = "device-specific 0 to 3"
    _copy_flag(payload, result, "frame_enabled", ("bHumanoidFrame",))
    _copy_flag(payload, result, "zoom_tracking_setting_enabled", ("humanoid_zoom",))
    if tracking_payload is not None:
        for field, expected in (("result", 0), ("cmd", 2127), ("command", 1)):
            if field in tracking_payload:
                _as_integer(tracking_payload[field], field, expected, expected)
        _copy_flag(tracking_payload, result, "tracking_setting_enabled", ("enable",))
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
