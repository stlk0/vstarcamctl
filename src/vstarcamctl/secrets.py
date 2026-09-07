"""Secret masking for paths, responses, and logging."""

from __future__ import annotations

import logging
import re
from typing import Any

SENSITIVE_KEYS = (
    "ExPwd",
    "ExUser",
    "OwnerPwd",
    "OwnerUser",
    "pwd3",
    "user3",
    "user1_name",
    "user1_pwd",
    "user2_name",
    "user2_pwd",
    "user3_name",
    "user3_pwd",
    "WebPwd",
    "pwd",
    "password",
    "loginuse",
    "user",
    "loginToken",
    "login_token",
    "token",
    "loginpas",
    "login_hash",
    "hash",
    "userId",
    "loginAccount",
    "account_id",
    "psk",
    "wifi_password",
    "wifipwd",
    "wifi_pwd",
    "rtsppwd",
    "rtsp_pwd",
    "rtsp_password",
    "rtspuser",
    "rtsp_user",
    "wifi_wpa_psk",
    "wpa_psk",
    "wifi_ssid",
    "wlan_ssid",
    "ap_ssid",
    "bssid",
    "wifi_bssid",
    "wlan_bssid",
    "ap_bssid",
    "ap_mac",
    "mac",
    "mac_address",
    "public_ip",
    "external_ip",
    "source_ip",
    "destination_ip",
    "local_ip",
    "remote_ip",
    "ntp_svr",
    "vuid",
    "uid",
    "deviceid",
    "realdeviceid",
    "device_id",
    "devid",
    "serial",
    "did",
    "ssid",
)

_KEYS = "|".join(sorted((re.escape(key) for key in SENSITIVE_KEYS), key=len, reverse=True))
_SENSITIVE_KEYS_LOWER = frozenset(key.lower() for key in SENSITIVE_KEYS)
_QUOTED_VALUE = r"""(?:"(?:\\.|[^"\\])*(?:"|\\?$)|'(?:\\.|[^'\\])*(?:'|\\?$))"""
_QUERY_RE = re.compile(
    rf"(\b(?:{_KEYS})\b\s*=\s*)"
    rf"({_QUOTED_VALUE}|(?![\"'])[^&;\r\n]*?"
    rf"(?=&|;|\r?\n|$|\s+(?={{|\[|[A-Za-z_][A-Za-z0-9_]*\s*[=:])))",
    re.IGNORECASE | re.DOTALL,
)
_MAPPING_RE = re.compile(
    rf"([\"']?(?:{_KEYS})[\"']?\s*:\s*)({_QUOTED_VALUE}|(?![\"'])[^,}}\]\r\n]+)",
    re.IGNORECASE | re.DOTALL,
)
_PAREN_IDENTIFIER_RE = re.compile(r"(?i)(\b(?:dev(?:ice)?id|vuid|serial)\s*\(\s*)([^)\s]+)(\s*\))")


def _mask_value(match: re.Match[str]) -> str:
    value = match.group(2)
    quote = value[:1] if value.startswith(("'", '"')) else ""
    return f"{match.group(1)}{quote}***{quote}"


def mask_secrets(value: Any) -> str:
    """Return a printable value with known credential fields redacted."""

    text = str(value)
    text = _QUERY_RE.sub(_mask_value, text)
    text = _MAPPING_RE.sub(_mask_value, text)
    text = _PAREN_IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}***{match.group(3)}", text)
    return text


def mask_identifier(value: str | None) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


def redact_data(value: Any) -> Any:
    """Recursively redact sensitive mapping fields before CLI serialization."""

    if isinstance(value, dict):
        return {
            key: "***" if str(key).lower() in _SENSITIVE_KEYS_LOWER else redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return mask_secrets(value)
    return value


class SecretMaskingFilter(logging.Filter):
    """Best-effort last line of defense for application log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = ()
        return True
