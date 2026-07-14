"""Validation, authentication, and formatting of tunneled CGI requests."""

from __future__ import annotations

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
    if not parsed.path.endswith(".cgi"):
        raise RawCommandError("only CGI endpoints ending in .cgi are accepted")
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
    params.extend([("user", username), ("pwd", password)])
    return params


def append_auth(path: str, config: VStarcamConfig) -> str:
    """Preserve non-auth query items and append centrally managed auth values."""

    validate_raw_path(path)
    parsed = urlsplit(path)
    existing = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in AUTH_KEYS
    ]
    query = urlencode([*existing, *build_auth_params(config)], doseq=True)
    return urlunsplit(("", "", parsed.path, query, ""))


def format_get_request(path: str, config: VStarcamConfig) -> str:
    """Format the exact payload written to the PPPP command channel."""

    return f"GET {append_auth(path, config)}&"


def format_eye4_auth_request(config: VStarcamConfig) -> str:
    """Build the dual-auth preflight for observed authentication mode."""

    config.validate(require_auth=True)
    if not config.login_token:
        raise RawCommandError("observed dual auth requires login_token")
    query = urlencode(
        [
            ("loginAccount", config.account_id or ""),
            ("loginToken", config.login_token),
            *build_auth_params(config),
        ]
    )
    return f"GET /eye4_authentication.cgi?{query}&"
