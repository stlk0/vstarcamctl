import json
import os
import stat

import pytest
import yaml

import vstarcamctl.cli as cli_module
from vstarcamctl import __version__
from vstarcamctl.cli import main
from vstarcamctl.config import load_config
from vstarcamctl.errors import DiscoveryError
from vstarcamctl.media_stream import RTSPStream
from vstarcamctl.transport import DiscoveredCamera

BASE_ARGS = [
    "--host",
    "192.0.2.10",
    "--device-id",
    "VSTG-000001-AAAAA",
    "--password",
    "camera-admin-password",
]

DISCOVERED_CAMERA = {
    "host": "192.0.2.20",
    "port": 24680,
    "device_id": "VSTG-000001-AAAAA",
    "protocol": "binary",
    "encryption": "PSK",
}
DISCOVERED_CAMERA_RESULT = DiscoveredCamera(**DISCOVERED_CAMERA)


def test_version_flag_uses_installed_package_metadata(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"vstarcamctl {__version__}"


def test_legacy_vuid_option_is_not_part_of_the_cli(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--vuid=legacy", "discover"])

    assert caught.value.code == 2
    assert "unrecognized arguments: --vuid=legacy" in capsys.readouterr().err


class StubCamera:
    def __init__(self, _config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback):
        return None

    async def close(self):
        pass


@pytest.mark.parametrize(
    ("global_args", "expected_host"),
    [
        ([], None),
        (["--host", "192.0.2.44"], "192.0.2.44"),
    ],
)
def test_discover_ignores_stale_default_profile_and_uses_only_explicit_host(
    global_args,
    expected_host,
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.local.yaml").write_text(
        "host: 192.0.2.99\ndevice_id: VSTG-999999-ZZZZZ\nudp_port: 44001\n"
        "discovery_port: 44002\npsk: stale-seed\n",
        encoding="utf-8",
    )
    calls = {}

    async def discover(**kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(cli_module, "discover_cameras", discover)

    assert main([*global_args, "discover", "--seconds", "0.01"]) == 0
    assert calls == {
        "timeout": 0.01,
        "host": expected_host,
        "source_address": None,
        "psk": None,
        "discovery_port": 32108,
        "udp_port": 12833,
    }


def test_discover_source_address_uses_only_source_bound_seeded_discovery(monkeypatch):
    calls = {}

    async def discover(**kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(cli_module, "discover_cameras", discover)

    assert main(["--source-address", "192.0.2.44", "discover", "--seconds", "0.01"]) == 0
    assert calls["source_address"] == "192.0.2.44"


def test_discover_device_id_filters_the_regular_list(monkeypatch, capsys):
    calls = {}

    async def discover(**kwargs):
        calls.update(kwargs)
        return DISCOVERED_CAMERA_RESULT

    monkeypatch.setattr(cli_module, "discover_camera", discover)

    assert (
        main(
            [
                "--device-id",
                DISCOVERED_CAMERA["device_id"],
                "discover",
                "--seconds",
                "0.01",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"host": "192.0.2.20"' in output
    assert DISCOVERED_CAMERA["device_id"] not in output
    assert calls == {
        "device_id": DISCOVERED_CAMERA["device_id"],
        "timeout": 0.01,
        "host": None,
        "source_address": None,
        "psk": None,
        "discovery_port": 32108,
        "udp_port": 12833,
    }


@pytest.mark.parametrize(
    ("global_args", "expected_psk"),
    [
        ([], None),
        (["--psk", "private-model-seed"], "private-model-seed"),
    ],
)
def test_discover_single_camera_creates_private_minimal_profile(
    global_args,
    expected_psk,
    tmp_path,
    monkeypatch,
    capsys,
):
    async def discover(*, device_id, host, **_kwargs):
        assert device_id is None
        assert host is None
        return DISCOVERED_CAMERA_RESULT

    monkeypatch.setattr(cli_module, "discover_camera", discover)
    profile = tmp_path / "camera.local.yaml"

    assert main([*global_args, "discover", "--save-config", str(profile)]) == 0
    expected_profile = {
        "host": DISCOVERED_CAMERA["host"],
        "device_id": DISCOVERED_CAMERA["device_id"],
        "udp_port": DISCOVERED_CAMERA["port"],
        "discovery_port": 32108,
    }
    if expected_psk is not None:
        expected_profile["psk"] = expected_psk
    assert yaml.safe_load(profile.read_text(encoding="utf-8")) == expected_profile
    assert load_config(yaml_path=profile, env={}).psk == expected_psk
    if os.name == "posix":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    output = capsys.readouterr().out
    assert DISCOVERED_CAMERA["device_id"] not in output
    assert "private-model-seed" not in output
    assert '"device_id": "***"' in output


def test_discover_profile_uses_the_psk_that_decrypted_the_camera(
    tmp_path,
    monkeypatch,
    capsys,
):
    discovered = DiscoveredCamera(
        **DISCOVERED_CAMERA,
        psk="vstarcam2019",
    )

    async def discover(**_kwargs):
        return discovered

    monkeypatch.setattr(cli_module, "discover_camera", discover)
    profile = tmp_path / "camera.local.yaml"

    assert main(["discover", "--save-config", str(profile)]) == 0
    assert yaml.safe_load(profile.read_text(encoding="utf-8"))["psk"] == "vstarcam2019"
    output = capsys.readouterr().out
    assert "vstarcam2019" not in output
    assert '"psk"' not in output


def test_discover_list_redacts_the_matched_psk(monkeypatch, capsys):
    discovered = DiscoveredCamera(
        **DISCOVERED_CAMERA,
        psk="vstarcam2019",
    )

    async def discover(**_kwargs):
        return [discovered]

    monkeypatch.setattr(cli_module, "discover_cameras", discover)

    assert main(["discover", "--seconds", "0.01"]) == 0
    output = capsys.readouterr().out
    assert "vstarcam2019" not in output
    assert '"psk"' not in output


@pytest.mark.parametrize(
    ("global_args", "message"),
    [
        ([], "no cameras were discovered"),
        (["--device-id", "VSTG-999999-ZZZZZ"], "selected device ID was not discovered"),
        (["--device-id", DISCOVERED_CAMERA["device_id"]], "multiple endpoints"),
        ([], "multiple cameras were discovered"),
    ],
)
def test_discover_refuses_unselectable_results_without_writing(
    global_args,
    message,
    tmp_path,
    monkeypatch,
    capsys,
):
    async def discover(**_kwargs):
        raise DiscoveryError(message)

    monkeypatch.setattr(cli_module, "discover_camera", discover)
    profile = tmp_path / "camera.local.yaml"

    assert main([*global_args, "discover", "--save-config", str(profile)]) == 2
    assert not profile.exists()
    assert message in capsys.readouterr().err


def test_discover_explicit_device_id_selects_one_of_multiple_cameras(
    tmp_path,
    monkeypatch,
    capsys,
):
    selected = {
        **DISCOVERED_CAMERA,
        "host": "192.0.2.22",
        "port": 24681,
        "device_id": "VSTG-000002-BBBBB",
    }

    selected_result = DiscoveredCamera(**selected)
    calls = {}

    async def discover(**kwargs):
        calls.update(kwargs)
        return selected_result

    monkeypatch.setattr(cli_module, "discover_camera", discover)
    profile = tmp_path / "camera.local.yaml"

    assert (
        main(
            [
                "--device-id",
                selected["device_id"],
                "discover",
                "--save-config",
                str(profile),
            ]
        )
        == 0
    )
    saved = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert (saved["host"], saved["device_id"], saved["udp_port"]) == (
        selected["host"],
        selected["device_id"],
        selected["port"],
    )
    assert calls["device_id"] == selected["device_id"]
    assert selected["device_id"] not in capsys.readouterr().out


def test_raw_dry_run_never_prints_credentials_or_device_id(capsys):
    result = main(
        [
            *BASE_ARGS,
            "--username",
            "private-user",
            "raw",
            "--dry-run",
            "/get_status.cgi?vuid=VE123456",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "camera-admin-password" not in output
    assert "private-user" not in output
    assert "VE123456" not in output
    assert "pwd=***" in output


def test_stream_snapshot_pppp_routes_to_camera_without_rtsp_lookup(
    monkeypatch,
    tmp_path,
    capsys,
):
    called = {}
    output = tmp_path / "frame.jpg"

    class SnapshotCamera(StubCamera):
        async def capture_pppp_snapshot(
            self,
            path,
            *,
            timeout,
            overwrite,
            experimental,
        ):
            called.update(
                path=path,
                timeout=timeout,
                overwrite=overwrite,
                experimental=experimental,
            )
            return {"path": str(path), "bytes_written": 5, "codec": "h264", "source": "pppp"}

    async def unexpected_rtsp(*_args, **_kwargs):
        raise AssertionError("PPPP snapshot must not resolve RTSP")

    monkeypatch.setattr(cli_module, "VStarcamCamera", SnapshotCamera)
    monkeypatch.setattr(cli_module, "_get_rtsp_stream", unexpected_rtsp)

    assert (
        main(
            [
                *BASE_ARGS,
                "--timeout",
                "3",
                "stream",
                "snapshot",
                "--source",
                "pppp",
                str(output),
                "--experimental",
            ]
        )
        == 0
    )
    assert called == {
        "path": output,
        "timeout": 3.0,
        "overwrite": False,
        "experimental": True,
    }
    assert '"source": "pppp"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "options",
    [
        ["--port", "10554"],
        ["--rtsp-username", "user", "--rtsp-password", "password"],
        ["--quality", "sub"],
        ["--rtsp-transport", "udp"],
    ],
)
def test_stream_snapshot_pppp_rejects_rtsp_only_options(options, tmp_path, capsys):
    assert (
        main(
            [
                *BASE_ARGS,
                "stream",
                "snapshot",
                "--source",
                "pppp",
                *options,
                str(tmp_path / "frame.jpg"),
                "--experimental",
            ]
        )
        == 2
    )
    assert "RTSP input options cannot be used" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command,expected,hidden",
    [
        (
            [
                "--account-id",
                "test-account",
                "wifi",
                "set",
                "--ssid",
                "private network",
                "--wifi-password",
                "private-passphrase",
                "--channel",
                "6",
                "--auth-type",
                "4",
                "--dry-run",
            ],
            ("ssid=***", "wpa_psk=***", "userid=***"),
            ("private network", "private-passphrase", "test-account"),
        ),
        (
            [
                "rtsp",
                "set",
                "on",
                "--port",
                "10554",
                "--rtsp-username",
                "stream-user",
                "--rtsp-password",
                "stream-secret",
                "--dry-run",
            ],
            ("rtspuser=***", "rtsppwd=***"),
            ("stream-user", "stream-secret"),
        ),
        (["onvif", "set", "on", "--dry-run"], ("onvifenable=1",), ()),
        (["audio", "recording", "off", "--dry-run"], ("record_audio=0",), ()),
        (
            ["audio", "volume", "speaker", "21", "--dry-run"],
            ("param=25&value=21",),
            (),
        ),
        (
            ["night", "set", "smart", "--dry-run"],
            ("param=14&value=1", "param=33&value=2"),
            (),
        ),
        (["ir", "set", "on", "--dry-run"], ("InfraredLaser=1",), ()),
        (
            [
                "time",
                "set",
                "--timezone",
                "UTC+05:30",
                "--ntp",
                "on",
                "--ntp-server",
                "time.windows.com",
                "--unix-time",
                "1700000000",
                "--dry-run",
            ],
            ("tz=-19800", "ntp_svr=***", "now=1700000000"),
            ("time.windows.com",),
        ),
        (
            [
                "human",
                "detection",
                "on",
                "--sensitivity",
                "2",
                "--distance",
                "3",
                "--dry-run",
            ],
            ("cmd=2106&command=4&humanDetection=2&DistanceAdjust=3",),
            (),
        ),
        (["human", "sensitivity", "1", "--dry-run"], ("sensitive=1",), ()),
        (["human", "frame", "off", "--dry-run"], ("bHumanoidFrame=0",), ()),
        (["human", "tracking", "on", "--dry-run"], ("cmd=2127", "enable=1"), ()),
        (
            ["human", "zoom-tracking", "on", "--dry-run"],
            ("cmd=2126", "humanoid_zoom=1"),
            (),
        ),
        (
            ["ptz", "move", "up", "--duration", "0.25", "--dry-run"],
            ("command=0&onestep=0", "command=1&onestep=0", '"duration": 0.25'),
            (),
        ),
        (["ptz", "stop", "right", "--dry-run"], ("command=7&onestep=0",), ()),
        (
            ["osd", "clock", "12h", "--dry-run"],
            ("cmd=4109&command=1&osd_12h_mode=1",),
            (),
        ),
        (
            ["osd", "clock", "24h", "--dry-run"],
            ("cmd=4109&command=1&osd_12h_mode=0",),
            (),
        ),
        (
            ["alarm-led", "on", "--dry-run"],
            ("cmd=2109&command=0&alarmLed=1",),
            (),
        ),
        (
            ["alarm-led", "off", "--dry-run"],
            ("cmd=2109&command=0&alarmLed=0",),
            (),
        ),
        (["osd", "logo", "on", "--dry-run"], ("param=11&value=1",), ()),
        (["osd", "logo", "off", "--dry-run"], ("param=11&value=0",), ()),
    ],
)
def test_dry_run_routes_build_redacted_requests(command, expected, hidden, capsys):
    assert main([*BASE_ARGS, *command]) == 0
    output = capsys.readouterr().out
    assert all(fragment in output for fragment in expected)
    assert all(secret not in output for secret in (*hidden, "camera-admin-password", "VE123456"))


def test_rtsp_set_reads_both_dedicated_credentials_from_environment(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("VSTARCAM_RTSP_USERNAME", "environment-user")
    monkeypatch.setenv("VSTARCAM_RTSP_PASSWORD", "environment-password")

    assert main([*BASE_ARGS, "rtsp", "set", "on", "--port", "10554", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "rtspuser=***" in output
    assert "rtsppwd=***" in output
    assert "environment-user" not in output
    assert "environment-password" not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--transport", "aiopppp", "status"],
        ["--auth-mode", "auto", "status"],
        ["audio", "duplex", "default"],
        ["siren", "on", "--experimental"],
        ["light", "off", "--experimental"],
        ["time", "set", "--experimental"],
    ],
)
def test_removed_cli_surface_is_rejected(arguments):
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == 2


def test_expected_error_returns_exit_two_without_leaking_credentials(capsys):
    result = main([*BASE_ARGS, "time", "set", "--dry-run"])
    captured = capsys.readouterr()
    assert result == 2
    assert "time dry-run requires" in captured.err
    assert "camera-admin-password" not in captured.err


def test_wifi_set_cannot_print_success_without_a_proven_outcome(monkeypatch, capsys):
    async def set_wifi(_self, *_args, **_kwargs):
        return {"result": 0}

    monkeypatch.setattr(StubCamera, "set_wifi", set_wifi, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    result = main(
        [
            *BASE_ARGS,
            "--account-id",
            "test-account",
            "wifi",
            "set",
            "--ssid",
            "test-network",
            "--wifi-password",
            "test-passphrase",
            "--experimental",
            "--confirm",
            "--recovery-ready",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert not captured.out
    assert "without a proven outcome" in captured.err


def test_ptz_move_cli_passes_explicit_bound_and_safety_flags(monkeypatch, capsys):
    called = {}

    async def move_ptz(_self, direction, duration, *, experimental, confirm):
        called.update(
            direction=direction,
            duration=duration,
            experimental=experimental,
            confirm=confirm,
        )
        return {"start": {"result": 0}, "stop": {"result": 0}}

    monkeypatch.setattr(StubCamera, "move_ptz", move_ptz, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)
    assert (
        main(
            [
                *BASE_ARGS,
                "ptz",
                "move",
                "left",
                "--duration",
                "0.5",
                "--experimental",
                "--confirm",
            ]
        )
        == 0
    )
    assert called == {
        "direction": "left",
        "duration": 0.5,
        "experimental": True,
        "confirm": True,
    }
    assert "physically verify that movement stopped" in capsys.readouterr().out


def test_osd_clock_status_prints_only_mode_without_experimental(monkeypatch, capsys):
    async def get_osd_12h_mode(_self):
        return True

    monkeypatch.setattr(StubCamera, "get_osd_12h_mode", get_osd_12h_mode, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "osd", "clock", "status"]) == 0
    output = capsys.readouterr().out
    assert '"mode": "12h"' in output
    assert "12h_mode_support" not in output


def test_timestamp_osd_status_prints_only_boolean(monkeypatch, capsys):
    async def get_timestamp_osd(_self):
        return True

    monkeypatch.setattr(StubCamera, "get_timestamp_osd", get_timestamp_osd, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "osd", "timestamp", "status"]) == 0
    assert '"enabled": true' in capsys.readouterr().out


@pytest.mark.parametrize(("subcommand", "expected"), [("12h", True), ("24h", False)])
def test_osd_clock_set_forwards_confirmed_mode(
    subcommand,
    expected,
    monkeypatch,
    capsys,
):
    called = {}

    async def set_osd_12h_mode(_self, twelve_hour):
        called.update(twelve_hour=twelve_hour)
        return twelve_hour

    monkeypatch.setattr(StubCamera, "set_osd_12h_mode", set_osd_12h_mode, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "osd", "clock", subcommand]) == 0
    assert called == {"twelve_hour": expected}
    assert "verify with osd clock status" in capsys.readouterr().out


def test_alarm_led_status_prints_boolean_without_experimental(monkeypatch, capsys):
    async def get_alarm_led(_self):
        return True

    monkeypatch.setattr(StubCamera, "get_alarm_led", get_alarm_led, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "alarm-led", "status"]) == 0
    assert '"enabled": true' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "method_name", "expected"),
    [
        ("siren", "get_siren_state", True),
        ("light", "get_light_state", False),
    ],
)
def test_confirmed_actuator_status_cli_needs_no_experimental(
    command,
    method_name,
    expected,
    monkeypatch,
    capsys,
):
    async def get_state(_self):
        return expected

    monkeypatch.setattr(StubCamera, method_name, get_state, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, command, "status"]) == 0
    assert f'"enabled": {str(expected).lower()}' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "method_name", "state", "expected"),
    [
        ("siren", "set_siren", "on", True),
        ("light", "set_light", "off", False),
    ],
)
def test_confirmed_actuator_on_off_cli_remains_unchanged(
    command,
    method_name,
    state,
    expected,
    monkeypatch,
    capsys,
):
    called = {}

    async def set_state(_self, enabled):
        called["enabled"] = enabled
        return {"result": 0}

    monkeypatch.setattr(StubCamera, method_name, set_state, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, command, state]) == 0
    assert called == {"enabled": expected}
    assert '"result": 0' in capsys.readouterr().out


@pytest.mark.parametrize(("subcommand", "expected"), [("on", True), ("off", False)])
def test_alarm_led_set_forwards_state_and_experimental(
    subcommand,
    expected,
    monkeypatch,
    capsys,
):
    called = {}

    async def set_alarm_led(_self, enabled, *, experimental):
        called.update(enabled=enabled, experimental=experimental)
        return enabled

    monkeypatch.setattr(StubCamera, "set_alarm_led", set_alarm_led, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "alarm-led", subcommand, "--experimental"]) == 0
    assert called == {"enabled": expected, "experimental": True}
    assert "verify with alarm-led status" in capsys.readouterr().out


