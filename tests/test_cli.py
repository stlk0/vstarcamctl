import vstarcamctl.cli as cli_module
from vstarcamctl.cli import main
from vstarcamctl.media_stream import RTSPStream


def test_dry_run_never_prints_password_or_vuid(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--vuid",
            "VE123456",
            "--password",
            "supersecret",
            "raw",
            "--dry-run",
            "/get_status.cgi?vuid=VE123456",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert "supersecret" not in captured.out
    assert "VE123456" not in captured.out
    assert "pwd=***" in captured.out


def test_wifi_dry_run_masks_ssid_and_network_password(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--vuid",
            "VE123456",
            "--password",
            "camera-admin-password",
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
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert "private network" not in captured.out
    assert "private-passphrase" not in captured.out
    assert "camera-admin-password" not in captured.out
    assert "ssid=***" in captured.out
    assert "wpa_psk=***" in captured.out


def test_rtsp_dry_run_masks_both_credentials(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--vuid",
            "VE123456",
            "--password",
            "camera-admin-password",
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
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert "stream-user" not in captured.out
    assert "stream-secret" not in captured.out
    assert "camera-admin-password" not in captured.out
    assert "rtspuser=***" in captured.out
    assert "rtsppwd=***" in captured.out


def test_onvif_and_record_audio_dry_runs_do_not_connect(capsys):
    common = [
        "--host",
        "192.0.2.10",
        "--vuid",
        "VE123456",
        "--password",
        "camera-admin-password",
    ]
    assert main([*common, "onvif", "set", "on", "--dry-run"]) == 0
    assert main([*common, "audio", "recording", "off", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "/set_onvif.cgi?onvifenable=1" in output
    assert "/set_recordsch.cgi?record_audio=0" in output


def test_audio_volume_dry_run_builds_guarded_candidate(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--vuid",
            "VE123456",
            "--password",
            "camera-admin-password",
            "audio",
            "volume",
            "speaker",
            "21",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "camera_control.cgi?param=25&value=21" in output
    assert "camera-admin-password" not in output


def test_audio_duplex_is_refused_until_full_duplex_is_implemented(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--password",
            "camera-admin-password",
            "audio",
            "duplex",
            "default",
            "--input-format",
            "pulse",
            "--experimental",
            "--confirm",
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "full-duplex talk is not implemented" in captured.err
    assert "camera-admin-password" not in captured.err


def test_night_and_ir_dry_runs_build_guarded_candidates(capsys):
    common = [
        "--host",
        "192.0.2.10",
        "--vuid",
        "VE123456",
        "--password",
        "camera-admin-password",
    ]
    assert main([*common, "night", "set", "smart", "--dry-run"]) == 0
    assert main([*common, "ir", "set", "on", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "camera_control.cgi?param=14&value=1" in output
    assert "camera_control.cgi?param=33&value=2" in output
    assert "cmd=2120&command=0&InfraredLaser=1" in output
    assert "camera-admin-password" not in output
    assert "VE123456" not in output


def test_time_dry_run_builds_one_shot_request_and_masks_ntp_server(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--vuid",
            "VE123456",
            "--password",
            "camera-admin-password",
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
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "set_datetime.cgi?tz=-19800&ntp_enable=1" in output
    assert "ntp_svr=***" in output
    assert "time.windows.com" not in output
    assert "now=1700000000" in output
    assert "camera-admin-password" not in output
    assert "VE123456" not in output


def test_human_detection_dry_runs_build_each_guarded_candidate(capsys):
    common = [
        "--host",
        "192.0.2.10",
        "--vuid",
        "VE123456",
        "--password",
        "camera-admin-password",
        "human",
    ]
    commands = (
        ["detection", "on", "--sensitivity", "2", "--distance", "3", "--dry-run"],
        ["sensitivity", "1", "--dry-run"],
        ["frame", "off", "--dry-run"],
        ["tracking", "on", "--dry-run"],
        ["zoom-tracking", "on", "--dry-run"],
    )
    for command in commands:
        assert main([*common, *command]) == 0
    output = capsys.readouterr().out
    assert "cmd=2106&command=4&humanDetection=2&DistanceAdjust=3" in output
    assert "cmd=2126&command=0&sensitive=1" in output
    assert "cmd=2126&command=0&bHumanoidFrame=0" in output
    assert "cmd=2127&command=0&enable=1" in output
    assert "cmd=2126&command=0&humanoid_zoom=1" in output


def test_params_prints_only_a_field_count(monkeypatch, capsys):
    class StubCamera:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def get_params(self):
            return {
                "WebPwd": "web-secret",
                "user3_pwd": "slot-secret",
                "alias": "camera",
            }

    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--password",
            "camera-password",
            "params",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert '"field_count": 3' in output
    assert "web-secret" not in output
    assert "slot-secret" not in output


def test_stream_probe_never_prints_rtsp_credentials(monkeypatch, capsys):
    class StubCamera:
        def __init__(self, _config):
            pass

        async def get_rtsp_stream(self, **_kwargs):
            return RTSPStream(
                host="192.0.2.10",
                port=10554,
                username="private-user",
                password="private-password",
            )

        async def close(self):
            pass

    async def stub_probe(_stream, *, timeout):
        assert timeout == 8.0
        return {"streams": [{"type": "video", "codec": "h264"}]}

    monkeypatch.setattr(cli_module, "VStarcamCamera", StubCamera)
    monkeypatch.setattr(cli_module, "probe_rtsp", stub_probe)

    result = main(
        [
            "--host",
            "192.0.2.10",
            "--password",
            "camera-secret",
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
    assert "private-user" not in captured.out
    assert "private-password" not in captured.out
    assert "camera-secret" not in captured.out
