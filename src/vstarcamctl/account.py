"""Camera-account password validation and CGI request construction."""

from __future__ import annotations

from urllib.parse import urlencode

from ._normalize import has_control_chars
from .errors import AccountConfigurationError


def validate_camera_account_password(username: str, password: str) -> None:
    if not isinstance(username, str) or not username:
        raise AccountConfigurationError("camera account username must not be empty")
    if not isinstance(password, str):
        raise AccountConfigurationError("camera account password must be text")
    for field, value in (("username", username), ("password", password)):
        if has_control_chars(value):
            raise AccountConfigurationError(f"camera account {field} contains control characters")
    if len(username.encode("utf-8")) > 32:
        raise AccountConfigurationError("camera account username is too long")
    if not 8 <= len(password) <= 31:
        raise AccountConfigurationError("camera account password must contain 8 to 31 characters")


def build_camera_account_password_set_path(username: str, password: str) -> str:
    validate_camera_account_password(username, password)
    query = urlencode(
        [
            ("pwd_change_realtime", 1),
            ("ExUser", username),
            ("ExPwd", password),
        ]
    )
    return f"/set_users.cgi?{query}"
