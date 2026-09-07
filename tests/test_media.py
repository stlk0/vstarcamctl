from __future__ import annotations

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    MediaConfigurationError,
    ServiceChangeUncertainError,
)
from vstarcamctl.media import (
    build_audio_volume_set_path,
    build_onvif_set_path,
    build_pppp_livestream_path,
    build_record_audio_set_path,
    build_rtsp_set_path,
    parse_audio_status,
    parse_audio_volume_set_response,
    parse_livestream_set_response,
    parse_onvif_status,
    parse_rtsp_status,
)
from vstarcamctl.media_stream import RTSPStream


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
        "configured_enabled": True,
        "reported_port": 10554,
        "authentication_enabled": True,
        "credential_source": "dedicated_rtsp",
        "dedicated_username_configured": True,
        "dedicated_password_configured": True,
    }


def test_rtsp_status_identifies_camera_account_auth_without_disclosing_it():
    assert parse_rtsp_status(
        {"rtspenable": 1, "rtspport": 10554, "rtspuser": "", "rtsppwd": ""},
        {"rtsp_auth_enable": 1, "WebPwd": "secret"},
    ) == {
        "configured_enabled": True,
        "reported_port": 10554,
        "authentication_enabled": True,
        "credential_source": "camera_account",
        "dedicated_username_configured": False,
        "dedicated_password_configured": False,
    }


def test_rtsp_status_does_not_claim_resolvable_camera_credentials_without_webpwd():
    assert (
        parse_rtsp_status(
            {"rtspenable": 1, "rtspport": 10554, "rtspuser": "", "rtsppwd": ""},
            {"rtsp_auth_enable": 1, "user3_name": "admin", "user3_pwd": "secret"},
        )["credential_source"]
        == "unknown"
    )


@pytest.mark.parametrize("auth_value", [None, "invalid", -1])
def test_rtsp_status_preserves_unknown_authentication_state(auth_value):
    params = {} if auth_value is None else {"rtsp_auth_enable": auth_value}
    status = parse_rtsp_status(
        {
            "rtspenable": 1,
            "rtspport": 10554,
            "rtspuser": "",
            "rtsppwd": "",
        },
        params,
    )

    assert status["authentication_enabled"] is None
    assert status["credential_source"] == "unknown"


def test_onvif_and_audio_status_normalization():
    assert parse_onvif_status({"onvifenable": 0}) == {"configured_enabled": False}
    assert parse_audio_status({"disable_audio": 0}, {"record_audio": 1}) == {
        "capability_flag_present": True,
        "recording_setting_present": True,
        "microphone_volume_present": False,
        "speaker_volume_present": False,
        "g711a_capability_present": False,
        "echo_cancellation_capability_present": False,
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
        "echo_cancellation_capability_present": True,
        "audio_available": True,
        "recording_enabled": True,
        "microphone_volume": 12,
        "speaker_volume": 23,
        "volume_range": {"minimum": 0, "maximum": 31},
        "g711a_supported": True,
        "echo_cancellation_supported": True,
        "full_duplex_audio_supported": True,
    }


def test_audio_status_preserves_unknown_and_explicit_unavailable_capability():
    unknown = parse_audio_status({}, {})
    assert unknown["capability_flag_present"] is False
    assert "audio_available" not in unknown

    unavailable = parse_audio_status({"disable_audio": 1}, {})
    assert unavailable["capability_flag_present"] is True
    assert unavailable["audio_available"] is False


@pytest.mark.parametrize(
    "status",
    [
        {"support_g711a": -1, "EchoCancellationVer": "invalid"},
        {"support_audio_g711a": "invalid", "EchoCancellationVer": -1},
        {"support_g711a": None, "EchoCancellationVer": None},
    ],
)
def test_audio_status_preserves_malformed_capabilities_as_unknown(status):
    result = parse_audio_status({}, {}, status=status)

    assert result["g711a_capability_present"] is True
    assert result["echo_cancellation_capability_present"] is True
    assert "g711a_supported" not in result
    assert "echo_cancellation_supported" not in result
    assert "full_duplex_audio_supported" not in result


