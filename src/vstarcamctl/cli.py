"""Command line interface for safe local camera control."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time as system_time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

from . import __version__
from ._normalize import require_positive_finite
from .account import build_camera_account_password_set_path
from .actuator_state import build_alarm_led_set_path
from .audio_talk import iter_adpcm_talk_frames
from .camera import VStarcamCamera
from .cgi import format_get_request
from .config import DEFAULTS, VStarcamConfig, load_config, save_discovered_profile
from .detection import (
    build_human_detection_set_path,
    build_human_frame_set_path,
    build_human_sensitivity_set_path,
    build_human_tracking_set_path,
    build_human_zoom_tracking_set_path,
)
from .discovery import discover_camera, discover_cameras
from .errors import (
    ConfigError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
    ServiceChangeUncertainError,
    TimeConfigurationError,
    VStarcamError,
)
from .media import (
    build_audio_volume_set_path,
    build_onvif_set_path,
    build_record_audio_set_path,
    build_rtsp_set_path,
)
from .media_stream import (
    RTSPStream,
    capture_rtsp_snapshot,
    play_rtsp,
    prepare_media_output,
    probe_rtsp,
    record_rtsp,
)
from .night_vision import build_infrared_light_set_path, build_night_vision_set_paths
from .osd import build_logo_osd_set_path, build_osd_12h_set_path
from .provisioning import build_static_wifi_qr_payload, write_static_wifi_qr_svg
from .ptz import build_ptz_start_path, build_ptz_stop_path, validate_ptz_duration
from .secrets import SecretMaskingFilter, mask_secrets, redact_data
from .time_settings import build_time_settings_set_path, parse_utc_offset
from .transport import DiscoveredCamera
from .wifi import build_wifi_set_path

_Result = TypeVar("_Result")


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    require_positive_finite(seconds, "seconds", argparse.ArgumentTypeError)
    return seconds


def _timezone_arg(value: str) -> int:
    try:
        return parse_utc_offset(value)
    except TimeConfigurationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_stream_input_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--quality",
        choices=("main", "sub"),
        default="main",
        help="main is full resolution; sub uses less bandwidth",
    )
    command.add_argument(
        "--rtsp-transport",
        choices=("tcp", "udp"),
        default="tcp",
        help="TCP is the reliable default for LAN use",
    )
    command.add_argument(
        "--port",
        type=int,
        help="skip the PPPP status lookup and use this RTSP port",
    )
    command.add_argument(
        "--rtsp-username",
        help="dedicated RTSP username; defaults to the camera account when applicable",
    )
    command.add_argument(
        "--rtsp-password",
        help="prefer VSTARCAM_RTSP_PASSWORD to avoid shell history",
    )


def _add_state(command: argparse.ArgumentParser) -> None:
    command.add_argument("state", choices=("on", "off"))


def _add_safety_options(
    command: argparse.ArgumentParser,
    *,
    dry_run: bool = False,
    experimental: bool = False,
    confirm: bool = False,
    recovery_ready: bool = False,
) -> None:
    if dry_run:
        command.add_argument("--dry-run", action="store_true")
    if experimental:
        command.add_argument("--experimental", action="store_true")
    if confirm:
        command.add_argument("--confirm", action="store_true")
    if recovery_ready:
        command.add_argument(
            "--recovery-ready",
            action="store_true",
            help="acknowledge that physical reset/recovery has been verified",
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vstarcamctl", description="Local control for VStarcam-compatible PPPP/CGI cameras"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="ignored local YAML file")
    parser.add_argument("--host")
    parser.add_argument(
        "--source-address",
        help="bind LAN discovery and the PPPP session to this local IPv4 address",
    )
    parser.add_argument(
        "--device-id",
        help="PPPP transport device ID returned by LAN discovery",
    )
    parser.add_argument("--username")
    parser.add_argument("--password", help="prefer VSTARCAM_PASSWORD to avoid shell history")
    parser.add_argument(
        "--psk",
        help=(
            "override the transport seed; omit to detect built-in profiles "
            "(this is not a camera password)"
        ),
    )
    parser.add_argument("--udp-port", type=int)
    parser.add_argument("--discovery-port", type=int)
    parser.add_argument("--auth-mode", choices=("basic", "observed"))
    parser.add_argument("--account-id")
    parser.add_argument("--login-hash")
    parser.add_argument("--login-token")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--retries", type=int)
    parser.add_argument("--debug", action="store_true")

    sub = parser.add_subparsers(dest="action", required=True)
    discover = sub.add_parser("discover", help="discover LAN PPPP cameras")
    discover.add_argument("--seconds", type=float, default=3.0)
    discover.add_argument(
        "--save-config",
        type=Path,
        help="create a new private profile for one discovered camera",
    )
    discover.set_defaults(handler=_handle_discover)

    status = sub.add_parser("status", help="read get_status.cgi")
    status.set_defaults(handler=_handle_read, camera_method="get_status")
    params = sub.add_parser("params", help="read get_params.cgi")
    params.set_defaults(handler=_handle_params)

    device = sub.add_parser("device", help="inspect allowlisted device information")
    device_sub = device.add_subparsers(dest="device_action", required=True)
    device_info = device_sub.add_parser("info", help="read reported software versions")
    device_info.set_defaults(handler=_handle_read, camera_method="get_device_software_info")

    imaging = sub.add_parser("imaging", help="inspect reported brightness and contrast")
    imaging_sub = imaging.add_subparsers(dest="imaging_action", required=True)
    imaging_status = imaging_sub.add_parser("status", help="read image adjustments")
    imaging_status.set_defaults(handler=_handle_read, camera_method="get_image_adjustments")

    for name, status_method in (
        ("siren", "get_siren_state"),
        ("light", "get_light_state"),
    ):
        control = sub.add_parser(name)
        control_sub = control.add_subparsers(dest=f"{name}_action", required=True)
        for state in ("on", "off"):
            state_command = control_sub.add_parser(state)
            state_command.set_defaults(
                handler=_handle_confirmed_state,
                camera_method=f"set_{name}",
                state=state,
            )
        state_status = control_sub.add_parser("status", help=f"read the {name} state")
        state_status.set_defaults(
            handler=_handle_boolean_status,
            camera_method=status_method,
        )

    night = sub.add_parser("night", help="inspect or experimentally set night vision")
    night_sub = night.add_subparsers(dest="night_action", required=True)
    night_status = night_sub.add_parser("status", help="read the normalized night-vision mode")
    night_status.set_defaults(handler=_handle_read, camera_method="get_night_vision_settings")
    night_set = night_sub.add_parser(
        "set", help="send the documented two-step night-mode transition once"
    )
    night_set.add_argument("mode", choices=("black-white", "starlight", "full-color", "smart"))
    _add_safety_options(night_set, dry_run=True, experimental=True)
    night_set.set_defaults(handler=_handle_night_set)

    infrared = sub.add_parser(
        "ir", help="inspect or experimentally control the infrared illuminator"
    )
    infrared_sub = infrared.add_subparsers(dest="ir_action", required=True)
    infrared_status = infrared_sub.add_parser(
        "status", help="read the logical command-2120 infrared control flag"
    )
    infrared_status.set_defaults(handler=_handle_read, camera_method="get_infrared_light_settings")
    infrared_set = infrared_sub.add_parser(
        "set", help="send the infrared illuminator candidate once"
    )
    _add_state(infrared_set)
    _add_safety_options(infrared_set, dry_run=True, experimental=True)
    infrared_set.set_defaults(
        handler=_handle_state_write,
        camera_method="set_infrared_light",
        path_builder=build_infrared_light_set_path,
        note="verify with ir status before another infrared-light write",
    )

    alarm_led = sub.add_parser(
        "alarm-led",
        help="inspect or experimentally control the red/blue alarm indicator",
    )
    alarm_led_sub = alarm_led.add_subparsers(dest="alarm_led_action", required=True)
    alarm_led_status = alarm_led_sub.add_parser("status", help="read the alarm indicator state")
    alarm_led_status.set_defaults(
        handler=_handle_boolean_status,
        camera_method="get_alarm_led",
    )
    for state in ("on", "off"):
        alarm_led_set = alarm_led_sub.add_parser(
            state,
            help=f"turn the alarm indicator {state} once",
        )
        _add_safety_options(alarm_led_set, dry_run=True, experimental=True)
        alarm_led_set.set_defaults(
            handler=_handle_state_write,
            state=state,
            camera_method="set_alarm_led",
            path_builder=build_alarm_led_set_path,
            note="verify with alarm-led status before another write",
        )

    time_control = sub.add_parser("time", help="inspect or configure clock, timezone, and NTP")
    time_sub = time_control.add_subparsers(dest="time_action", required=True)
    time_status = time_sub.add_parser(
        "status", help="read normalized time fields from get_params.cgi"
    )
    time_status.set_defaults(handler=_handle_read, camera_method="get_time_settings")
    time_set = time_sub.add_parser("set", help="sync the clock and send the datetime setting once")
    time_set.add_argument(
        "--timezone",
        type=_timezone_arg,
        help="fixed offset such as UTC+05:30; preserves the current value when omitted",
    )
    time_set.add_argument(
        "--ntp",
        choices=("on", "off"),
        help="preserve the current NTP state when omitted",
    )
    time_set.add_argument(
        "--ntp-server",
        help="hostname or IP address; preserves the current value when omitted",
    )
    time_set.add_argument(
        "--unix-time",
        type=int,
        help="seconds since Unix epoch; defaults to the client clock",
    )
    _add_safety_options(time_set, dry_run=True)
    time_set.set_defaults(handler=_handle_time_set)

    raw = sub.add_parser("raw", help="send a relative CGI path through PPPP")
    raw.add_argument("path")
    _add_safety_options(raw, dry_run=True, experimental=True, confirm=True)
    raw.set_defaults(handler=_handle_raw)

    rtsp = sub.add_parser("rtsp", help="inspect or experimentally configure RTSP")
    rtsp_sub = rtsp.add_subparsers(dest="rtsp_action", required=True)
    rtsp_status = rtsp_sub.add_parser("status", help="read reported RTSP configuration")
    rtsp_status.set_defaults(handler=_handle_read, camera_method="get_rtsp_settings")
    rtsp_set = rtsp_sub.add_parser("set", help="send the RTSP setting candidate once")
    _add_state(rtsp_set)
    rtsp_set.add_argument("--port", type=int, help="preserve the current port when omitted")
    rtsp_set.add_argument("--rtsp-username", help="preserve the current username when omitted")
    rtsp_set.add_argument(
        "--rtsp-password",
        help="prefer VSTARCAM_RTSP_PASSWORD; preserve the current password when omitted",
    )
    _add_safety_options(rtsp_set, dry_run=True, experimental=True, confirm=True)
    rtsp_set.set_defaults(handler=_handle_rtsp_set)

    account = sub.add_parser(
        "account",
        help="experimentally change the camera account used by RTSP",
    )
    account_sub = account.add_subparsers(dest="account_action", required=True)
    account_password = account_sub.add_parser(
        "password",
        help="change the camera WebPwd used by RTSP once",
    )
    account_password.add_argument(
        "--new-password",
        help="prefer VSTARCAM_NEW_CAMERA_PASSWORD to avoid shell history",
    )
    _add_safety_options(
        account_password,
        dry_run=True,
        experimental=True,
        confirm=True,
        recovery_ready=True,
    )
    account_password.set_defaults(handler=_handle_account_password)

    onvif = sub.add_parser("onvif", help="inspect or experimentally configure ONVIF")
    onvif_sub = onvif.add_subparsers(dest="onvif_action", required=True)
    onvif_status = onvif_sub.add_parser("status", help="read reported ONVIF configuration")
    onvif_status.set_defaults(handler=_handle_read, camera_method="get_onvif_settings")
    onvif_set = onvif_sub.add_parser("set", help="send the ONVIF setting candidate once")
    _add_state(onvif_set)
    _add_safety_options(onvif_set, dry_run=True, experimental=True, confirm=True)
    onvif_set.set_defaults(
        handler=_handle_state_write,
        camera_method="set_onvif",
        path_builder=build_onvif_set_path,
        note="verify with onvif status before relying on the new setting",
        always_uncertain=True,
    )

    audio = sub.add_parser("audio", help="live audio, two-way talk, volume, and recording")
    audio_sub = audio.add_subparsers(dest="audio_action", required=True)
    audio_status = audio_sub.add_parser(
        "status", help="read audio capability and record-audio state"
    )
    audio_status.set_defaults(handler=_handle_read, camera_method="get_audio_settings")
    recording = audio_sub.add_parser(
        "recording",
        help="experimentally include or exclude audio in camera recordings",
    )
    _add_state(recording)
    _add_safety_options(recording, dry_run=True, experimental=True, confirm=True)
    recording.set_defaults(
        handler=_handle_state_write,
        camera_method="set_record_audio",
        path_builder=build_record_audio_set_path,
        note="verify with audio status before relying on the new setting",
        always_uncertain=True,
    )

    volume = audio_sub.add_parser(
        "volume", help="experimentally set camera microphone or speaker volume"
    )
    volume.add_argument("target", choices=("microphone", "speaker"))
    volume.add_argument("level", type=int, help="camera level from 0 to 31")
    _add_safety_options(volume, dry_run=True, experimental=True)
    volume.set_defaults(handler=_handle_audio_volume)

    listen = audio_sub.add_parser("listen", help="play live camera audio with ffplay")
    _add_stream_input_options(listen)
    listen.add_argument("--duration", type=_positive_seconds)
    listen.add_argument("--local-volume", type=int, default=100, help="ffplay volume 0-100")
    listen.set_defaults(handler=_handle_audio_listen)

    talk = audio_sub.add_parser(
        "talk", help="experimentally transmit half-duplex ADPCM audio to the camera"
    )
    talk.add_argument("source", help="FFmpeg input, for example default or an audio file")
    talk.add_argument("--input-format", help="FFmpeg input format, for example pulse or alsa")
    talk.add_argument("--duration", type=_positive_seconds, required=True)
    talk.add_argument(
        "--max-speaker-volume",
        type=int,
        required=True,
        help="refuse talk if the reported camera speaker level exceeds this 0-31 limit",
    )
    _add_safety_options(talk, experimental=True, confirm=True)
    talk.set_defaults(handler=_handle_audio_talk)

    motion = sub.add_parser("motion", help="inspect or experimentally configure motion detection")
    motion_sub = motion.add_subparsers(dest="motion_action", required=True)
    motion_status = motion_sub.add_parser(
        "status", help="read motion state and sensitivity from get_params.cgi"
    )
    motion_status.set_defaults(
        handler=_handle_read,
        camera_method="get_motion_detection_settings",
    )
    motion_set = motion_sub.add_parser("set", help="send the motion setting candidate once")
    _add_state(motion_set)
    motion_set.add_argument(
        "--sensitivity",
        type=int,
        help="0 is highest and 9 is lowest; preserve the current value when omitted",
    )
    _add_safety_options(motion_set, experimental=True, confirm=True)
    motion_set.set_defaults(handler=_handle_motion_set)

    human = sub.add_parser("human", help="inspect or experimentally configure human detection")
    human_sub = human.add_subparsers(dest="human_action", required=True)
    human_status = human_sub.add_parser(
        "status", help="read humanoid frame, sensitivity, and zoom fields"
    )
    human_status.add_argument(
        "--include-tracking",
        action="store_true",
        help="also run the validated 2127 tracking getter",
    )
    human_status.set_defaults(handler=_handle_human_status)
    human_detection = human_sub.add_parser(
        "detection", help="configure the main humanoid detector once"
    )
    _add_state(human_detection)
    human_detection.add_argument("--sensitivity", type=int, required=True, help="level 1-3")
    human_detection.add_argument("--distance", type=int, required=True, help="level 1-3")
    _add_safety_options(human_detection, dry_run=True, experimental=True, confirm=True)
    human_detection.set_defaults(handler=_handle_human_detection)

    human_sensitivity = human_sub.add_parser(
        "sensitivity", help="configure the 2126 humanoid sensitivity once"
    )
    human_sensitivity.add_argument("level", type=int, help="device-specific level 0-3")
    _add_safety_options(human_sensitivity, dry_run=True)
    human_sensitivity.set_defaults(handler=_handle_human_sensitivity)

    for action_name, help_text, camera_method, path_builder, experimental in (
        (
            "frame",
            "show or hide humanoid target frames",
            "set_human_frame",
            build_human_frame_set_path,
            False,
        ),
        (
            "tracking",
            "enable or disable humanoid PTZ tracking",
            "set_human_tracking",
            build_human_tracking_set_path,
            True,
        ),
        (
            "zoom-tracking",
            "enable or disable humanoid zoom tracking",
            "set_human_zoom_tracking",
            build_human_zoom_tracking_set_path,
            True,
        ),
    ):
        control = human_sub.add_parser(action_name, help=help_text)
        _add_state(control)
        _add_safety_options(
            control,
            dry_run=True,
            experimental=experimental,
            confirm=experimental,
        )
        control.set_defaults(
            handler=_handle_state_write,
            camera_method=camera_method,
            path_builder=path_builder,
            note="verify with human status before another detection write",
            always_uncertain=experimental,
        )

    ptz = sub.add_parser("ptz", help="experimentally control manual pan and tilt")
    ptz_sub = ptz.add_subparsers(dest="ptz_action", required=True)
    ptz_move = ptz_sub.add_parser("move", help="move for a bounded duration, then stop")
    ptz_move.add_argument("direction", choices=("up", "down", "left", "right"))
    ptz_move.add_argument("--duration", type=float, required=True, help="positive seconds")
    _add_safety_options(ptz_move, dry_run=True, experimental=True, confirm=True)
    ptz_move.set_defaults(handler=_handle_ptz_move)

    ptz_stop = ptz_sub.add_parser("stop", help="send one direction-specific emergency stop")
    ptz_stop.add_argument("direction", choices=("up", "down", "left", "right"))
    _add_safety_options(ptz_stop, dry_run=True, experimental=True)
    ptz_stop.set_defaults(handler=_handle_ptz_stop)

    osd = sub.add_parser("osd", help="inspect or experimentally configure the video overlay")
    osd_sub = osd.add_subparsers(dest="osd_action", required=True)
    osd_clock = osd_sub.add_parser("clock", help="inspect or configure the overlay clock format")
    osd_clock_sub = osd_clock.add_subparsers(dest="osd_clock_action", required=True)
    osd_clock_status = osd_clock_sub.add_parser("status", help="read the overlay clock format")
    osd_clock_status.set_defaults(handler=_handle_osd_clock_status)
    for mode, twelve_hour in (("12h", True), ("24h", False)):
        osd_clock_set = osd_clock_sub.add_parser(
            mode,
            help=f"set the overlay clock to {mode} format once",
        )
        _add_safety_options(osd_clock_set, dry_run=True)
        osd_clock_set.set_defaults(
            handler=_handle_osd_clock_set,
            twelve_hour=twelve_hour,
        )

    osd_timestamp = osd_sub.add_parser("timestamp", help="inspect timestamp visibility")
    osd_timestamp_sub = osd_timestamp.add_subparsers(dest="osd_timestamp_action", required=True)
    osd_timestamp_status = osd_timestamp_sub.add_parser(
        "status", help="read timestamp overlay visibility"
    )
    osd_timestamp_status.set_defaults(
        handler=_handle_boolean_status,
        camera_method="get_timestamp_osd",
    )

    osd_logo = osd_sub.add_parser("logo", help="inspect or configure the camera logo overlay")
    osd_logo_sub = osd_logo.add_subparsers(dest="osd_logo_action", required=True)
    osd_logo_status = osd_logo_sub.add_parser("status", help="read camera logo visibility")
    osd_logo_status.set_defaults(
        handler=_handle_boolean_status,
        camera_method="get_logo_osd",
    )
    for state in ("on", "off"):
        osd_logo_set = osd_logo_sub.add_parser(
            state,
            help=f"turn the camera logo overlay {state} once",
        )
        _add_safety_options(osd_logo_set, dry_run=True, experimental=True)
        osd_logo_set.set_defaults(
            handler=_handle_state_write,
            state=state,
            camera_method="set_logo_osd",
            path_builder=build_logo_osd_set_path,
            note="verify with osd logo status before another write",
        )

    stream = sub.add_parser("stream", help="receive camera video and audio")
    stream_sub = stream.add_subparsers(dest="stream_action", required=True)

    stream_probe = stream_sub.add_parser(
        "probe",
        help="inspect available video/audio codecs with ffprobe",
    )
    _add_stream_input_options(stream_probe)
    stream_probe.add_argument("--probe-timeout", type=_positive_seconds)
    stream_probe.set_defaults(handler=_handle_stream)

    stream_record = stream_sub.add_parser(
        "record",
        help="save a bounded RTSP video/audio segment with ffmpeg",
    )
    _add_stream_input_options(stream_record)
    stream_record.add_argument("output", type=Path)
    stream_record.add_argument("--duration", type=_positive_seconds, default=30.0)
    stream_record.add_argument(
        "--media",
        choices=("both", "video", "audio"),
        default="both",
    )
    stream_record.add_argument("--overwrite", action="store_true")
    stream_record.set_defaults(handler=_handle_stream)

    stream_snapshot = stream_sub.add_parser(
        "snapshot",
        help="save one decoded video frame with ffmpeg",
    )
    _add_stream_input_options(stream_snapshot)
    stream_snapshot.add_argument(
        "--source",
        choices=("rtsp", "pppp"),
        default="rtsp",
        help="RTSP by default; PPPP uses the experimental bounded camera path",
    )
    stream_snapshot.add_argument("output", type=Path)
    stream_snapshot.add_argument("--snapshot-timeout", type=_positive_seconds)
    stream_snapshot.add_argument("--overwrite", action="store_true")
    _add_safety_options(stream_snapshot, experimental=True)
    stream_snapshot.set_defaults(handler=_handle_stream)

    wifi = sub.add_parser("wifi", help="inspect or experimentally change Wi-Fi")
    wifi_sub = wifi.add_subparsers(dest="wifi_action", required=True)
    wifi_status = wifi_sub.add_parser(
        "status", help="read current Wi-Fi fields from get_params.cgi"
    )
    wifi_status.set_defaults(handler=_handle_read, camera_method="get_wifi_status")
    wifi_scan = wifi_sub.add_parser("scan", help="request and parse a local access-point scan")
    wifi_scan.set_defaults(handler=_handle_wifi_scan)
    wifi_set = wifi_sub.add_parser(
        "set",
        help="send the account-bound, unconfirmed set_wifi.cgi candidate once",
    )
    wifi_set.add_argument("--ssid", required=True)
    wifi_set.add_argument(
        "--wifi-password",
        help="prefer VSTARCAM_WIFI_PASSWORD to avoid shell history",
    )
    wifi_set.add_argument("--channel", type=int, help="otherwise inferred from an exact SSID scan")
    wifi_set.add_argument(
        "--auth-type",
        type=int,
        help="camera ap_security/authtype value; otherwise inferred from an exact SSID scan",
    )
    _add_safety_options(
        wifi_set,
        dry_run=True,
        experimental=True,
        confirm=True,
        recovery_ready=True,
    )
    wifi_set.set_defaults(handler=_handle_wifi_set)

    provision = sub.add_parser(
        "provision",
        help="experimentally prepare Wi-Fi setup for a reset camera",
    )
    provision_sub = provision.add_subparsers(dest="provision_action", required=True)
    provision_qr = provision_sub.add_parser(
        "qr",
        help="render the static Wi-Fi setup QR that a reset camera scans",
    )
    provision_qr.add_argument("--ssid", required=True)
    provision_qr.add_argument(
        "--wifi-password",
        help="prefer VSTARCAM_WIFI_PASSWORD to avoid shell history",
    )
    provision_qr.add_argument(
        "--bssid",
        help="router BSSID as 12 hexadecimal characters; omit for the protocol NULL fallback",
    )
    provision_qr.add_argument(
        "--output",
        type=Path,
        required=True,
        help="private SVG output path; it encodes the Wi-Fi password",
    )
    provision_qr.add_argument("--overwrite", action="store_true")
    _add_safety_options(
        provision_qr,
        experimental=True,
        confirm=True,
        recovery_ready=True,
    )
    provision_qr.set_defaults(handler=_handle_provision_qr)

    return parser


def _configure_logging(debug: bool) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretMaskingFilter())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
        force=True,
    )
    if not debug:
        logging.getLogger("aiopppp").setLevel(logging.WARNING)


def _config_from_args(args: argparse.Namespace) -> VStarcamConfig:
    values = {key: getattr(args, key, None) for key in DEFAULTS}
    return load_config(
        cli=values,
        yaml_path=args.config,
        use_default_file=args.action != "discover",
    )


def _wifi_password(args: argparse.Namespace) -> str:
    password = args.wifi_password or os.environ.get("VSTARCAM_WIFI_PASSWORD")
    if password is None:
        raise ConfigError(
            "Wi-Fi password is required via --wifi-password or VSTARCAM_WIFI_PASSWORD"
        )
    return password


def _rtsp_password(args: argparse.Namespace) -> str | None:
    if args.rtsp_password is not None:
        return args.rtsp_password
    return os.environ.get("VSTARCAM_RTSP_PASSWORD")


def _rtsp_username(args: argparse.Namespace) -> str | None:
    if args.rtsp_username is not None:
        return args.rtsp_username
    return os.environ.get("VSTARCAM_RTSP_USERNAME")


def _new_camera_password(args: argparse.Namespace) -> str:
    password = args.new_password or os.environ.get("VSTARCAM_NEW_CAMERA_PASSWORD")
    if password is None:
        raise ConfigError(
            "new camera password is required via --new-password or VSTARCAM_NEW_CAMERA_PASSWORD"
        )
    return password


def _print_json(value) -> None:
    print(json.dumps(redact_data(value), indent=2, ensure_ascii=False, sort_keys=True))


def _discovered_camera_output(camera: DiscoveredCamera) -> dict[str, object]:
    return {key: value for key, value in asdict(camera).items() if key != "psk"}


def _enabled(args: argparse.Namespace) -> bool:
    return args.state == "on"


def _format_requests(config: VStarcamConfig, paths: tuple[str, ...]) -> list[str]:
    return [mask_secrets(format_get_request(path, config)) for path in paths]


def _print_request(config: VStarcamConfig, path: str) -> None:
    print(mask_secrets(format_get_request(path, config)))


def _print_response(result: object, note: str) -> None:
    _print_json({"response": result, "note": note})


async def _camera_call(
    config: VStarcamConfig, operation: Callable[[VStarcamCamera], Awaitable[_Result]]
) -> _Result:
    camera = VStarcamCamera(config)
    try:
        return await operation(camera)
    finally:
        await camera.close()


async def _get_rtsp_stream(
    args: argparse.Namespace,
    config: VStarcamConfig,
) -> RTSPStream:
    return await _camera_call(
        config,
        lambda camera: camera.get_rtsp_stream(
            quality=args.quality,
            transport=args.rtsp_transport,
            port=args.port,
            username=_rtsp_username(args),
            password=_rtsp_password(args),
        ),
    )


async def _handle_discover(args: argparse.Namespace, config: VStarcamConfig) -> None:
    options = {
        "timeout": args.seconds,
        "host": args.host,
        "source_address": config.source_address,
        "psk": config.psk,
        "discovery_port": config.discovery_port,
        "udp_port": config.udp_port,
    }
    if args.save_config is None:
        if args.device_id is not None:
            selected = await discover_camera(device_id=args.device_id, **options)
            _print_json([_discovered_camera_output(selected)])
            return
        _print_json([_discovered_camera_output(item) for item in await discover_cameras(**options)])
        return

    selected = await discover_camera(device_id=args.device_id, **options)
    profile = save_discovered_profile(
        args.save_config,
        host=selected.host,
        device_id=selected.device_id,
        psk=selected.psk if selected.psk is not None else config.psk,
        udp_port=selected.port,
        discovery_port=config.discovery_port,
        source_address=config.source_address,
    )
    _print_json(
        {
            "camera": _discovered_camera_output(selected),
            "config": str(profile),
            "profile_created": True,
            "authentication": "not discovered",
        }
    )


async def _handle_read(args: argparse.Namespace, config: VStarcamConfig) -> None:
    async with VStarcamCamera(config) as camera:
        _print_json(await getattr(camera, args.camera_method)())


async def _handle_boolean_status(args: argparse.Namespace, config: VStarcamConfig) -> None:
    result = await _camera_call(
        config,
        lambda camera: getattr(camera, args.camera_method)(),
    )
    _print_json({"enabled": result})


async def _handle_params(_args: argparse.Namespace, config: VStarcamConfig) -> None:
    async with VStarcamCamera(config) as camera:
        params = await camera.get_params()
    _print_json({"field_count": len(params)})


async def _handle_confirmed_state(args: argparse.Namespace, config: VStarcamConfig) -> None:
    async with VStarcamCamera(config) as camera:
        _print_json(await getattr(camera, args.camera_method)(_enabled(args)))


async def _handle_stream(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.stream_action != "probe":
        prepare_media_output(args.output, overwrite=args.overwrite)
    if args.stream_action == "snapshot" and args.source == "pppp":
        if (
            args.port is not None
            or args.rtsp_username is not None
            or args.rtsp_password is not None
            or args.quality != "main"
            or args.rtsp_transport != "tcp"
        ):
            raise ConfigError("RTSP input options cannot be used with --source pppp")
        result = await _camera_call(
            config,
            lambda camera: camera.capture_pppp_snapshot(
                args.output,
                timeout=(
                    args.snapshot_timeout if args.snapshot_timeout is not None else config.timeout
                ),
                overwrite=args.overwrite,
                experimental=args.experimental,
            ),
        )
        _print_json(result)
        return

    stream = await _get_rtsp_stream(args, config)
    if args.stream_action == "probe":
        timeout = args.probe_timeout if args.probe_timeout is not None else config.timeout
        result = await probe_rtsp(stream, timeout=timeout)
    elif args.stream_action == "record":
        result = await record_rtsp(
            stream,
            args.output,
            duration=args.duration,
            media=args.media,
            overwrite=args.overwrite,
        )
    else:
        result = await capture_rtsp_snapshot(
            stream,
            args.output,
            timeout=(
                args.snapshot_timeout if args.snapshot_timeout is not None else config.timeout
            ),
            overwrite=args.overwrite,
        )
    _print_json(result)


async def _handle_audio_listen(args: argparse.Namespace, config: VStarcamConfig) -> None:
    stream = await _get_rtsp_stream(args, config)
    result = await play_rtsp(
        stream,
        media="audio",
        duration=args.duration,
        volume=args.local_volume,
    )
    _print_json(result)


async def _handle_audio_talk(args: argparse.Namespace, config: VStarcamConfig) -> None:
    result = await _camera_call(
        config,
        lambda camera: camera.send_talk_audio(
            iter_adpcm_talk_frames(
                args.source,
                input_format=args.input_format,
                duration=args.duration,
            ),
            duration=args.duration,
            max_speaker_volume=args.max_speaker_volume,
            experimental=args.experimental,
            confirm=args.confirm,
        ),
    )
    _print_json(result)


async def _handle_raw(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_request(config, args.path)
        return
    response = await _camera_call(
        config,
        lambda camera: camera.send_raw_cgi(
            args.path, experimental=args.experimental, confirm=args.confirm
        ),
    )
    print(mask_secrets(response))


async def _handle_night_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_json(
            {
                "requests": _format_requests(
                    config,
                    build_night_vision_set_paths(args.mode),
                )
            }
        )
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_night_vision(args.mode, experimental=args.experimental),
    )
    _print_json(
        {
            "responses": result,
            "note": "verify with night status before another night-mode write",
        }
    )


async def _handle_time_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    ntp_enabled = None if args.ntp is None else args.ntp == "on"
    if args.dry_run:
        if args.timezone is None or ntp_enabled is None or args.ntp_server is None:
            raise ConfigError("time dry-run requires --timezone, --ntp, and --ntp-server")
        unix_time = args.unix_time if args.unix_time is not None else int(system_time.time())
        path = build_time_settings_set_path(
            args.timezone,
            ntp_enabled,
            args.ntp_server,
            unix_time,
        )
        _print_request(config, path)
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_time_settings(
            timezone_offset_seconds=args.timezone,
            ntp_enabled=ntp_enabled,
            ntp_server=args.ntp_server,
            unix_time=args.unix_time,
        ),
    )
    _print_response(
        result,
        "verify with time status before another time-setting write",
    )


async def _handle_rtsp_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    rtsp_username = _rtsp_username(args)
    rtsp_password = _rtsp_password(args)
    enabled = _enabled(args)
    if args.dry_run:
        if args.port is None or rtsp_username is None or rtsp_password is None:
            raise ConfigError(
                "RTSP dry-run requires --port, --rtsp-username, and "
                "--rtsp-password, or their VSTARCAM_RTSP_* environment variables"
            )
        _print_request(
            config,
            build_rtsp_set_path(enabled, args.port, rtsp_username, rtsp_password),
        )
        return
    await _camera_call(
        config,
        lambda camera: camera.set_rtsp(
            enabled,
            port=args.port,
            username=rtsp_username,
            password=rtsp_password,
            experimental=args.experimental,
            confirm=args.confirm,
        ),
    )
    raise ServiceChangeUncertainError(
        "RTSP setter returned without a proven outcome; verify with rtsp status"
    )


async def _handle_account_password(args: argparse.Namespace, config: VStarcamConfig) -> None:
    new_password = _new_camera_password(args)
    if args.dry_run:
        path = build_camera_account_password_set_path(config.username, new_password)
        _print_json(
            {
                "branch": "selected after read-only WebPwd/DualAuthentication preflight",
                "existing_web_password_request": _format_requests(config, (path,))[0],
                "first_enable_steps": [
                    "owner authorization when state is 0",
                    "same-session owner-state readback",
                    "empty external-account enable when state is 1",
                    "external password write",
                    "camera reboot",
                    "fresh-session WebPwd verification",
                ],
            }
        )
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_camera_account_password(
            new_password,
            experimental=args.experimental,
            confirm=args.confirm,
            recovery_ready=args.recovery_ready,
        ),
    )
    _print_json(
        {
            "response_summary": {"field_count": len(result), "fields": sorted(result)},
            "session_closed": True,
            "note": (
                "the external WebPwd account may protect RTSP; verify the fresh "
                "WebPwd getter and service before another write"
            ),
        }
    )


async def _handle_state_write(args: argparse.Namespace, config: VStarcamConfig) -> None:
    enabled = _enabled(args)
    if args.dry_run:
        _print_request(config, args.path_builder(enabled))
        return
    kwargs = {}
    if hasattr(args, "experimental"):
        kwargs["experimental"] = args.experimental
    if hasattr(args, "confirm"):
        kwargs["confirm"] = args.confirm
    result = await _camera_call(
        config,
        lambda camera: getattr(camera, args.camera_method)(enabled, **kwargs),
    )
    if getattr(args, "always_uncertain", False):
        raise ServiceChangeUncertainError(
            f"{args.camera_method} returned without a proven outcome; verify with its getter"
        )
    _print_response(result, args.note)


async def _handle_audio_volume(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_request(config, build_audio_volume_set_path(args.target, args.level))
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_audio_volume(
            args.target, args.level, experimental=args.experimental
        ),
    )
    _print_response(
        result,
        "verify the reported volume with audio status before another write",
    )


async def _handle_motion_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    await _camera_call(
        config,
        lambda camera: camera.set_motion_detection(
            _enabled(args),
            sensitivity=args.sensitivity,
            experimental=args.experimental,
            confirm=args.confirm,
        ),
    )
    raise ServiceChangeUncertainError(
        "motion setter returned without a proven outcome; verify with motion status"
    )


async def _handle_human_status(args: argparse.Namespace, config: VStarcamConfig) -> None:
    async with VStarcamCamera(config) as camera:
        result = await camera.get_human_detection_settings(
            include_tracking=args.include_tracking,
        )
    _print_json(result)


async def _handle_human_detection(args: argparse.Namespace, config: VStarcamConfig) -> None:
    enabled = _enabled(args)
    if args.dry_run:
        path = build_human_detection_set_path(enabled, args.sensitivity, args.distance)
        _print_request(config, path)
        return
    await _camera_call(
        config,
        lambda camera: camera.set_human_detection(
            enabled,
            sensitivity=args.sensitivity,
            distance=args.distance,
            experimental=args.experimental,
            confirm=args.confirm,
        ),
    )
    raise ServiceChangeUncertainError(
        "main human-detection setter returned without a proven outcome"
    )


async def _handle_human_sensitivity(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_request(config, build_human_sensitivity_set_path(args.level))
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_human_sensitivity(args.level),
    )
    _print_response(
        result,
        "verify with human status before another detection write",
    )


async def _handle_ptz_move(args: argparse.Namespace, config: VStarcamConfig) -> None:
    duration = validate_ptz_duration(args.duration)
    if args.dry_run:
        _print_json(
            {
                "duration": duration,
                "requests": _format_requests(
                    config,
                    (
                        build_ptz_start_path(args.direction),
                        build_ptz_stop_path(args.direction),
                    ),
                ),
            }
        )
        return
    result = await _camera_call(
        config,
        lambda camera: camera.move_ptz(
            args.direction,
            duration,
            experimental=args.experimental,
            confirm=args.confirm,
        ),
    )
    _print_response(result, "physically verify that movement stopped")


async def _handle_ptz_stop(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_request(config, build_ptz_stop_path(args.direction))
        return
    result = await _camera_call(
        config,
        lambda camera: camera.stop_ptz(
            args.direction,
            experimental=args.experimental,
        ),
    )
    _print_response(result, "physically verify that movement stopped")


async def _handle_osd_clock_status(_args: argparse.Namespace, config: VStarcamConfig) -> None:
    mode = await _camera_call(
        config,
        lambda camera: camera.get_osd_12h_mode(),
    )
    _print_json({"mode": "12h" if mode else "24h"})


async def _handle_osd_clock_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if args.dry_run:
        _print_request(config, build_osd_12h_set_path(args.twelve_hour))
        return
    result = await _camera_call(
        config,
        lambda camera: camera.set_osd_12h_mode(args.twelve_hour),
    )
    _print_response(
        result,
        "verify with osd clock status before another write",
    )


async def _handle_wifi_scan(args: argparse.Namespace, config: VStarcamConfig) -> None:
    result = await _camera_call(
        config,
        lambda camera: camera.scan_wifi(),
    )
    _print_json(result)


async def _handle_wifi_set(args: argparse.Namespace, config: VStarcamConfig) -> None:
    wifi_password = _wifi_password(args)
    if args.dry_run:
        if args.channel is None or args.auth_type is None:
            raise ConfigError("Wi-Fi dry-run requires --channel and --auth-type")
        path = build_wifi_set_path(args.ssid, wifi_password, args.channel, args.auth_type)
        _print_request(config, path)
        return
    camera = VStarcamCamera(config)
    await camera.set_wifi(
        args.ssid,
        wifi_password,
        channel=args.channel,
        auth_type=args.auth_type,
        experimental=args.experimental,
        confirm=args.confirm,
        recovery_ready=args.recovery_ready,
    )
    raise ServiceChangeUncertainError(
        "Wi-Fi setter returned without a proven outcome; rediscover the camera or use "
        "the recovery plan"
    )


async def _handle_provision_qr(args: argparse.Namespace, config: VStarcamConfig) -> None:
    if not args.experimental:
        raise ExperimentalCommandError(
            "initial Wi-Fi QR provisioning is experimental; pass --experimental"
        )
    if not args.confirm:
        raise ConfirmationRequiredError(
            "initial Wi-Fi QR provisioning can change the camera network; pass --confirm"
        )
    if not args.recovery_ready:
        raise ConfirmationRequiredError(
            "verify physical reset/recovery before provisioning; pass --recovery-ready"
        )

    payload = build_static_wifi_qr_payload(
        args.ssid,
        _wifi_password(args),
        account_id=config.account_id,
        bssid=args.bssid,
    )
    output = write_static_wifi_qr_svg(payload, args.output, overwrite=args.overwrite)
    _print_json(
        {
            "format": "svg",
            "output": str(output),
            "payload": "redacted",
            "note": (
                "open the SVG at full screen, show it to the reset camera from 5-10 inches, "
                "then separately rediscover it on the LAN; discovery does not "
                "prove QR acknowledgement or control readiness"
            ),
        }
    )


async def _dispatch(args: argparse.Namespace) -> int:
    await args.handler(args, _config_from_args(args))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.debug)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        return 130
    except (VStarcamError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
