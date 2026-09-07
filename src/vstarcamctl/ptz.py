"""Manual pan/tilt request construction and validation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlencode

from ._normalize import require_zero_result
from .errors import PTZConfigurationError

PTZDirection = Literal["up", "down", "left", "right"]

_COMMANDS: dict[PTZDirection, tuple[int, int]] = {
    "up": (0, 1),
    "down": (2, 3),
    "left": (4, 5),
    "right": (6, 7),
}
_DURATION_ERROR = "PTZ duration must be a finite number greater than zero"


def _command(direction: PTZDirection, *, stop: bool) -> int:
    try:
        start_command, stop_command = _COMMANDS[direction]
    except (KeyError, TypeError) as exc:
        raise PTZConfigurationError("PTZ direction must be one of: up, down, left, right") from exc
    return stop_command if stop else start_command


def build_ptz_start_path(direction: PTZDirection) -> str:
    return "/decoder_control.cgi?" + urlencode(
        (("command", _command(direction, stop=False)), ("onestep", 0))
    )


def build_ptz_stop_path(direction: PTZDirection) -> str:
    return "/decoder_control.cgi?" + urlencode(
        (("command", _command(direction, stop=True)), ("onestep", 0))
    )


def validate_ptz_duration(duration: float) -> float:
    """Require a caller-selected finite positive movement bound."""

    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise PTZConfigurationError(_DURATION_ERROR)
    try:
        value = float(duration)
    except OverflowError as exc:
        raise PTZConfigurationError(_DURATION_ERROR) from exc
    if not math.isfinite(value) or value <= 0:
        raise PTZConfigurationError(_DURATION_ERROR)
    return value


def parse_ptz_response(payload: Mapping[str, object]) -> dict[str, int]:
    """Validate the observed decoder-control acknowledgement."""

    require_zero_result(payload, "PTZ", PTZConfigurationError)
    return {"result": 0}