def test_media_paths_are_validated_and_urlencoded():
    assert build_rtsp_set_path(True, 10554, "stream user", "p+a ssword") == (
        "/set_rtsp.cgi?rtspenable=1&rtspport=10554&rtspuser=stream+user&rtsppwd=p%2Ba+ssword"
    )
    assert build_onvif_set_path(False) == "/set_onvif.cgi?onvifenable=0"
    assert build_record_audio_set_path(True) == "/set_recordsch.cgi?record_audio=1"
    assert build_audio_volume_set_path("microphone", 0) == ("/camera_control.cgi?param=24&value=0")
    assert build_audio_volume_set_path("speaker", 31) == ("/camera_control.cgi?param=25&value=31")
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
    assert build_rtsp_set_path(False, 0, "", "") == (
        "/set_rtsp.cgi?rtspenable=0&rtspport=0&rtspuser=&rtsppwd="
    )
    long_credential = "x" * 65
    assert f"rtspuser={long_credential}" in build_rtsp_set_path(
        True,
        10554,
        long_credential,
        long_credential,
    )
    with pytest.raises(MediaConfigurationError, match="substream"):
        build_pppp_livestream_path(enabled=True, substream=2)


def test_livestream_acknowledgement_requires_exact_zero_result():
    assert parse_livestream_set_response({"result": "0", "vendor": "ignored"}) == {"result": 0}
    for payload in ({}, {"result": 1}, {"result": "ok"}):
        with pytest.raises(MediaConfigurationError):
            parse_livestream_set_response(payload)


def test_audio_volume_acknowledgement_requires_exact_zero_result():
    assert parse_audio_volume_set_response({"result": "0", "vendor": "ignored"}) == {"result": 0}
    for payload in ({}, {"result": 1}, {"result": "ok"}):
        with pytest.raises(MediaConfigurationError):
            parse_audio_volume_set_response(payload)


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
        "configured_enabled": True,
        "reported_port": 10554,
        "authentication_enabled": True,
        "credential_source": "dedicated_rtsp",
        "dedicated_username_configured": True,
        "dedicated_password_configured": True,
    }
    assert await camera.get_onvif_settings() == {"configured_enabled": False}
    assert transport.requests == [
        "GET /get_rtsp.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&",
        "GET /get_params.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&",
        "GET /get_onvif.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&",
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
        "echo_cancellation_capability_present": True,
        "audio_available": True,
        "recording_enabled": True,
        "microphone_volume": 12,
        "speaker_volume": 23,
        "volume_range": {"minimum": 0, "maximum": 31},
        "g711a_supported": True,
        "echo_cancellation_supported": True,
        "full_duplex_audio_supported": True,
    }
    assert len(transport.requests) == 4
    assert "/get_params.cgi?" in transport.requests[0]
    assert "/get_record.cgi?" in transport.requests[1]
    assert "/get_camera_params.cgi?" in transport.requests[2]
    assert transport.requests[3] == (
        "GET /get_status.cgi?name=admin&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    )


async def test_microphone_volume_write_is_confirmed_one_shot():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    assert await camera.set_audio_volume("microphone", 15) == {"result": 0}
    assert len(transport.requests) == 1
    assert "/camera_control.cgi?param=24&value=15&" in transport.requests[0]


async def test_speaker_volume_write_is_experimental_one_shot():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.set_audio_volume("speaker", 15)
    assert await camera.set_audio_volume("speaker", 15, experimental=True) == {"result": 0}
    assert len(transport.requests) == 1
    assert "/camera_control.cgi?param=25&value=15&" in transport.requests[0]

    with pytest.raises(MediaConfigurationError, match="0 to 31"):
        build_audio_volume_set_path("microphone", 32)


@pytest.mark.parametrize("response", ["var result=1;", "var status='ok';"])
async def test_audio_volume_write_rejects_invalid_acknowledgement(response):
    transport = FakeTransport([response])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="acknowledgement was invalid"):
        await camera.set_audio_volume("microphone", 30)

    assert len(transport.requests) == 1


