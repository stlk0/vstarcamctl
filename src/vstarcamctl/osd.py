"""On-screen-display request and response validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._normalize import as_flag, as_int, require_zero_result
from .errors import OSDConfigurationError

OSD_12H_STATUS_PATH = "/trans_cmd_string.cgi?cmd=4109&command=0"
LOGO_OSD_STATUS_PATH = "/get_camera_params.cgi"


def build_osd_12h_set_path(twelve_hour: bool) -> str:
    if not isinstance(twelve_hour, bool):
        raise OSDConfigurationError("OSD clock mode must be boolean")
    return f"/trans_cmd_string.cgi?cmd=4109&command=1&osd_12h_mode={int(twelve_hour)}"


def build_logo_osd_set_path(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise OSDConfigurationError("logo OSD state must be boolean")
    return f"/camera_control.cgi?param=11&value={int(enabled)}"


def _parse_response(payload: Mapping[str, Any], *, command: int) -> bool:
    require_zero_result(payload, "OSD", OSDConfigurationError)
    command_id = as_int(payload.get("cmd"), "cmd", OSDConfigurationError)
    operation = as_int(payload.get("command"), "command", OSDConfigurationError)
    mode = as_int(payload.get("osd_12h_mode"), "osd_12h_mode", OSDConfigurationError)
    if command_id != 4109 or operation != command:
        raise OSDConfigurationError(f"OSD response must identify cmd=4109 and command={command}")
    return as_flag(mode, "osd_12h_mode", OSDConfigurationError)


def parse_osd_12h_status(payload: Mapping[str, Any]) -> bool:
    """Return the normalized 12-hour mode from an exact command-4109 getter."""

    return _parse_response(payload, command=0)


def parse_timestamp_osd_status(payload: Mapping[str, Any]) -> bool:
    """Return whether the timestamp overlay is visible."""

    require_zero_result(payload, "timestamp OSD", OSDConfigurationError)
    state = as_int(payload.get("osdenable"), "osdenable", OSDConfigurationError)
    return as_flag(state, "osdenable", OSDConfigurationError)


def parse_osd_12h_set_response(
    payload: Mapping[str, Any],
    expected: bool,
) -> bool:
    """Validate a command-4109 setter acknowledgement and its echoed mode."""

    if not isinstance(expected, bool):
        raise OSDConfigurationError("expected OSD clock mode must be boolean")
    mode = _parse_response(payload, command=1)
    if mode is not expected:
        raise OSDConfigurationError("OSD response mode does not match the requested mode")
    return mode


def parse_logo_osd_status(payload: Mapping[str, Any]) -> bool:
    """Return the exact logo visibility flag from camera parameters."""

    if not isinstance(payload, Mapping):
        raise OSDConfigurationError("logo OSD response must be a mapping")
    state = as_int(payload.get("logoOsdEnable"), "logoOsdEnable", OSDConfigurationError)
    return as_flag(state, "logoOsdEnable", OSDConfigurationError)


def parse_logo_osd_set_response(payload: Mapping[str, Any]) -> dict[str, int]:
    """Validate the generic camera-control acknowledgement for logo visibility."""

    require_zero_result(payload, "logo OSD", OSDConfigurationError)
    return {"result": 0}
