"""Camera-account validation and exact CGI request construction."""

from __future__ import annotations

from urllib.parse import urlencode

from ._normalize import has_control_chars
from .errors import AccountConfigurationError

CAMERA_REBOOT_PATH = "/reboot.cgi"


def _validate_text(name: str, value: str, *, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise AccountConfigurationError(f"camera account {name} must not be empty")
    if has_control_chars(value):
        raise AccountConfigurationError(f"camera account {name} contains control characters")
    if len(value.encode("utf-8")) > max_bytes:
        raise AccountConfigurationError(f"camera account {name} is too long")
    return value


def _build_external_path(username: str, password: str) -> str:
    query = urlencode(
        [
            ("pwd_change_realtime", 1),
            ("ExUser", username),
            ("ExPwd", password),
            ("ExUserSwitch", 1),
        ]
    )
    return f"/set_users.cgi?{query}"


def build_camera_account_password_set_path(username: str, password: str) -> str:
    username = _validate_text("username", username, max_bytes=32)
    if not isinstance(password, str):
        raise AccountConfigurationError("camera account password must be text")
    if has_control_chars(password):
        raise AccountConfigurationError("camera account password contains control characters")
    if not 8 <= len(password) <= 31:
        raise AccountConfigurationError("camera account password must contain 8 to 31 characters")
    return _build_external_path(username, password)


def build_camera_account_plaintext_enable_path(username: str) -> str:
    """Build the first-enable step that deliberately carries an empty ExPwd."""

    return _build_external_path(_validate_text("username", username, max_bytes=32), "")


def build_camera_owner_set_path(account_id: str, login_hash: str) -> str:
    """Build the owner step from already-authorized private configuration."""

    query = urlencode(
        [
            ("pwd_change_realtime", 1),
            ("OwnerUser", _validate_text("owner ID", account_id, max_bytes=256)),
            ("OwnerPwd", _validate_text("owner credential", login_hash, max_bytes=256)),
        ]
    )
    return f"/set_users.cgi?{query}"


def validate_account_step_response(payload: dict, *, dual_authentication: int) -> dict[str, int]:
    result = payload.get("result")
    state = payload.get("DualAuthentication")
    if (
        type(result) is not int
        or result != 0
        or type(state) is not int
        or state != dual_authentication
    ):
        raise AccountConfigurationError(
            "camera account acknowledgement did not confirm the expected authentication state"
        )
    return {"result": 0, "DualAuthentication": dual_authentication}