async def test_audio_volume_write_timeout_is_not_retried():
    transport = FakeTransport([TimeoutError("camera-control timeout")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="sent once without retry"):
        await camera.set_audio_volume("microphone", 30)

    assert len(transport.requests) == 1
    assert transport.connect_count == 1


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


async def test_unknown_rtsp_authentication_requires_explicit_credentials():
    transport = FakeTransport(
        [
            "var rtspenable=1; var rtspport=10554; var rtspuser=''; var rtsppwd='';",
            "var WebPwd='configured-but-auth-state-missing';",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="authentication state is unknown"):
        await camera.get_rtsp_stream()


async def test_rtsp_stream_reports_disabled_service_with_zero_port():
    transport = FakeTransport(["var rtspenable=0; var rtspport=0;", "var rtsp_auth_enable=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="RTSP is disabled"):
        await camera.get_rtsp_stream()


async def test_rtsp_configuration_flag_does_not_claim_listener_availability():
    transport = FakeTransport(
        [
            "var rtspenable=0; var rtspport=10554; var rtspuser=''; var rtsppwd='';",
            "var rtsp_auth_enable=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    stream = await camera.get_rtsp_stream()

    assert stream.port == 10554
    assert stream.username is None


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
            "var result=ok;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="response command code"):
        await camera.set_rtsp(
            False,
            experimental=True,
            confirm=True,
        )

    assert len(transport.requests) == 2
    assert transport.requests[1] == (
        "GET /set_rtsp.cgi?rtspenable=0&rtspport=10554"
        "&rtspuser=existing-user&rtsppwd=existing-secret"
        "&loginuse=admin&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    )


async def test_rtsp_enable_requires_explicit_port_for_disabled_zero_sentinel():
    transport = FakeTransport(
        ["var rtspenable=0; var rtspport=0; var rtspuser=''; var rtsppwd='';"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="provide an explicit port"):
        await camera.set_rtsp(True, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert "/get_rtsp.cgi?" in transport.requests[0]


async def test_rtsp_disable_preserves_zero_sentinel():
    transport = FakeTransport(
        [
            "var rtspenable=0; var rtspport=0; var rtspuser=''; var rtsppwd='';",
            "var result=ok;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="response command code"):
        await camera.set_rtsp(
            False,
            experimental=True,
            confirm=True,
        )
    assert "/set_rtsp.cgi?rtspenable=0&rtspport=0&rtspuser=&rtsppwd=&" in transport.requests[1]


async def test_rtsp_enable_accepts_explicit_port_with_disabled_empty_profile():
    transport = FakeTransport(
        [
            "var rtspenable=0; var rtspport=0; var rtspuser=''; var rtsppwd='';",
            "var result=ok;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="response command code"):
        await camera.set_rtsp(
            True,
            port=10554,
            experimental=True,
            confirm=True,
        )
    assert (
        "/set_rtsp.cgi?rtspenable=1&rtspport=10554&rtspuser=&rtsppwd=&" in (transport.requests[1])
    )


@pytest.mark.parametrize(
    ("method", "argument", "path"),
    [
        ("set_onvif", True, "/set_onvif.cgi?onvifenable=1&"),
        ("set_record_audio", False, "/set_recordsch.cgi?record_audio=0&"),
    ],
)
async def test_unverified_media_writes_send_once_but_never_claim_success(
    method,
    argument,
    path,
):
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await getattr(camera, method)(argument, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert path in transport.requests[0]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("set_rtsp", (True,)),
        ("set_onvif", (True,)),
        ("set_record_audio", (True,)),
    ],
)
async def test_unverified_media_writes_treat_every_parseable_response_as_unknown(method, args):
    transport = FakeTransport(["var result=1;"])
    camera = VStarcamCamera(config(), transport=transport)
    kwargs = (
        {"port": 10554, "username": "user", "password": "password"} if method == "set_rtsp" else {}
    )

    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await getattr(camera, method)(
            *args,
            **kwargs,
            experimental=True,
            confirm=True,
        )

    assert len(transport.requests) == 1


async def test_media_service_write_rejects_acknowledgement_without_result():
    transport = FakeTransport(["var status='ok';"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await camera.set_onvif(True, experimental=True, confirm=True)

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/set_rtsp.cgi?rtspenable=1&rtspport=10554&rtspuser=x&rtsppwd=y",
        "/set_onvif.cgi?onvifenable=1",
        "/set_recordsch.cgi?record_audio=1",
    ],
)
async def test_raw_media_writes_require_their_validating_high_level_api(path):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_service_write_timeout_is_not_retried():
    transport = FakeTransport([TimeoutError("service restarted")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_onvif(True, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert transport.connect_count == 1
