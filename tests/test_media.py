from __future__ import annotations

import pytest

from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    MediaConfigurationError,
    ServiceChangeUncertainError,
)
from vstarcamctl.media import (
    build_audio_stream_path,
    build_audio_volume_set_path,
    build_onvif_set_path,
    build_pppp_livestream_path,
    build_record_audio_set_path,
    build_rtsp_set_path,
    parse_audio_status,
    parse_onvif_status,
    parse_rtsp_status,
)
from vstarcamctl.media_stream import RTSPStream
from vstarcamctl.transport import FakeTransport


def config(**overrides):
    values = {
        "host": "192.0.2.10",
        "vuid": "VE123456",
        "username": "admin",
        "password": "camera-secret",
        "transport": "fake",
        "retries": 3,
    }
    values.update(overrides)
    return VStarcamConfig(**values)


def test_rtsp_status_never_returns_credentials():
    assert parse_rtsp_status(
        {
            "rtspenable": 1,
            "rtspport": 10554,
            "rtspuser": "stream-user",
            "rtsppwd": "stream-secret",
        },
        {"rtsp_auth_enable": 1, "user3_name": "admin", "user3_pwd": "secret"},
    ) == {
        "enabled": True,
        "port": 10554,
        "authentication_enabled": True,
        "credential_source": "dedicated_rtsp",
        "dedicated_username_configured": True,
        "dedicated_password_configured": True,
    }


def test_rtsp_status_identifies_camera_account_auth_without_disclosing_it():
    assert parse_rtsp_status(
        {"rtspenable": 1, "rtspport": 10554, "rtspuser": "", "rtsppwd": ""},
        {"rtsp_auth_enable": 1, "user3_name": "admin", "user3_pwd": "secret"},
    ) == {
        "enabled": True,
        "port": 10554,
        "authentication_enabled": True,
        "credential_source": "camera_account",
        "dedicated_username_configured": False,
        "dedicated_password_configured": False,
    }


def test_onvif_and_audio_status_normalization():
    assert parse_onvif_status({"onvifenable": 0}) == {"enabled": False}
    assert parse_audio_status({"disable_audio": 0}, {"record_audio": 1}) == {
        "capability_flag_present": True,
        "recording_setting_present": True,
        "microphone_volume_present": False,
        "speaker_volume_present": False,
        "g711a_capability_present": False,
        "two_way_audio_capability_present": False,
        "audio_available": True,
        "recording_enabled": True,
    }


def test_audio_status_includes_safe_volume_and_duplex_capability_fields():
    assert parse_audio_status(
        {"disable_audio": 0},
        {"record_audio": 1},
        {"involume": 12, "outvolume": 23},
        {"support_audio_g711a": 1, "EchoCancellationVer": 2},
    ) == {
        "capability_flag_present": True,
        "recording_setting_present": True,
        "microphone_volume_present": True,
        "speaker_volume_present": True,
        "g711a_capability_present": True,
        "two_way_audio_capability_present": True,
        "audio_available": True,
        "recording_enabled": True,
        "microphone_volume": 12,
        "speaker_volume": 23,
        "volume_range": {"minimum": 0, "maximum": 31},
        "g711a_supported": True,
        "two_way_audio_supported": True,
    }


def test_media_paths_are_validated_and_urlencoded():
    assert build_rtsp_set_path(True, 10554, "stream user", "p+a ssword") == (
        "/set_rtsp.cgi?rtspenable=1&rtspport=10554&rtspuser=stream+user&rtsppwd=p%2Ba+ssword"
    )
    assert build_onvif_set_path(False) == "/set_onvif.cgi?onvifenable=0"
    assert build_record_audio_set_path(True) == "/set_recordsch.cgi?record_audio=1"
    assert build_audio_volume_set_path("microphone", 0) == ("/camera_control.cgi?param=24&value=0")
    assert build_audio_volume_set_path("speaker", 31) == ("/camera_control.cgi?param=25&value=31")
    assert build_audio_stream_path(enabled=True) == "/audiostream.cgi?streamid=7"
    assert build_audio_stream_path(enabled=False) == "/audiostream.cgi?streamid=16"
    assert (
        build_pppp_livestream_path(enabled=True, substream=0)
        == "/livestream.cgi?streamid=10&substream=0"
    )
    assert (
        build_pppp_livestream_path(enabled=True, substream=1)
        == "/livestream.cgi?streamid=10&substream=1"
    )
    assert build_pppp_livestream_path(enabled=False) == "/livestream.cgi?streamid=16&substream=0"
    with pytest.raises(MediaConfigurationError, match="RTSP port"):
        build_rtsp_set_path(True, 0, "user", "password")
    with pytest.raises(MediaConfigurationError, match="substream"):
        build_pppp_livestream_path(enabled=True, substream=2)


