"""Offline QR artifacts for reset-camera Wi-Fi provisioning.

The QR code is shown to a camera in its initial Wi-Fi-setup mode. It is not
sent through the established PPPP/CGI session, which does not exist until the
camera has joined a network.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from ._normalize import has_control_chars
from ._private_file import write_private_file
from .errors import ProvisioningConfigurationError
from .wifi import validate_wifi_credentials

_UNKNOWN_BSSID = "NULL"
_BSSID_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def _validate_text(value: str, *, field: str, max_bytes: int, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ProvisioningConfigurationError(f"{field} must be text")
    if not allow_empty and (not value or not value.strip()):
        raise ProvisioningConfigurationError(f"{field} cannot be empty")
    if has_control_chars(value):
        raise ProvisioningConfigurationError(f"{field} contains control characters")
    if len(value.encode("utf-8")) > max_bytes:
        raise ProvisioningConfigurationError(f"{field} must be at most {max_bytes} UTF-8 bytes")


def _normalize_bssid(bssid: str | None) -> str:
    if bssid is None:
        return _UNKNOWN_BSSID
    if not isinstance(bssid, str):
        raise ProvisioningConfigurationError("Wi-Fi BSSID must be text")
    if not bssid.strip():
        return _UNKNOWN_BSSID
    value = bssid.strip()
    if value == _UNKNOWN_BSSID:
        return value
    if not _BSSID_RE.fullmatch(value):
        raise ProvisioningConfigurationError(
            "Wi-Fi BSSID must be 12 hexadecimal characters without separators or 'NULL'"
        )
    return value


def build_static_wifi_qr_payload(
    ssid: str,
    password: str,
    *,
    account_id: str | None = None,
    bssid: str | None = None,
) -> str:
    """Build the compact static Wi-Fi QR payload used during initial setup.

    The field order is part of the supported camera wire format. ``NULL`` is
    the fallback value when the router BSSID is unavailable. An omitted
    ``account_id`` uses the string ``"0"`` in the QR's ``U`` field.

    The returned string contains Wi-Fi credentials and must not be logged or
    printed.
    """

    validate_wifi_credentials(ssid, password)
    if '"' in ssid or "\\" in ssid:
        raise ProvisioningConfigurationError(
            "Wi-Fi SSID cannot contain quote or backslash characters for static provisioning"
        )
    if '"' in password or "\\" in password:
        raise ProvisioningConfigurationError(
            "Wi-Fi password cannot contain quote or backslash characters for static provisioning"
        )
    if account_id is None:
        account_id = "0"
    _validate_text(
        account_id,
        field="account ID",
        max_bytes=128,
    )
    payload = {
        "BS": _normalize_bssid(bssid),
        "P": password,
        "U": account_id,
        "RS": ssid,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _render_svg(payload: str) -> bytes:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise ProvisioningConfigurationError(
            "QR rendering requires the provisioning extra; install vstarcamctl[provisioning]"
        ) from exc

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        border=4,
    )
    code.add_data(payload, optimize=0)
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


def write_static_wifi_qr_svg(
    payload: str,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Render a payload to a private SVG file and return its path.

    The SVG encodes the Wi-Fi password. It is mode ``0600`` on POSIX; on
    Windows, select an output directory whose inherited ACL is private to the
    current user. Existing files are never replaced unless explicitly asked.
    """

    path = Path(output)
    if not path.name:
        raise ProvisioningConfigurationError("QR output path must name a file")
    if not path.parent.is_dir():
        raise ProvisioningConfigurationError(f"QR output directory does not exist: {path.parent}")
    if path.is_symlink():
        raise ProvisioningConfigurationError("refusing to write a QR through a symlink")
    if path.exists() and not overwrite:
        raise ProvisioningConfigurationError(
            f"QR output already exists: {path}; pass --overwrite to replace it"
        )

    artifact = _render_svg(payload)
    try:
        write_private_file(path, artifact, overwrite=overwrite)
    except FileExistsError as exc:
        raise ProvisioningConfigurationError(
            f"QR output already exists: {path}; pass --overwrite to replace it"
        ) from exc
    except OSError as exc:
        raise ProvisioningConfigurationError(f"cannot write QR output: {path}") from exc

    return path
