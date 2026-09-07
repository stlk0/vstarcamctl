"""Narrow normalization for reported camera image adjustments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._normalize import as_int, require_zero_result
from .errors import ImagingConfigurationError

CAMERA_PARAMS_PATH = "/get_camera_params.cgi"


def parse_image_adjustments(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only reported brightness and contrast values."""

    require_zero_result(payload, "camera image", ImagingConfigurationError)

    fields = {"brightness": "vbright", "contrast": "vcontrast"}
    result: dict[str, Any] = {"available": any(field in payload for field in fields.values())}
    for output, field in fields.items():
        if field in payload:
            result[output] = as_int(payload[field], field, ImagingConfigurationError)
    return result