def test_ir_off_prints_an_exact_acknowledgement(monkeypatch, capsys):
    async def set_infrared_light(_self, enabled, *, experimental):
        assert (enabled, experimental) == (False, True)
        return {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": 0}

    monkeypatch.setattr(StubCamera, "set_infrared_light", set_infrared_light, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "ir", "set", "off", "--experimental"]) == 0
    output = capsys.readouterr().out
    assert '"InfraredLaser": 0' in output
    assert "verify with ir status" in output


def test_logo_osd_status_uses_confirmed_getter_and_prints_boolean(monkeypatch, capsys):
    async def get_logo_osd(_self):
        return False

    monkeypatch.setattr(StubCamera, "get_logo_osd", get_logo_osd, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "osd", "logo", "status"]) == 0
    assert '"enabled": false' in capsys.readouterr().out


def test_imaging_status_prints_only_normalized_adjustments(monkeypatch, capsys):
    async def get_image_adjustments(_self):
        return {"available": True, "brightness": 64, "contrast": 32}

    monkeypatch.setattr(
        StubCamera,
        "get_image_adjustments",
        get_image_adjustments,
        raising=False,
    )
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "imaging", "status"]) == 0
    output = capsys.readouterr().out
    assert '"brightness": 64' in output
    assert '"contrast": 32' in output
    assert "vbright" not in output


