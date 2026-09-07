"""Camera clock, fixed UTC-offset, and NTP setting helpers."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from ._normalize import as_flag, as_int
from .errors import TimeConfigurationError

_OFFSET_RE = re.compile(
    r"^(?:(?:UTC|GMT)\s*)?(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$",
    re.IGNORECASE,
)
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_MAX_OFFSET_SECONDS = 14 * 60 * 60
_MAX_UNIX_TIME = 2_147_483_647


def _as_int(value: Any, field: str) -> int:
    return as_int(value, field, TimeConfigurationError)


def _as_flag(value: Any, field: str) -> bool:
    return as_flag(value, field, TimeConfigurationError)


def parse_utc_offset(value: str) -> int:
    """Parse a fixed UTC offset and return seconds east of UTC."""

    if not isinstance(value, str):
        raise TimeConfigurationError("timezone must be text such as UTC+05:30")
    normalized = value.strip()
    if normalized.upper() in {"Z", "UTC", "GMT"}:
        return 0
    match = _OFFSET_RE.fullmatch(normalized)
    if match is None:
        raise TimeConfigurationError("timezone must be UTC, Z, or a fixed offset such as UTC+05:30")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes") or 0)
    if minutes > 59 or hours > 14 or (hours == 14 and minutes != 0):
        raise TimeConfigurationError("timezone offset must be between UTC-14:00 and UTC+14:00")
    seconds = hours * 60 * 60 + minutes * 60
    return seconds if match.group("sign") == "+" else -seconds


def format_utc_offset(offset_seconds: int) -> str:
    offset_seconds = validate_utc_offset(offset_seconds)
    sign = "+" if offset_seconds >= 0 else "-"
    hours, remainder = divmod(abs(offset_seconds), 60 * 60)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def validate_utc_offset(offset_seconds: int) -> int:
    offset_seconds = _as_int(offset_seconds, "timezone offset")
    if not -_MAX_OFFSET_SECONDS <= offset_seconds <= _MAX_OFFSET_SECONDS:
        raise TimeConfigurationError("timezone offset must be between UTC-14:00 and UTC+14:00")
    if offset_seconds % 60:
        raise TimeConfigurationError("timezone offset must use whole minutes")
    return offset_seconds


def validate_unix_time(unix_time: int) -> int:
    unix_time = _as_int(unix_time, "Unix time")
    if not 0 <= unix_time <= _MAX_UNIX_TIME:
        raise TimeConfigurationError("Unix time must be between 0 and 2147483647 seconds")
    return unix_time


def validate_ntp_server(server: str, *, enabled: bool) -> str:
    if not isinstance(server, str):
        raise TimeConfigurationError("NTP server must be text")
    if any(ord(char) < 33 or ord(char) == 127 for char in server):
        raise TimeConfigurationError("NTP server must not contain whitespace or control characters")
    if len(server.encode("utf-8")) > 64:
        raise TimeConfigurationError("NTP server must be at most 64 UTF-8 bytes")
    if not server:
        if enabled:
            raise TimeConfigurationError("NTP server is required when NTP is enabled")
        return server
    try:
        ipaddress.ip_address(server)
    except ValueError:
        if _HOSTNAME_RE.fullmatch(server) is None:
            raise TimeConfigurationError("NTP server must be a hostname or IP address")
    return server


def build_time_settings_set_path(
    timezone_offset_seconds: int,
    ntp_enabled: bool,
    ntp_server: str,
    unix_time: int,
) -> str:
    """Build the legacy datetime-setting request.

    The firmware's ``tz`` field uses the inverse of the conventional UTC
    offset: UTC+05:30 is encoded as ``tz=-19800``.
    """

    if not isinstance(ntp_enabled, bool):
        raise TimeConfigurationError("NTP enabled state must be boolean")
    offset = validate_utc_offset(timezone_offset_seconds)
    server = validate_ntp_server(ntp_server, enabled=ntp_enabled)
    now = validate_unix_time(unix_time)
    query = urlencode(
        (
            ("tz", -offset),
            ("ntp_enable", int(ntp_enabled)),
            ("ntp_svr", server),
            ("now", now),
        )
    )
    return f"/set_datetime.cgi?{query}"


def parse_time_settings_set_response(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate the exact datetime acknowledgement."""

    if not isinstance(payload, Mapping):
        raise TimeConfigurationError("time response must be a mapping")
    if payload.get("result") != "ok":
        raise TimeConfigurationError("time response result must be ok")
    return {"result": "ok"}


def _safe_ntp_server(server: str) -> str:
    """Avoid returning a literal public IP while retaining useful hostnames."""

    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        return server
    return "***" if address.is_global else server


def parse_time_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize only clock/timezone/NTP fields from ``get_params.cgi``."""

    present = {"now", "tz", "ntp_enable", "ntp_svr"} & payload.keys()
    if not present:
        return {"available": False}

    result: dict[str, Any] = {"available": True}
    if "tz" in payload:
        vendor_tz = _as_int(payload["tz"], "tz")
        offset = validate_utc_offset(-vendor_tz)
        result["timezone"] = format_utc_offset(offset)
        result["timezone_offset_seconds"] = offset

    if "now" in payload:
        unix_time = validate_unix_time(payload["now"])
        utc = datetime.fromtimestamp(unix_time, timezone.utc)
        result["unix_time"] = unix_time
        result["utc_time"] = utc.isoformat().replace("+00:00", "Z")
        if "timezone_offset_seconds" in result:
            offset_zone = timezone(timedelta(seconds=result["timezone_offset_seconds"]))
            result["local_time"] = utc.astimezone(offset_zone).isoformat()

    if "ntp_enable" in payload:
        result["ntp_enabled"] = _as_flag(payload["ntp_enable"], "ntp_enable")
    if "ntp_svr" in payload:
        enabled = result.get("ntp_enabled", False)
        server = validate_ntp_server(payload["ntp_svr"], enabled=enabled)
        result["ntp_server"] = _safe_ntp_server(server)
    return result
