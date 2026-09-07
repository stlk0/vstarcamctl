"""Validation, authentication, and formatting of tunneled CGI requests."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._normalize import has_control_chars
from .config import VStarcamConfig
from .errors import RawCommandError

AUTH_KEYS = {
    "loginuse",
    "user",
    "pwd",
    "userid",
    "loginpas",
    "logintoken",
}

_WIFI_SET_PATH = "/set_wifi.cgi"
_CGI_PATH = re.compile(r"/[A-Za-z0-9_-]+\.cgi").fullmatch


def validate_raw_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise RawCommandError("raw CGI path cannot be empty")
    if has_control_chars(path):
        raise RawCommandError("raw CGI path contains control characters")
    if not path.startswith("/") or path.startswith("//"):
        raise RawCommandError("only a single-host-relative path beginning with '/' is allowed")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise RawCommandError("full URLs and network locations are not allowed")
    if parsed.fragment:
        raise RawCommandError("URL fragments are not sent to the camera")
    if not _CGI_PATH(parsed.path):
        raise RawCommandError("only canonical root-level CGI endpoint paths are accepted")
    return path


def build_auth_params(config: VStarcamConfig) -> list[tuple[str, str]]:
    config.validate(require_auth=True)
    username = config.username
    password = config.password or ""
    params: list[tuple[str, str]] = [("loginuse", username)]
    if config.auth_mode == "observed":
        params.extend(
            [
                ("userId", config.account_id or ""),
                ("loginpas", config.login_hash or ""),
            ]
        )
    else:
        # The SDK uses owner zero when no vendor account is configured.
        params.extend([("userId", config.account_id or "0"), ("loginpas", password)])
    params.extend([("user", username), ("pwd", password)])
    return params


def _build_endpoint_auth_params(path: str, config: VStarcamConfig) -> list[tuple[str, str]]:
    """Return endpoint-specific identity fields from trusted configuration."""

    if path != _WIFI_SET_PATH:
        return []
    account_id = config.account_id
    if not isinstance(account_id, str) or not account_id.strip() or has_control_chars(account_id):
        raise RawCommandError(
            "set_wifi.cgi requires a non-empty account_id in trusted configuration"
        )
    return [("userid", account_id)]


def append_auth(path: str, config: VStarcamConfig) -> str:
    """Preserve non-auth query items and append centrally managed auth values."""

    validate_raw_path(path)
    parsed = urlsplit(path)
    existing = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in AUTH_KEYS
    ]
    endpoint_auth = _build_endpoint_auth_params(parsed.path, config)
    query = urlencode(
        [*existing, *endpoint_auth, *build_auth_params(config)],
        doseq=True,
    )
    return urlunsplit(("", "", parsed.path, query, ""))


def format_get_request(path: str, config: VStarcamConfig) -> str:
    """Format the exact payload written to the PPPP command channel."""

    return f"GET {append_auth(path, config)}&"


def format_eye4_auth_request(config: VStarcamConfig) -> str:
    """Build account verification using configured or local owner-zero credentials."""

    auth_params = build_auth_params(config)
    if config.auth_mode == "basic" and config.account_id not in (None, "0"):
        raise RawCommandError(
            "dual authentication for a nonzero account_id requires authorized observed account credentials"
        )
    query = urlencode(
        [
            ("loginAccount", config.account_id or "0"),
            ("loginToken", config.login_token if config.auth_mode == "observed" else ""),
            *auth_params,
        ]
    )
    return f"GET /eye4_authentication.cgi?{query}&"


def build_login_status_path(username: str) -> str:
    """Build the exact status slice used before observed authentication."""

    if not isinstance(username, str) or not username:
        raise RawCommandError("login-status username cannot be empty")
    if has_control_chars(username):
        raise RawCommandError("login-status username contains control characters")
    return f"/get_status.cgi?{urlencode({'name': username})}"


def format_login_status_request(config: VStarcamConfig) -> str:
    """Build the same-session status step required before account authentication."""

    return format_get_request(build_login_status_path(config.username), config)