async def test_live_confirmed_getters_use_exact_paths_and_hide_rtsp_credentials():
    transport = FakeTransport(
        [
            "var rtspenable=1; var rtspport=10554; "
            "var rtspuser='stream-user'; var rtsppwd='stream-secret';",
            "var rtsp_auth_enable=1; var user3_name='admin'; var user3_pwd='secret';",
            "var onvifenable=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_rtsp_settings() == {
        "enabled": True,
        "port": 10554,
        "authentication_enabled": True,
        "credential_source": "dedicated_rtsp",
        "dedicated_username_configured": True,
        "dedicated_password_configured": True,
    }
    assert await camera.get_onvif_settings() == {"enabled": False}
    assert transport.requests == [
        "GET /get_rtsp.cgi?loginuse=admin&user=admin&pwd=camera-secret&",
        "GET /get_params.cgi?loginuse=admin&user=admin&pwd=camera-secret&",
        "GET /get_onvif.cgi?loginuse=admin&user=admin&pwd=camera-secret&",
    ]


async def test_audio_status_combines_params_and_record_getters():
    transport = FakeTransport(
        [
            "var disable_audio=0;",
            "var record_audio=1; var enc_bitrate=1024;",
            "var involume=12; var outvolume=23;",
            "var support_audio_g711a=1; var EchoCancellationVer=2;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_audio_settings() == {
        "capability_flag_present": True,
        "recording_setting_present": True,
        "microphone_volume_present": True,
        "speaker_volume_present": True,
        "g711a_capability_present": True,
        "two_way_audio_capability_present": True,
        "audio_available": True,
        "recording_enabled": True,
        "microphone_volume": 12,
        "speaker_volume": 23,
        "volume_range": {"minimum": 0, "maximum": 31},
        "g711a_supported": True,
        "two_way_audio_supported": True,
    }
    assert len(transport.requests) == 4
    assert "/get_params.cgi?" in transport.requests[0]
    assert "/get_record.cgi?" in transport.requests[1]
    assert "/get_camera_params.cgi?" in transport.requests[2]
    assert "/get_status.cgi?" in transport.requests[3]


async def test_audio_volume_write_is_experimental_one_shot():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.set_audio_volume("speaker", 15)
    assert await camera.set_audio_volume("speaker", 15, experimental=True) == {"result": 0}
    assert len(transport.requests) == 1
    assert "/camera_control.cgi?param=25&value=15&" in transport.requests[0]

    with pytest.raises(MediaConfigurationError, match="0 to 31"):
        camera.format_audio_volume_set_cgi("microphone", 32)


async def test_rtsp_stream_uses_live_port_and_camera_account_credentials():
    transport = FakeTransport(
        [
            "var rtspenable=1; var rtspport=10554; var rtspuser=''; var rtsppwd='';",
            (
                "var rtsp_auth_enable=1; var user3_name='admin'; "
                "var user3_pwd='slot-secret'; var WebPwd='web-secret';"
            ),
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    stream = await camera.get_rtsp_stream(quality="sub")

    assert isinstance(stream, RTSPStream)
    assert stream.port == 10554
    assert stream.quality == "sub"
    assert stream.url.endswith("/tcp/av0_1")
    assert "web-secret" in stream.url
    assert "camera-secret" not in stream.url


async def test_dedicated_rtsp_credentials_must_be_supplied_explicitly():
    transport = FakeTransport(
        [
            "var rtspenable=1; var rtspport=10554; var rtspuser='configured'; var rtsppwd='set';",
            "var rtsp_auth_enable=1;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="provide both"):
        await camera.get_rtsp_stream()


async def test_explicit_rtsp_port_requires_explicit_credentials():
    camera = VStarcamCamera(config(), transport=FakeTransport())

    with pytest.raises(MediaConfigurationError, match="explicit RTSP port"):
        await camera.get_rtsp_stream(port=10554)

    stream = await camera.get_rtsp_stream(
        port=10554,
        username="rtsp-user",
        password="rtsp-secret",
    )
    assert stream.url.startswith("rtsp://rtsp-user:rtsp-secret@")
    assert camera.transport.connect_count == 0


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("set_rtsp", (True,)),
        ("set_onvif", (True,)),
        ("set_record_audio", (True,)),
    ],
)
async def test_media_writes_require_experimental_and_confirm_before_connect(method, args):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    call = getattr(camera, method)

    with pytest.raises(ExperimentalCommandError):
        await call(*args)
    with pytest.raises(ConfirmationRequiredError, match="--confirm"):
        await call(*args, experimental=True)

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_rtsp_write_preserves_omitted_values_and_is_sent_once():
    transport = FakeTransport(
        [
            "var rtspenable=1; var rtspport=10554; "
            "var rtspuser='existing-user'; var rtsppwd='existing-secret';",
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_rtsp(
        False,
        experimental=True,
        confirm=True,
    ) == {"result": 0}

    assert len(transport.requests) == 2
    assert transport.requests[1] == (
        "GET /set_rtsp.cgi?rtspenable=0&rtspport=10554"
        "&rtspuser=existing-user&rtsppwd=existing-secret"
        "&loginuse=admin&user=admin&pwd=camera-secret&"
    )


async def test_onvif_and_record_audio_writes_use_guarded_candidates():
    transport = FakeTransport(["var result=0;", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_onvif(True, experimental=True, confirm=True) == {"result": 0}
    assert await camera.set_record_audio(False, experimental=True, confirm=True) == {"result": 0}
    assert "/set_onvif.cgi?onvifenable=1&" in transport.requests[0]
    assert "/set_recordsch.cgi?record_audio=0&" in transport.requests[1]


@pytest.mark.parametrize(
    "path",
    [
        "/set_rtsp.cgi?rtspenable=1&rtspport=10554&rtspuser=x&rtsppwd=y",
        "/set_onvif.cgi?onvifenable=1",
        "/set_recordsch.cgi?record_audio=1",
    ],
)
async def test_raw_media_writes_cannot_enable_request_retries(path):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ConfirmationRequiredError, match="retries are forbidden"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)

    assert transport.connect_count == 0


async def test_service_write_timeout_is_not_retried():
    transport = FakeTransport([TimeoutError("service restarted")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_onvif(True, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert transport.connect_count == 1
