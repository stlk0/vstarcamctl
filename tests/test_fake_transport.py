from __future__ import annotations

import pytest

from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    ServiceChangeUncertainError,
)
from vstarcamctl.transport import FakeTransport


def config(**overrides):
    values = {
        "host": "192.0.2.10",
        "vuid": "VE123456",
        "username": "admin",
        "password": "secret",
        "transport": "fake",
        "retries": 1,
    }
    values.update(overrides)
    return VStarcamConfig(**values)


class FlakyConnectTransport(FakeTransport):
    def __init__(self, failures: int, responses=None):
        super().__init__(responses)
        self.failures = failures

    async def connect(self) -> None:
        self.connect_count += 1
        if self.failures:
            self.failures -= 1
            raise TimeoutError("transient handshake timeout")
        self._connected = True


async def test_fake_transport_captures_exact_command_and_parses_response():
    transport = FakeTransport(['var alarm_status=0; var alias="camera";'])
    camera = VStarcamCamera(config(), transport=transport)
    result = await camera.get_status()
    await camera.close()
    assert result == {"alarm_status": 0, "alias": "camera"}
    assert transport.requests == [
        "GET /get_status.cgi?vuid=VE123456&loginuse=admin&user=admin&pwd=secret&"
    ]


async def test_timeout_reconnects_and_retries():
    transport = FakeTransport([TimeoutError("first attempt"), "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.get_params() == {"result": 0}
    assert transport.connect_count == 2
    assert transport.close_count == 1
    assert len(transport.requests) == 2


async def test_initial_connection_honors_retry_count():
    transport = FlakyConnectTransport(1)
    camera = VStarcamCamera(config(retries=1), transport=transport)
    await camera.connect()
    assert camera.connected
    assert transport.connect_count == 2
    assert transport.close_count == 1


async def test_unknown_raw_command_is_blocked_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="unknown raw CGI"):
        await camera.send_raw_cgi("/unknown.cgi")
    assert transport.connect_count == 0


async def test_confirmed_siren_command_needs_no_opt_in():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_siren(True) == {"result": 0}
    assert len(transport.requests) == 1


async def test_motion_detection_preserves_current_fields_and_writes_once():
    transport = FakeTransport(
        [
            "var alarm_motion_armed=0; var alarm_motion_sensitivity=6; var alarm_audio=1;",
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(retries=3), transport=transport)
    assert await camera.set_motion_detection(True, experimental=True, confirm=True) == {"result": 0}
    assert len(transport.requests) == 2
    assert "GET /get_params.cgi?" in transport.requests[0]
    assert (
        "GET /set_alarm.cgi?enable_alarm_audio=1&motion_armed=1"
        "&motion_sensitivity=6&loginuse=admin&user=admin&pwd=secret&"
    ) == transport.requests[1]


async def test_human_status_combines_detection_and_tracking_getters():
    transport = FakeTransport(
        [
            "var HumanoidDetection=1; var sensitive=2; var bHumanoidFrame=1;",
            "var enable=0; var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.get_human_detection_settings(include_tracking=True, experimental=True) == {
        "available": True,
        "enabled": True,
        "sensitivity": 2,
        "sensitivity_scale": "device-specific 0 to 3",
        "frame_enabled": True,
        "tracking_enabled": False,
    }
    assert "cmd=2126&command=1" in transport.requests[0]
    assert "cmd=2127&command=1" in transport.requests[1]


async def test_live_confirmed_human_tracking_getter_needs_no_opt_in():
    transport = FakeTransport(
        ["var sensitive=0; var bHumanoidFrame=0;", "var enable=0; var result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)
    result = await camera.get_human_detection_settings(include_tracking=True)
    assert result["tracking_enabled"] is False
    assert len(transport.requests) == 2


async def test_human_write_requires_both_safety_gates_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_human_tracking(True)
    with pytest.raises(ConfirmationRequiredError, match="pass --confirm"):
        await camera.set_human_tracking(True, experimental=True)
    assert transport.connect_count == 0


async def test_live_confirmed_human_writes_need_no_safety_flags():
    transport = FakeTransport(["var result=0; var bHumanoidFrame=1;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_human_frame(True) == {
        "result": 0,
        "bHumanoidFrame": 1,
    }
    assert len(transport.requests) == 1

    transport = FakeTransport(["var result=0; var sensitive=1;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_human_sensitivity(1) == {
        "result": 0,
        "sensitive": 1,
    }
    assert len(transport.requests) == 1


async def test_night_and_ir_status_are_normalized_from_separate_getters():
    transport = FakeTransport(
        [
            "var night_vision_mode=0; var ircut=1; var vendor_secret='ignored';",
            "var InfraredLaser=0; var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.get_night_vision_settings() == {
        "available": True,
        "low_light_mode": "black-white",
        "mode": "black-white",
    }
    assert await camera.get_infrared_light_settings() == {
        "available": True,
        "enabled": False,
    }
    assert "GET /get_camera_params.cgi?" in transport.requests[0]
    assert "cmd=2120&command=1" in transport.requests[1]


async def test_time_status_uses_confirmed_params_getter_and_normalizes_fields():
    transport = FakeTransport(
        [
            "var now=1700000000; var tz=-19800; var ntp_enable=1; "
            "var ntp_svr='time.windows.com'; var WebPwd='ignored';"
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)
    result = await camera.get_time_settings()
    assert result["timezone"] == "UTC+05:30"
    assert result["ntp_enabled"] is True
    assert result["ntp_server"] == "time.windows.com"
    assert len(transport.requests) == 1
    assert "GET /get_params.cgi?" in transport.requests[0]


async def test_live_confirmed_time_write_needs_no_experimental_flag():
    transport = FakeTransport(["var result='ok';"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_time_settings(
        timezone_offset_seconds=19800,
        ntp_enabled=True,
        ntp_server="time.windows.com",
        unix_time=1_700_000_000,
    ) == {"result": "ok"}
    assert len(transport.requests) == 1


async def test_time_write_preserves_omitted_fields_and_is_one_shot():
    transport = FakeTransport(
        [
            "var tz=-19800; var ntp_enable=1; var ntp_svr='time.windows.com';",
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)
    assert await camera.set_time_settings(
        unix_time=1_700_000_000,
        experimental=True,
    ) == {"result": 0}
    assert len(transport.requests) == 2
    assert "GET /get_params.cgi?" in transport.requests[0]
    assert (
        "GET /set_datetime.cgi?tz=-19800&ntp_enable=1&ntp_svr=time.windows.com&now=1700000000&"
    ) in transport.requests[1]


async def test_time_write_timeout_is_not_retried():
    transport = FakeTransport([TimeoutError("no acknowledgement")])
    camera = VStarcamCamera(config(retries=5), transport=transport)
    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_time_settings(
            timezone_offset_seconds=0,
            ntp_enabled=False,
            ntp_server="",
            unix_time=1_700_000_000,
            experimental=True,
        )
    assert len(transport.requests) == 1


async def test_night_and_ir_writes_require_experimental_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_night_vision("black-white")
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_infrared_light(True)
    assert transport.connect_count == 0


async def test_night_transition_and_ir_write_are_one_shot():
    transport = FakeTransport(
        [
            "var result=0;",
            "var result=0;",
            "var result=0; var InfraredLaser=1;",
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)
    assert await camera.set_night_vision("smart", experimental=True) == [
        {"result": 0},
        {"result": 0},
    ]
    assert await camera.set_infrared_light(True, experimental=True) == {
        "result": 0,
        "InfraredLaser": 1,
    }
    assert len(transport.requests) == 3
    assert "camera_control.cgi?param=14&value=1" in transport.requests[0]
    assert "camera_control.cgi?param=33&value=2" in transport.requests[1]
    assert "cmd=2120&command=0&InfraredLaser=1" in transport.requests[2]


async def test_night_transition_timeout_is_not_retried():
    transport = FakeTransport(["var result=0;", TimeoutError("no acknowledgement")])
    camera = VStarcamCamera(config(retries=5), transport=transport)
    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_night_vision("smart", experimental=True)
    assert len(transport.requests) == 2
    assert "camera_control.cgi?param=14&value=1" in transport.requests[0]
    assert "camera_control.cgi?param=33&value=2" in transport.requests[1]


async def test_human_write_is_one_shot_even_when_normal_retries_are_enabled():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)
    assert await camera.set_human_tracking(True, experimental=True, confirm=True) == {"result": 0}
    assert len(transport.requests) == 1
    assert "cmd=2127&command=0&enable=1" in transport.requests[0]
