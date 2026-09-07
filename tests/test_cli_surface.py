"""Smoke-check complete CLI invocations without relying on argparse internals."""

from vstarcamctl.cli import _build_parser

COMMANDS = [
    "account password --new-password fictional-password",
    "alarm-led off",
    "alarm-led on",
    "alarm-led status",
    "audio listen",
    "audio recording off",
    "audio status",
    "audio talk audio.wav --duration 1 --max-speaker-volume 10",
    "audio volume microphone 10",
    "device info",
    "discover",
    "human detection on --sensitivity 1 --distance 1",
    "human frame off",
    "human sensitivity 1",
    "human status",
    "human tracking off",
    "human zoom-tracking off",
    "imaging status",
    "ir set off",
    "ir status",
    "light off",
    "light on",
    "light status",
    "motion set off",
    "motion status",
    "night set smart",
    "night status",
    "onvif set off",
    "onvif status",
    "osd clock 12h",
    "osd clock 24h",
    "osd clock status",
    "osd logo off",
    "osd logo on",
    "osd logo status",
    "osd timestamp status",
    "params",
    "provision qr --ssid example-network --output profile.svg",
    "ptz move up --duration 1",
    "ptz stop up",
    "raw /get_status.cgi",
    "rtsp set off",
    "rtsp status",
    "siren off",
    "siren on",
    "siren status",
    "status",
    "stream probe",
    "stream record clip.mkv --duration 1",
    "stream snapshot frame.jpg",
    "time set",
    "time status",
    "wifi scan",
    "wifi set --ssid example-network",
    "wifi status",
]


def test_supported_cli_invocations_parse_and_select_a_handler():
    parser = _build_parser()

    for command in COMMANDS:
        args = parser.parse_args(command.split())
        assert callable(getattr(args, "handler", None)), command
