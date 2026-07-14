"""Wi-Fi response normalization and guarded request construction."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ._normalize import has_control_chars
from .errors import WifiConfigurationError

_HEX_PSK_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

_STATUS_FIELDS = {
    "enabled": ("wifi_enable", "wlan_enable"),
    "ssid": ("wifi_ssid", "wlan_ssid", "ssid"),
    "bssid": ("wifi_bssid", "wlan_bssid", "bssid"),
    "channel": ("wifi_channel", "wlan_channel", "channel"),
    "encryption": ("wifi_encrypt", "wlan_encrypt", "encrypt"),
    "auth_type": ("wifi_authtype", "wlan_authtype", "authtype"),
    "signal_quality": ("wifi_signal_quality", "wifi_quality", "signal_quality"),
}

_SCAN_FIELDS = {
    "ssid": ("ap_ssid", "wifi_ssid", "ssid"),
    "bssid": ("ap_bssid", "wifi_bssid", "bssid"),
    "channel": ("ap_channel", "wifi_channel", "channel"),
    "auth_type": ("ap_security", "ap_authtype", "wifi_authtype", "authtype"),
    "mode": ("ap_mode", "wifi_mode", "mode"),
    "signal": ("ap_dbm0", "ap_signal", "wifi_signal_quality", "signal"),
}


def _casefolded(payload: dict[str, Any]) -> dict[str, Any]:
    return {str(key).casefold(): value for key, value in payload.items()}


def _pick(payload: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name.casefold() in payload:
            return payload[name.casefold()]
    return None


def extract_wifi_status(params: dict[str, Any]) -> dict[str, Any]:
    """Return only current Wi-Fi fields, leaving CLI redaction to the caller."""

    folded = _casefolded(params)
    result = {
        output: value
        for output, candidates in _STATUS_FIELDS.items()
        if (value := _pick(folded, candidates)) is not None
    }
    return {"available": bool(result), **result}


def normalize_wifi_scan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Align the indexed arrays returned by VStarcam Wi-Fi scan responses."""

    folded = _casefolded(payload)
    columns: dict[str, list[Any]] = {}
    for output, candidates in _SCAN_FIELDS.items():
        value = _pick(folded, candidates)
        if isinstance(value, list):
            columns[output] = value
    length = max((len(values) for values in columns.values()), default=0)
    networks: list[dict[str, Any]] = []
    for index in range(length):
        network: dict[str, Any] = {"index": index}
        for name, values in columns.items():
            if index < len(values) and values[index] is not None:
                network[name] = values[index]
        if network.get("ssid") is not None:
            networks.append(network)
    return networks


def validate_wifi_credentials(ssid: str, password: str) -> None:
    if not isinstance(ssid, str) or not ssid:
        raise WifiConfigurationError("Wi-Fi SSID cannot be empty")
    if has_control_chars(ssid):
        raise WifiConfigurationError("Wi-Fi SSID contains control characters")
    if len(ssid.encode("utf-8")) > 32:
        raise WifiConfigurationError("Wi-Fi SSID must be at most 32 UTF-8 bytes")
    if not isinstance(password, str):
        raise WifiConfigurationError("Wi-Fi password must be text")
    if has_control_chars(password):
        raise WifiConfigurationError("Wi-Fi password contains control characters")
    password_bytes = password.encode("utf-8")
    if not (8 <= len(password_bytes) <= 63 or _HEX_PSK_RE.fullmatch(password)):
        raise WifiConfigurationError(
            "Wi-Fi password must be 8-63 UTF-8 bytes or exactly 64 hexadecimal characters"
        )


def validate_wifi_metadata(channel: int, auth_type: int) -> None:
    if not isinstance(channel, int) or isinstance(channel, bool) or not 1 <= channel <= 196:
        raise WifiConfigurationError("Wi-Fi channel must be an integer from 1 to 196")
    if not isinstance(auth_type, int) or isinstance(auth_type, bool) or not 0 <= auth_type <= 255:
        raise WifiConfigurationError("Wi-Fi auth type must be an integer from 0 to 255")


def build_wifi_set_path(ssid: str, password: str, channel: int, auth_type: int) -> str:
    """Build the candidate command without camera/admin auth fields."""

    validate_wifi_credentials(ssid, password)
    validate_wifi_metadata(channel, auth_type)
    query = urlencode(
        [
            ("ssid", ssid),
            ("channel", channel),
            ("authtype", auth_type),
            ("wpa_psk", password),
            ("enable", 1),
        ]
    )
    return f"/set_wifi.cgi?{query}"


def complete_wifi_metadata(
    networks: list[dict[str, Any]],
    ssid: str,
    channel: int | None,
    auth_type: int | None,
) -> tuple[int, int]:
    """Fill missing metadata from an exact SSID match without guessing."""

    matches = [network for network in networks if network.get("ssid") == ssid]
    if channel is not None:
        matches = [network for network in matches if network.get("channel") == channel]
    if auth_type is not None:
        matches = [network for network in matches if network.get("auth_type") == auth_type]
    if not matches:
        raise WifiConfigurationError(
            "SSID was not found in the camera scan; supply --channel and --auth-type explicitly"
        )

    def unique_integer(field: str, explicit: int | None) -> int:
        if explicit is not None:
            return explicit
        values = {
            value
            for network in matches
            if isinstance((value := network.get(field)), int) and not isinstance(value, bool)
        }
        if len(values) != 1:
            raise WifiConfigurationError(
                f"camera scan did not provide one unambiguous {field}; supply it explicitly"
            )
        return values.pop()

    resolved_channel = unique_integer("channel", channel)
    resolved_auth_type = unique_integer("auth_type", auth_type)
    validate_wifi_metadata(resolved_channel, resolved_auth_type)
    return resolved_channel, resolved_auth_type