def test_device_info_prints_only_allowlisted_software_fields(monkeypatch, capsys):
    async def get_device_software_info(_self):
        return {
            "available": True,
            "system_version": "system-test",
            "application_version": "application-test",
            "kernel_version": "kernel-test",
        }

    monkeypatch.setattr(
        StubCamera,
        "get_device_software_info",
        get_device_software_info,
        raising=False,
    )
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "device", "info"]) == 0
    output = capsys.readouterr().out
    assert '"system_version": "system-test"' in output
    assert '"application_version": "application-test"' in output
    assert '"kernel_version": "kernel-test"' in output
    assert "vuid" not in output.casefold()
    assert "wifi" not in output.casefold()


@pytest.mark.parametrize(("subcommand", "expected"), [("on", True), ("off", False)])
def test_logo_osd_set_forwards_state_and_experimental(
    subcommand,
    expected,
    monkeypatch,
    capsys,
):
    called = {}

    async def set_logo_osd(_self, enabled, *, experimental):
        called.update(enabled=enabled, experimental=experimental)
        return {"result": 0}

    monkeypatch.setattr(StubCamera, "set_logo_osd", set_logo_osd, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "osd", "logo", subcommand, "--experimental"]) == 0
    assert called == {"enabled": expected, "experimental": True}
    assert "verify with osd logo status" in capsys.readouterr().out


