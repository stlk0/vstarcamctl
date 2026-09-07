"""Allowlisted software information from the confirmed status response."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._normalize import has_control_chars, require_zero_result
from .errors import DeviceInfoConfigurationError

_SOFTWARE_FIELDS = {
    "system_version": "sys_ver",
    "application_version": "app_version",
    "kernel_version": "kernel_version",
}


def parse_device_software_info(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only reported software versions, never identity or network data."""

    require_zero_result(payload, "device status", DeviceInfoConfigurationError)

    result: dict[str, Any] = {
        "available": any(field in payload for field in _SOFTWARE_FIELDS.values())
    }
    for output, field in _SOFTWARE_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            raise DeviceInfoConfigurationError(f"{field} must be text")
        if not value.strip():
            raise DeviceInfoConfigurationError(f"{field} cannot be empty")
        if has_control_chars(value):
            raise DeviceInfoConfigurationError(f"{field} contains control characters")
        result[output] = value
    return result
