"""Command line interface for safe local camera control."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time as system_time
from dataclasses import asdict
from pathlib import Path

from .audio_talk import iter_adpcm_talk_frames
from .camera import VStarcamCamera
from .config import DEFAULTS, load_config
from .discovery_encrypted import discover_with_seed
from .errors import ConfigError, TimeConfigurationError, VStarcamError
from .media_stream import capture_rtsp_snapshot, play_rtsp, probe_rtsp, record_rtsp
from .secrets import SecretMaskingFilter, mask_secrets, redact_data
from .time_settings import parse_utc_offset
from .transport_aiopppp import discover_cameras


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vstarcamctl", description="Local control for VStarcam-compatible PPPP/CGI cameras"
    )
    parser.add_argument("--config", help="ignored local YAML file")
    parser.add_argument("--host")
    parser.add_argument("--vuid")
    parser.add_argument("--username")
    parser.add_argument("--password", help="prefer VSTARCAM_PASSWORD to avoid shell history")
    parser.add_argument("--psk")
    parser.add_argument("--udp-port", type=int)
    parser.add_argument("--discovery-port", type=int)
    parser.add_argument("--auth-mode", choices=("basic", "observed", "auto"))
    parser.add_argument("--account-id")
    parser.add_argument("--login-hash")
    parser.add_argument("--login-token")
    parser.add_argument("--transport", choices=("aiopppp",))
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--retries", type=int)
    parser.add_argument("--debug", action="store_true")

    sub = parser.add_subparsers(dest="action", required=True)
    discover = sub.add_parser("discover", help="discover LAN PPPP cameras")
    discover.add_argument("--seconds", type=float, default=3.0)

    sub.add_parser("status", help="read get_status.cgi")
    sub.add_parser("params", help="read get_params.cgi")

    for name in ("siren", "light"):
        control = sub.add_parser(name)
        control.add_argument("state", choices=("on", "off"))
        control.add_argument("--experimental", action="store_true")

    night = sub.add_parser("night", help="inspect or experimentally set night vision")
    night_sub = night.add_subparsers(dest="night_action", required=True)
    night_sub.add_parser("status", help="read the normalized night-vision mode")
    night_set = night_sub.add_parser(
        "set", help="send the documented two-step night-mode transition once"
    )
    night_set.add_argument("mode", choices=("black-white", "starlight", "full-color", "smart"))
    night_set.add_argument("--dry-run", action="store_true")
    night_set.add_argument("--experimental", action="store_true")

    infrared = sub.add_parser(
        "ir", help="inspect or experimentally control the infrared illuminator"
    )
    infrared_sub = infrared.add_subparsers(dest="ir_action", required=True)
    infrared_sub.add_parser("status", help="read the infrared illuminator state")
    infrared_set = infrared_sub.add_parser(
        "set", help="send the infrared illuminator candidate once"
    )
    infrared_set.add_argument("state", choices=("on", "off"))
    infrared_set.add_argument("--dry-run", action="store_true")
    infrared_set.add_argument("--experimental", action="store_true")

    time_control = sub.add_parser("time", help="inspect or configure clock, timezone, and NTP")
    time_sub = time_control.add_subparsers(dest="time_action", required=True)
    time_sub.add_parser("status", help="read normalized time fields from get_params.cgi")
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
    time_set.add_argument("--dry-run", action="store_true")
    time_set.add_argument("--experimental", action="store_true")

    raw = sub.add_parser("raw", help="send a relative CGI path through PPPP")
    raw.add_argument("path")
    raw.add_argument("--dry-run", action="store_true")
    raw.add_argument("--experimental", action="store_true")
    raw.add_argument("--confirm", action="store_true")

    rtsp = sub.add_parser("rtsp", help="inspect or experimentally configure RTSP")
    rtsp_sub = rtsp.add_subparsers(dest="rtsp_action", required=True)
    rtsp_sub.add_parser("status", help="read live RTSP service settings")
    rtsp_set = rtsp_sub.add_parser("set", help="send the RTSP setting candidate once")
    rtsp_set.add_argument("state", choices=("on", "off"))
    rtsp_set.add_argument("--port", type=int, help="preserve the current port when omitted")
    rtsp_set.add_argument("--rtsp-username", help="preserve the current username when omitted")
    rtsp_set.add_argument(
        "--rtsp-password",
        help="prefer VSTARCAM_RTSP_PASSWORD; preserve the current password when omitted",
    )
    rtsp_set.add_argument("--dry-run", action="store_true")
    rtsp_set.add_argument("--experimental", action="store_true")
    rtsp_set.add_argument("--confirm", action="store_true")

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
    account_password.add_argument("--dry-run", action="store_true")
    account_password.add_argument("--experimental", action="store_true")
    account_password.add_argument("--confirm", action="store_true")
    account_password.add_argument("--recovery-ready", action="store_true")

    onvif = sub.add_parser("onvif", help="inspect or experimentally configure ONVIF")
    onvif_sub = onvif.add_subparsers(dest="onvif_action", required=True)
    onvif_sub.add_parser("status", help="read live ONVIF service settings")
    onvif_set = onvif_sub.add_parser("set", help="send the ONVIF setting candidate once")
    onvif_set.add_argument("state", choices=("on", "off"))
    onvif_set.add_argument("--dry-run", action="store_true")
    onvif_set.add_argument("--experimental", action="store_true")
    onvif_set.add_argument("--confirm", action="store_true")

    audio = sub.add_parser("audio", help="live audio, two-way talk, volume, and recording")
    audio_sub = audio.add_subparsers(dest="audio_action", required=True)
    audio_sub.add_parser("status", help="read audio capability and record-audio state")
    recording = audio_sub.add_parser(
        "recording",
        help="experimentally include or exclude audio in camera recordings",
    )
    recording.add_argument("state", choices=("on", "off"))
    recording.add_argument("--dry-run", action="store_true")
    recording.add_argument("--experimental", action="store_true")
    recording.add_argument("--confirm", action="store_true")

    volume = audio_sub.add_parser(
        "volume", help="experimentally set camera microphone or speaker volume"
    )
    volume.add_argument("target", choices=("microphone", "speaker"))
    volume.add_argument("level", type=int, help="camera level from 0 to 31")
    volume.add_argument("--dry-run", action="store_true")
    volume.add_argument("--experimental", action="store_true")

    listen = audio_sub.add_parser("listen", help="play live camera audio with ffplay")
    _add_stream_input_options(listen)
    listen.add_argument("--duration", type=float)
    listen.add_argument("--local-volume", type=int, default=100, help="ffplay volume 0-100")

    talk = audio_sub.add_parser(
        "talk", help="experimentally transmit half-duplex ADPCM audio to the camera"
    )
    talk.add_argument("source", help="FFmpeg input, for example default or an audio file")
    talk.add_argument("--input-format", help="FFmpeg input format, for example pulse or alsa")
    talk.add_argument("--duration", type=float)
    talk.add_argument("--experimental", action="store_true")
    talk.add_argument("--confirm", action="store_true")

    # Reserved placeholder: always refused until a full-duplex-capable target
    # exists. It accepts the same positional/optional inputs as "talk" so the
    # refusal message is reached instead of an argparse error.
    duplex = audio_sub.add_parser(
        "duplex", help="reserved; refused until full-duplex target support exists"
    )
    duplex.add_argument("source", help="FFmpeg input, for example default or an audio file")
    duplex.add_argument("--input-format", help="FFmpeg input format, for example pulse or alsa")
    duplex.add_argument("--duration", type=float)
    duplex.add_argument("--experimental", action="store_true")
    duplex.add_argument("--confirm", action="store_true")

    motion = sub.add_parser("motion", help="inspect or experimentally configure motion detection")
    motion_sub = motion.add_subparsers(dest="motion_action", required=True)
    motion_sub.add_parser("status", help="read motion state and sensitivity from get_params.cgi")
    motion_set = motion_sub.add_parser("set", help="send the motion setting candidate once")
    motion_set.add_argument("state", choices=("on", "off"))
    motion_set.add_argument(
        "--sensitivity",
        type=int,
        help="0 is highest and 9 is lowest; preserve the current value when omitted",
    )
    motion_set.add_argument("--experimental", action="store_true")
    motion_set.add_argument("--confirm", action="store_true")

    human = sub.add_parser("human", help="inspect or experimentally configure human detection")
    human_sub = human.add_subparsers(dest="human_action", required=True)
    human_status = human_sub.add_parser(
        "status", help="read humanoid frame, sensitivity, and zoom fields"
    )
    human_status.add_argument(
        "--include-tracking",
        action="store_true",
        help="also run the live-confirmed 2127 tracking getter",
    )
    human_detection = human_sub.add_parser(
        "detection", help="configure the main humanoid detector once"
    )
    human_detection.add_argument("state", choices=("on", "off"))
    human_detection.add_argument("--sensitivity", type=int, required=True, help="level 1-3")
    human_detection.add_argument("--distance", type=int, required=True, help="level 1-3")

    human_sensitivity = human_sub.add_parser(
        "sensitivity", help="configure the 2126 humanoid sensitivity once"
    )
    human_sensitivity.add_argument("level", type=int, help="device-specific level 0-3")

    for action_name, help_text in (
        ("frame", "show or hide humanoid target frames"),
        ("tracking", "enable or disable humanoid PTZ tracking"),
        ("zoom-tracking", "enable or disable humanoid zoom tracking"),
    ):
        control = human_sub.add_parser(action_name, help=help_text)
        control.add_argument("state", choices=("on", "off"))

    human_write_controls = (
        human_detection,
        human_sensitivity,
        *(human_sub.choices[name] for name in ("frame", "tracking", "zoom-tracking")),
    )
    for control in human_write_controls:
        control.add_argument("--dry-run", action="store_true")
    for control in (
        human_detection,
        human_sub.choices["tracking"],
        human_sub.choices["zoom-tracking"],
    ):
        control.add_argument("--experimental", action="store_true")
        control.add_argument("--confirm", action="store_true")

    stream = sub.add_parser("stream", help="receive RTSP video and audio")
    stream_sub = stream.add_subparsers(dest="stream_action", required=True)

    stream_probe = stream_sub.add_parser(
        "probe",
        help="inspect available video/audio codecs with ffprobe",
    )
    _add_stream_input_options(stream_probe)
    stream_probe.add_argument("--probe-timeout", type=float)

    stream_record = stream_sub.add_parser(
        "record",
        help="save a bounded RTSP video/audio segment with ffmpeg",
    )
    _add_stream_input_options(stream_record)
    stream_record.add_argument("output", type=Path)
    stream_record.add_argument("--duration", type=float, default=30.0)
    stream_record.add_argument(
        "--media",
        choices=("both", "video", "audio"),
        default="both",
    )
    stream_record.add_argument("--overwrite", action="store_true")

    stream_snapshot = stream_sub.add_parser(
        "snapshot",
        help="save one decoded video frame with ffmpeg",
    )
    _add_stream_input_options(stream_snapshot)
    stream_snapshot.add_argument("output", type=Path)
    stream_snapshot.add_argument("--snapshot-timeout", type=float)
    stream_snapshot.add_argument("--overwrite", action="store_true")

    wifi = sub.add_parser("wifi", help="inspect or experimentally change Wi-Fi")
    wifi_sub = wifi.add_subparsers(dest="wifi_action", required=True)
    wifi_sub.add_parser("status", help="read current Wi-Fi fields from get_params.cgi")
    wifi_scan = wifi_sub.add_parser("scan", help="request and parse a local access-point scan")
    wifi_scan.add_argument("--experimental", action="store_true")
    wifi_set = wifi_sub.add_parser("set", help="send the unconfirmed set_wifi.cgi candidate once")
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
    wifi_set.add_argument("--dry-run", action="store_true")
    wifi_set.add_argument("--experimental", action="store_true")
    wifi_set.add_argument("--confirm", action="store_true")
    wifi_set.add_argument(
        "--recovery-ready",
        action="store_true",
        help="acknowledge that physical reset/recovery has been verified",
    )

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


def _config_from_args(args: argparse.Namespace):
    values = vars(args)
    cli = {key: values.get(key) for key in DEFAULTS}
    return load_config(cli=cli, yaml_path=args.config)


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


async def _discover(config, seconds: float) -> list[dict]:
    regular_task = discover_cameras(
        remote_addr=config.host or "255.255.255.255",
        remote_port=config.discovery_port,
        timeout=seconds,
    )
    seeded_task = asyncio.to_thread(
        discover_with_seed,
        config.host or "255.255.255.255",
        ports=(config.discovery_port, config.udp_port),
        seed=config.psk,
        timeout=seconds,
    )
    regular, seeded = await asyncio.gather(regular_task, seeded_task)
    items = [asdict(camera) for camera in regular]
    items.extend(
        {
            "host": camera.host,
            "port": camera.port,
            "vuid": camera.vuid,
            "protocol": "binary",
            "encryption": "PSK",
        }
        for camera in seeded
    )
    unique = {(item["host"], item["port"], item["vuid"]): item for item in items}
    return list(unique.values())


async def _run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if args.action == "discover":
        _print_json(await _discover(config, args.seconds))
        return 0

    if args.action == "stream":
        camera = VStarcamCamera(config)
        try:
            stream = await camera.get_rtsp_stream(
                quality=args.quality,
                transport=args.rtsp_transport,
                port=args.port,
                username=_rtsp_username(args),
                password=_rtsp_password(args),
            )
        finally:
            await camera.close()

        if args.stream_action == "probe":
            result = await probe_rtsp(
                stream,
                timeout=args.probe_timeout or config.timeout,
            )
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
                timeout=args.snapshot_timeout or config.timeout,
                overwrite=args.overwrite,
            )
        _print_json(result)
        return 0

    if args.action == "audio" and args.audio_action == "listen":
        camera = VStarcamCamera(config)
        try:
            stream = await camera.get_rtsp_stream(
                quality=args.quality,
                transport=args.rtsp_transport,
                port=args.port,
                username=_rtsp_username(args),
                password=_rtsp_password(args),
            )
        finally:
            await camera.close()
        _print_json(
            await play_rtsp(
                stream,
                media="audio",
                duration=args.duration,
                volume=args.local_volume,
            )
        )
        return 0

    if args.action == "audio" and args.audio_action in {"talk", "duplex"}:
        if args.audio_action == "duplex":
            raise ConfigError(
                "full-duplex talk is not implemented for this target; "
                "use audio listen and half-duplex audio talk separately"
            )
        camera = VStarcamCamera(config)
        try:
            result = await camera.send_talk_audio(
                iter_adpcm_talk_frames(
                    args.source,
                    input_format=args.input_format,
                    duration=args.duration,
                ),
                experimental=args.experimental,
                confirm=args.confirm,
            )
        finally:
            await camera.close()
        _print_json(result)
        return 0

    camera = VStarcamCamera(config)
    if args.action == "raw" and args.dry_run:
        print(mask_secrets(camera.format_raw_cgi(args.path)))
        return 0

    if args.action == "night" and args.night_action == "set":
        if args.dry_run:
            _print_json(
                {
                    "requests": [
                        mask_secrets(request)
                        for request in camera.format_night_vision_set_cgi(args.mode)
                    ]
                }
            )
            return 0
        try:
            result = await camera.set_night_vision(
                args.mode,
                experimental=args.experimental,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "responses": result,
                "note": "verify with night status before another night-mode write",
            }
        )
        return 0

    if args.action == "ir" and args.ir_action == "set":
        if args.dry_run:
            print(mask_secrets(camera.format_infrared_light_set_cgi(args.state == "on")))
            return 0
        try:
            result = await camera.set_infrared_light(
                args.state == "on",
                experimental=args.experimental,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with ir status before another infrared-light write",
            }
        )
        return 0

    if args.action == "time" and args.time_action == "set":
        ntp_enabled = None if args.ntp is None else args.ntp == "on"
        if args.dry_run:
            if args.timezone is None or ntp_enabled is None or args.ntp_server is None:
                raise ConfigError("time dry-run requires --timezone, --ntp, and --ntp-server")
            unix_time = args.unix_time
            if unix_time is None:
                unix_time = int(system_time.time())
            print(
                mask_secrets(
                    camera.format_time_settings_set_cgi(
                        timezone_offset_seconds=args.timezone,
                        ntp_enabled=ntp_enabled,
                        ntp_server=args.ntp_server,
                        unix_time=unix_time,
                    )
                )
            )
            return 0
        try:
            result = await camera.set_time_settings(
                timezone_offset_seconds=args.timezone,
                ntp_enabled=ntp_enabled,
                ntp_server=args.ntp_server,
                unix_time=args.unix_time,
                experimental=args.experimental,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with time status before another time-setting write",
            }
        )
        return 0

    if args.action == "rtsp" and args.rtsp_action == "set":
        rtsp_password = _rtsp_password(args)
        if args.dry_run:
            if args.port is None or args.rtsp_username is None or rtsp_password is None:
                raise ConfigError(
                    "RTSP dry-run requires --port, --rtsp-username, and "
                    "--rtsp-password or VSTARCAM_RTSP_PASSWORD"
                )
            print(
                mask_secrets(
                    camera.format_rtsp_set_cgi(
                        args.state == "on",
                        port=args.port,
                        username=args.rtsp_username,
                        password=rtsp_password,
                    )
                )
            )
            return 0
        try:
            result = await camera.set_rtsp(
                args.state == "on",
                port=args.port,
                username=args.rtsp_username,
                password=rtsp_password,
                experimental=args.experimental,
                confirm=args.confirm,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with rtsp status before relying on the new settings",
            }
        )
        return 0

    if args.action == "account" and args.account_action == "password":
        new_password = _new_camera_password(args)
        if args.dry_run:
            print(mask_secrets(camera.format_camera_account_password_set_cgi(new_password)))
            return 0
        result = await camera.set_camera_account_password(
            new_password,
            experimental=args.experimental,
            confirm=args.confirm,
            recovery_ready=args.recovery_ready,
        )
        _print_json(
            {
                "response_summary": (
                    {
                        "field_count": len(result),
                        "fields": sorted(result),
                    }
                    if isinstance(result, dict)
                    else {"type": type(result).__name__}
                ),
                "session_closed": True,
                "note": (
                    "the camera-wide account password also protects RTSP; "
                    "update clients and verify before another write"
                ),
            }
        )
        return 0

    if args.action == "onvif" and args.onvif_action == "set":
        if args.dry_run:
            print(mask_secrets(camera.format_onvif_set_cgi(args.state == "on")))
            return 0
        try:
            result = await camera.set_onvif(
                args.state == "on",
                experimental=args.experimental,
                confirm=args.confirm,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with onvif status before relying on the new setting",
            }
        )
        return 0

    if args.action == "audio" and args.audio_action == "recording":
        if args.dry_run:
            print(mask_secrets(camera.format_record_audio_set_cgi(args.state == "on")))
            return 0
        try:
            result = await camera.set_record_audio(
                args.state == "on",
                experimental=args.experimental,
                confirm=args.confirm,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with audio status before relying on the new setting",
            }
        )
        return 0

    if args.action == "audio" and args.audio_action == "volume":
        if args.dry_run:
            print(mask_secrets(camera.format_audio_volume_set_cgi(args.target, args.level)))
            return 0
        try:
            result = await camera.set_audio_volume(
                args.target,
                args.level,
                experimental=args.experimental,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify the reported volume with audio status before another write",
            }
        )
        return 0

    if args.action == "motion" and args.motion_action == "set":
        try:
            result = await camera.set_motion_detection(
                args.state == "on",
                sensitivity=args.sensitivity,
                experimental=args.experimental,
                confirm=args.confirm,
            )
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with motion status before relying on the new setting",
            }
        )
        return 0

    if args.action == "human" and args.human_action != "status":
        enabled = getattr(args, "state", None) == "on"
        experimental = getattr(args, "experimental", False)
        confirm = getattr(args, "confirm", False)
        formatters = {
            "detection": lambda: camera.format_human_detection_set_cgi(
                enabled,
                sensitivity=args.sensitivity,
                distance=args.distance,
            ),
            "sensitivity": lambda: camera.format_human_sensitivity_set_cgi(args.level),
            "frame": lambda: camera.format_human_frame_set_cgi(enabled),
            "tracking": lambda: camera.format_human_tracking_set_cgi(enabled),
            "zoom-tracking": lambda: camera.format_human_zoom_tracking_set_cgi(enabled),
        }
        if args.dry_run:
            print(mask_secrets(formatters[args.human_action]()))
            return 0
        operations = {
            "detection": lambda: camera.set_human_detection(
                enabled,
                sensitivity=args.sensitivity,
                distance=args.distance,
                experimental=experimental,
                confirm=confirm,
            ),
            "sensitivity": lambda: camera.set_human_sensitivity(
                args.level,
                experimental=experimental,
                confirm=confirm,
            ),
            "frame": lambda: camera.set_human_frame(
                enabled,
                experimental=experimental,
                confirm=confirm,
            ),
            "tracking": lambda: camera.set_human_tracking(
                enabled,
                experimental=experimental,
                confirm=confirm,
            ),
            "zoom-tracking": lambda: camera.set_human_zoom_tracking(
                enabled,
                experimental=experimental,
                confirm=confirm,
            ),
        }
        try:
            result = await operations[args.human_action]()
        finally:
            await camera.close()
        _print_json(
            {
                "response": result,
                "note": "verify with human status before another detection write",
            }
        )
        return 0

    if args.action == "wifi" and args.wifi_action == "set":
        wifi_password = _wifi_password(args)
        if args.dry_run:
            if args.channel is None or args.auth_type is None:
                raise ConfigError("Wi-Fi dry-run requires --channel and --auth-type")
            print(
                mask_secrets(
                    camera.format_wifi_set_cgi(
                        args.ssid,
                        wifi_password,
                        channel=args.channel,
                        auth_type=args.auth_type,
                    )
                )
            )
            return 0
        result = await camera.set_wifi(
            args.ssid,
            wifi_password,
            channel=args.channel,
            auth_type=args.auth_type,
            experimental=args.experimental,
            confirm=args.confirm,
            recovery_ready=args.recovery_ready,
        )
        _print_json(
            {
                "response": result,
                "session_closed": True,
                "note": "verify the camera on the intended network before any further write",
            }
        )
        return 0

    async with camera:
        if args.action == "status":
            _print_json(await camera.get_status())
        elif args.action == "params":
            params = await camera.get_params()
            _print_json({"field_count": len(params)})
        elif args.action == "siren":
            _print_json(await camera.set_siren(args.state == "on", experimental=args.experimental))
        elif args.action == "light":
            _print_json(await camera.set_light(args.state == "on", experimental=args.experimental))
        elif args.action == "night":
            _print_json(await camera.get_night_vision_settings())
        elif args.action == "ir":
            _print_json(await camera.get_infrared_light_settings())
        elif args.action == "time":
            _print_json(await camera.get_time_settings())
        elif args.action == "rtsp":
            _print_json(await camera.get_rtsp_settings())
        elif args.action == "onvif":
            _print_json(await camera.get_onvif_settings())
        elif args.action == "audio":
            _print_json(await camera.get_audio_settings())
        elif args.action == "motion":
            _print_json(await camera.get_motion_detection_settings())
        elif args.action == "human":
            _print_json(
                await camera.get_human_detection_settings(
                    include_tracking=args.include_tracking,
                    experimental=False,
                )
            )
        elif args.action == "wifi":
            if args.wifi_action == "status":
                _print_json(await camera.get_wifi_status())
            else:
                _print_json(await camera.scan_wifi(experimental=args.experimental))
        elif args.action == "raw":
            response = await camera.send_raw_cgi(
                args.path, experimental=args.experimental, confirm=args.confirm
            )
            print(mask_secrets(response))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.debug)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (VStarcamError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
