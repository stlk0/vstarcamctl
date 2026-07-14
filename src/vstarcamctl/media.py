"""RTSP, ONVIF, and audio setting normalization and request construction."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlencode

from ._normalize import as_flag, capability_enabled, has_control_chars
from .errors import MediaConfigurationError

AudioVolumeTarget = Literal["microphone", "speaker"]
AUDIO_VOLUME_MIN = 0
AUDIO_VOLUME_MAX = 31
_AUDIO_VOLUME_PARAMS: dict[AudioVolumeTarget, int] = {
    "microphone": 24,
    "speaker": 25,
}


def _as_flag(value: Any, field: str) -> bool:
    return as_flag(value, field, MediaConfigurationError)


def _validate_text(value: str, field: str, *, maximum: int = 64) -> None:
    if not isinstance(value, str):
        raise MediaConfigurationError(f"{field} must be text")
    if has_control_chars(value):
        raise MediaConfigurationError(f"{field} contains control characters")
    if len(value.encode("utf-8")) > maximum:
        raise MediaConfigurationError(f"{field} must be at most {maximum} UTF-8 bytes")


def validate_rtsp_settings(port: int, username: str, password: str) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise MediaConfigurationError("RTSP port must be an integer from 1 to 65535")
    _validate_text(username, "RTSP username")
    _validate_text(password, "RTSP password")


def build_rtsp_set_path(
    enabled: bool,
    port: int,
    username: str,
    password: str,
) -> str:
    if not isinstance(enabled, bool):
        raise MediaConfigurationError("RTSP enabled state must be boolean")
    validate_rtsp_settings(port, username, password)
    query = urlencode(
        [
            ("rtspenable", int(enabled)),
            ("rtspport", port),
            ("rtspuser", username),
            ("rtsppwd", password),
        ]
    )
    return f"/set_rtsp.cgi?{query}"


def build_onvif_set_path(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise MediaConfigurationError("ONVIF enabled state must be boolean")
    return f"/set_onvif.cgi?{urlencode({'onvifenable': int(enabled)})}"


def build_record_audio_set_path(enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise MediaConfigurationError("record-audio enabled state must be boolean")
    return f"/set_recordsch.cgi?{urlencode({'record_audio': int(enabled)})}"


def validate_audio_volume(target: AudioVolumeTarget, level: int) -> None:
    if target not in _AUDIO_VOLUME_PARAMS:
        raise MediaConfigurationError("audio volume target must be microphone or speaker")
    if (
        not isinstance(level, int)
        or isinstance(level, bool)
        or not AUDIO_VOLUME_MIN <= level <= AUDIO_VOLUME_MAX
    ):
        raise MediaConfigurationError(
            f"audio volume must be an integer from {AUDIO_VOLUME_MIN} to {AUDIO_VOLUME_MAX}"
        )


def build_audio_volume_set_path(target: AudioVolumeTarget, level: int) -> str:
    validate_audio_volume(target, level)
    return "/camera_control.cgi?" + urlencode(
        {"param": _AUDIO_VOLUME_PARAMS[target], "value": level}
    )


def build_audio_stream_path(*, enabled: bool, g711a: bool = True) -> str:
    if not isinstance(enabled, bool) or not isinstance(g711a, bool):
        raise MediaConfigurationError("audio stream flags must be boolean")
    if not enabled:
        return "/audiostream.cgi?streamid=16"
    if g711a:
        return "/audiostream.cgi?streamid=7"
    return "/audiostream.cgi?streamid=1&adpcm_ver=1"


def build_pppp_livestream_path(*, enabled: bool, substream: int = 0) -> str:
    if not isinstance(enabled, bool):
        raise MediaConfigurationError("livestream enabled state must be boolean")
    if not isinstance(substream, int) or isinstance(substream, bool) or substream not in (0, 1):
        raise MediaConfigurationError("livestream substream must be 0 or 1")
    if not enabled:
        return "/livestream.cgi?streamid=16&substream=0"
    return f"/livestream.cgi?streamid=10&substream={substream}"


def parse_rtsp_status(
    payload: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if "rtspenable" not in payload:
        raise MediaConfigurationError("RTSP response does not contain rtspenable")
    port = payload.get("rtspport")
    if not isinstance(port, int) or isinstance(port, bool):
        raise MediaConfigurationError("RTSP response does not contain a numeric rtspport")
    dedicated_username_configured = bool(payload.get("rtspuser"))
    dedicated_password_configured = bool(payload.get("rtsppwd"))
    params = params or {}
    auth_value = params.get("rtsp_auth_enable")
    if auth_value in (0, 1, "0", "1", False, True):
        authentication_enabled = _as_flag(auth_value, "rtsp_auth_enable")
    else:
        authentication_enabled = dedicated_username_configured or dedicated_password_configured

    camera_account_configured = any(
        bool(params.get(f"user{slot}_name")) and bool(params.get(f"user{slot}_pwd"))
        for slot in range(1, 4)
    )
    if not authentication_enabled:
        credential_source = "none"
    elif dedicated_username_configured or dedicated_password_configured:
        credential_source = "dedicated_rtsp"
    elif camera_account_configured:
        credential_source = "camera_account"
    else:
        credential_source = "unknown"

    return {
        "enabled": _as_flag(payload["rtspenable"], "rtspenable"),
        "port": port,
        "authentication_enabled": authentication_enabled,
        "credential_source": credential_source,
        "dedicated_username_configured": dedicated_username_configured,
        "dedicated_password_configured": dedicated_password_configured,
    }


def parse_onvif_status(payload: dict[str, Any]) -> dict[str, Any]:
    if "onvifenable" not in payload:
        raise MediaConfigurationError("ONVIF response does not contain onvifenable")
    result = {"enabled": _as_flag(payload["onvifenable"], "onvifenable")}
    if isinstance(payload.get("onvifport"), int):
        result["port"] = payload["onvifport"]
    return result


def parse_audio_status(
    params: dict[str, Any],
    record: dict[str, Any],
    camera_params: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    camera_params = camera_params or {}
    status = status or {}
    disable_audio = params.get("disable_audio")
    record_audio = record.get("record_audio")
    microphone_volume = camera_params.get("involume")
    speaker_volume = camera_params.get("outvolume")
    g711a = status.get("support_g711a", status.get("support_audio_g711a"))
    echo_cancellation = status.get("EchoCancellationVer")
    result: dict[str, Any] = {
        "capability_flag_present": disable_audio in (0, 1, "0", "1", False, True),
        "recording_setting_present": record_audio in (0, 1, "0", "1", False, True),
        "microphone_volume_present": (
            isinstance(microphone_volume, int)
            and not isinstance(microphone_volume, bool)
            and AUDIO_VOLUME_MIN <= microphone_volume <= AUDIO_VOLUME_MAX
        ),
        "speaker_volume_present": (
            isinstance(speaker_volume, int)
            and not isinstance(speaker_volume, bool)
            and AUDIO_VOLUME_MIN <= speaker_volume <= AUDIO_VOLUME_MAX
        ),
        "g711a_capability_present": g711a is not None,
        "two_way_audio_capability_present": echo_cancellation is not None,
    }
    if result["capability_flag_present"]:
        result["audio_available"] = not _as_flag(disable_audio, "disable_audio")
    if result["recording_setting_present"]:
        result["recording_enabled"] = _as_flag(record_audio, "record_audio")
    if result["microphone_volume_present"]:
        result["microphone_volume"] = microphone_volume
    if result["speaker_volume_present"]:
        result["speaker_volume"] = speaker_volume
    if result["microphone_volume_present"] or result["speaker_volume_present"]:
        result["volume_range"] = {
            "minimum": AUDIO_VOLUME_MIN,
            "maximum": AUDIO_VOLUME_MAX,
        }
    if result["g711a_capability_present"]:
        result["g711a_supported"] = capability_enabled(g711a)
    if result["two_way_audio_capability_present"]:
        result["two_way_audio_supported"] = capability_enabled(echo_cancellation)
    return result