def test_raw_params_redacts_complete_quoted_credentials(monkeypatch, capsys):
    async def send_raw_cgi(_self, path, **_kwargs):
        assert path == "/get_params.cgi"
        return "var WebPwd='first;SECOND_PART'; var alias='visible';"

    monkeypatch.setattr(StubCamera, "send_raw_cgi", send_raw_cgi, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "raw", "/get_params.cgi"]) == 0
    output = capsys.readouterr().out
    assert "first" not in output
    assert "SECOND_PART" not in output
    assert "visible" in output


def test_status_redacts_real_device_id(monkeypatch, capsys):
    async def get_status(_self):
        return {"realdeviceid": "VSTG-000002-BBBBB", "result": 0}

    monkeypatch.setattr(StubCamera, "get_status", get_status, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)

    assert main([*BASE_ARGS, "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"realdeviceid": "***", "result": 0}


def test_params_prints_only_a_field_count(monkeypatch, capsys):
    async def get_params(_self):
        return {
            "WebPwd": "web-secret",
            "user3_pwd": "slot-secret",
            "alias": "camera",
        }

    monkeypatch.setattr(StubCamera, "get_params", get_params, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)
    assert main([*BASE_ARGS, "params"]) == 0
    output = capsys.readouterr().out
    assert '"field_count": 3' in output
    assert "web-secret" not in output
    assert "slot-secret" not in output


def test_stream_probe_never_prints_rtsp_credentials(monkeypatch, capsys):
    async def get_rtsp_stream(_self, **_kwargs):
        return RTSPStream(
            host="192.0.2.10",
            port=10554,
            username="private-user",
            password="private-password",
        )

    async def probe(_stream, *, timeout):
        assert timeout == 8.0
        return {"streams": [{"type": "video", "codec": "h264"}]}

    monkeypatch.setattr(StubCamera, "get_rtsp_stream", get_rtsp_stream, raising=False)
    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)
    monkeypatch.setattr(cli_module, "probe_rtsp", probe)

    result = main(
        [
            *BASE_ARGS,
            "stream",
            "probe",
            "--port",
            "10554",
            "--rtsp-username",
            "private-user",
            "--rtsp-password",
            "private-password",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert '"codec": "h264"' in captured.out
    assert all(
        secret not in captured.out
        for secret in ("private-user", "private-password", "camera-admin-password")
    )


@pytest.mark.parametrize(
    "command",
    [
        ["stream", "probe", "--probe-timeout", "0"],
        ["stream", "snapshot", "frame.jpg", "--snapshot-timeout", "nan"],
        ["stream", "record", "clip.mkv", "--duration", "-1"],
        ["audio", "listen", "--duration", "nan"],
    ],
)
def test_invalid_media_bounds_fail_before_camera_access(command, monkeypatch, capsys):
    def unexpected_camera(_config):
        raise AssertionError("invalid local options must not access the camera")

    monkeypatch.setattr(cli_module, "VStarcamCamera", unexpected_camera)

    with pytest.raises(SystemExit) as caught:
        main([*BASE_ARGS, *command])
    assert caught.value.code == 2
    assert "finite and greater than zero" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["record", "snapshot"])
def test_existing_media_output_fails_before_camera_access(action, monkeypatch, tmp_path, capsys):
    output = tmp_path / "existing.mkv"
    output.write_bytes(b"keep this file")

    def unexpected_camera(_config):
        raise AssertionError("invalid local output must not access the camera")

    monkeypatch.setattr(cli_module, "VStarcamCamera", unexpected_camera)

    assert main([*BASE_ARGS, "stream", action, str(output)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == b"keep this file"
