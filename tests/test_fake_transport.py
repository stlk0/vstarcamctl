from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    CapabilityUnavailableError,
    CatalogError,
    ConfigError,
    ConfirmationRequiredError,
    DetectionConfigurationError,
    ExperimentalCommandError,
    RawCommandError,
    ServiceChangeCancelledError,
    ServiceChangeUncertainError,
    TransportAuthenticationError,
    TransportTimeoutError,
    TransportUnavailableError,
)
from vstarcamctl.transport import DiscoveredCamera

DISCOVERED_CAMERA = DiscoveredCamera(
    host="192.0.2.20",
    port=40000,
    device_id="VSTG-000002-BBBBB",
    protocol="binary",
    encryption="PSK",
)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            {"password": "camera-secret"},
            VStarcamConfig(
                host="192.0.2.20",
                device_id="VSTG-000002-BBBBB",
                username="admin",
                password="camera-secret",
                udp_port=40000,
                auth_mode="basic",
            ),
        ),
        (
            {
                "password": "custom-secret",
                "username": "operator",
                "psk": "custom-seed",
                "source_address": "192.0.2.2",
                "discovery_port": 41000,
                "timeout": 2.5,
                "retries": 0,
            },
            VStarcamConfig(
                host="192.0.2.20",
                source_address="192.0.2.2",
                device_id="VSTG-000002-BBBBB",
                username="operator",
                password="custom-secret",
                psk="custom-seed",
                udp_port=40000,
                discovery_port=41000,
                auth_mode="basic",
                timeout=2.5,
                retries=0,
            ),
        ),
    ],
)
def test_camera_from_discovery_maps_config(options, expected):
    camera = VStarcamCamera.from_discovery(DISCOVERED_CAMERA, **options)

    assert camera.config == expected


@pytest.mark.parametrize(
    ("options", "expected"),
    [({}, "vstarcam2019"), ({"psk": "explicit-seed"}, "explicit-seed")],
    ids=["inherit-discovered-key", "explicit-key-wins"],
)
def test_camera_from_discovery_selects_the_psk(options, expected):
    discovered = replace(DISCOVERED_CAMERA, psk="vstarcam2019")

    camera = VStarcamCamera.from_discovery(
        discovered,
        password="camera-secret",
        **options,
    )

    assert camera.config.psk == expected


def test_camera_from_discovery_rejects_an_explicit_empty_psk():
    discovered = replace(DISCOVERED_CAMERA, psk="vstarcam2019")

    with pytest.raises(ConfigError, match="non-empty ASCII"):
        VStarcamCamera.from_discovery(discovered, password="camera-secret", psk="")


def test_camera_from_discovery_requires_explicit_password():
    with pytest.raises(TypeError, match="password"):
        VStarcamCamera.from_discovery(DISCOVERED_CAMERA)


def test_camera_from_discovery_rejects_incompatible_protocol():
    discovered = replace(DISCOVERED_CAMERA, protocol="json", encryption="NONE")

    with pytest.raises(TransportUnavailableError, match="requires binary protocol"):
        VStarcamCamera.from_discovery(discovered, password="camera-secret")


def _complete_alarm_response() -> str:
    fields = {
        "alarm_motion_armed": 0,
        "alarm_motion_sensitivity": 6,
        "enable_alarm_audio": 1,
        "input_armed": 1,
        "ioin_level": 0,
        "iolinkage": 1,
        "ioout_level": 0,
        "preset": 2,
        "mail": 1,
        "snapshot": 1,
        "record": 1,
        "upload_interval": 30,
        "schedule_enable": 1,
        "CloudVideoDuration": 20,
    }
    fields.update(
        {
            f"schedule_{day}_{slot}": day_index * 10 + slot
            for day_index, day in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
            for slot in range(3)
        }
    )
    fields.update({f"defense_plan{index}": index % 2 for index in range(1, 22)})
    return " ".join(f"var {name}={value};" for name, value in fields.items())


def _motion_regions_response(mask: int = 0) -> str:
    fields = {
        "result": 0,
        "cmd": 2123,
        "command": 1,
        **{f"md_reign{row}": mask for row in range(18)},
    }
    return " ".join(f"var {name}={value};" for name, value in fields.items())


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


async def test_fake_transport_requires_every_response_to_be_explicitly_queued():
    transport = FakeTransport()
    await transport.connect()

    with pytest.raises(AssertionError, match="no queued response"):
        await transport.request("synthetic request", timeout=1)

    assert transport.requests == ["synthetic request"]


async def test_fake_transport_captures_exact_command_and_parses_response():
    transport = FakeTransport(['var alarm_status=0; var alias="camera";'])
    camera = VStarcamCamera(config(), transport=transport)
    result = await camera.get_status()
    await camera.close()
    assert result == {"alarm_status": 0, "alias": "camera"}
    assert transport.requests == [
        "GET /get_status.cgi?vuid=VSTG-000001-AAAAA&loginuse=admin&user=admin&pwd=camera-secret&"
    ]


async def test_timeout_reconnects_and_retries():
    transport = FakeTransport([TimeoutError("first attempt"), "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.get_params() == {"result": 0}
    assert transport.connect_count == 2
    assert transport.close_count == 1
    assert len(transport.requests) == 2


async def test_authentication_failure_is_not_retried():
    failure = TransportAuthenticationError("camera rejected authentication")
    transport = FakeTransport([failure, "var result=0;"])
    camera = VStarcamCamera(config(retries=3), transport=transport)

    with pytest.raises(TransportAuthenticationError) as caught:
        await camera.get_params()

    assert caught.value is failure
    assert len(transport.requests) == 1
    assert transport.connect_count == 1


async def test_initial_connection_honors_retry_count():
    transport = FlakyConnectTransport(1)
    camera = VStarcamCamera(config(retries=1), transport=transport)
    await camera.connect()
    assert camera.connected
    assert transport.connect_count == 2
    assert transport.close_count == 1


@pytest.mark.parametrize("retries", [0, 2])
async def test_connection_stops_after_configured_attempts(retries):
    transport = FlakyConnectTransport(retries + 1)
    camera = VStarcamCamera(config(retries=retries), transport=transport)

    with pytest.raises(TransportTimeoutError, match="camera connection failed"):
        await camera.connect()

    assert transport.connect_count == retries + 1
    assert transport.close_count == retries + 1
    assert not camera.connected
    assert transport.requests == []


@pytest.mark.parametrize("retries", [0, 2])
async def test_read_stops_after_configured_attempts(retries):
    transport = FakeTransport([TimeoutError("read timed out")] * (retries + 1) + ["var result=0;"])
    camera = VStarcamCamera(config(retries=retries), transport=transport)

    with pytest.raises(TransportTimeoutError, match="camera request failed"):
        await camera.get_params()

    assert len(transport.requests) == retries + 1
    assert transport.connect_count == retries + 1
    assert transport.close_count == retries


async def test_read_cancellation_is_propagated_without_retry():
    transport = FakeTransport([asyncio.CancelledError("cancelled read")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(asyncio.CancelledError, match="cancelled read"):
        await camera.get_params()

    assert len(transport.requests) == 1
    assert transport.connect_count == 1


async def test_unknown_raw_command_is_blocked_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="unknown raw CGI"):
        await camera.send_raw_cgi("/unknown.cgi")
    with pytest.raises(ConfirmationRequiredError, match="write risk"):
        await camera.send_raw_cgi("/unknown.cgi", experimental=True)
    assert transport.connect_count == 0


async def test_unknown_raw_command_is_never_retried_after_timeout():
    transport = FakeTransport([TimeoutError("ack lost")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(TransportTimeoutError):
        await camera.send_raw_cgi("/unknown.cgi", experimental=True, confirm=True)
    assert len(transport.requests) == 1
    assert transport.connect_count == 1


@pytest.mark.parametrize(
    "path",
    [
        "/trans_cmd_string.cgi?cmd=2109&command=0&alarmLed=2",
        "/trans_cmd_string.cgi?cmd=4109&command=1&osd_12h_mode=2",
        "/camera_control.cgi?param=11&value=2",
        "/camera_control.cgi?param=25&value=999",
    ],
)
async def test_parameterized_known_write_requires_high_level_api(path):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            recovery_ready=True,
            retry_requests=False,
        )
    assert transport.connect_count == 0
    assert transport.requests == []


async def test_fixed_known_one_shot_write_requires_raw_retries_off():
    acknowledgement = "var result=0;"
    transport = FakeTransport([acknowledgement])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ConfirmationRequiredError, match="retries are forbidden"):
        await camera.send_raw_cgi("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1")
    assert (
        await camera.send_raw_cgi(
            "/trans_cmd_string.cgi?cmd=2109&command=0&siren=1",
            retry_requests=False,
        )
        == acknowledgement
    )
    assert len(transport.requests) == 1


async def test_login_status_is_available_only_through_allowlisted_high_level_reads():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi("/get_status.cgi?name=admin")
    assert transport.requests == []


async def test_motion_regions_use_exact_capability_gated_getter():
    transport = FakeTransport(
        [
            "var result=0; var support_motionArea=1;",
            _motion_regions_response((1 << 22) - 1),
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.get_motion_detection_regions()

    assert result == {
        "row_count": 18,
        "column_count": 22,
        "detection_enabled": [[True] * 22 for _ in range(18)],
    }
    assert "/get_status.cgi?name=admin&" in transport.requests[0]
    assert "/trans_cmd_string.cgi?cmd=2123&command=1&sensor=0&" in transport.requests[1]


async def test_motion_regions_explicit_false_capability_blocks_before_getter():
    transport = FakeTransport(["var result=0; var support_motionArea=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="support_motionArea"):
        await camera.get_motion_detection_regions()

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "status",
    ["var result=0;", "var result=0; var support_motionArea='invalid';"],
)
async def test_motion_regions_unknown_capability_allows_read(status):
    transport = FakeTransport([status, _motion_regions_response()])
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.get_motion_detection_regions()

    assert result["detection_enabled"] == [[False] * 22 for _ in range(18)]
    assert len(transport.requests) == 2


async def test_motion_regions_read_retries_after_timeout():
    transport = FakeTransport(
        [
            "var result=0; var support_motionArea=1;",
            TimeoutError("transient read timeout"),
            _motion_regions_response(),
        ]
    )
    camera = VStarcamCamera(config(retries=1), transport=transport)

    result = await camera.get_motion_detection_regions()

    assert result["row_count"] == 18
    assert len(transport.requests) == 3
    assert transport.connect_count == 2


async def test_motion_regions_raw_path_cannot_bypass_normalization_or_capability_gate():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi("/trans_cmd_string.cgi?cmd=2123&command=1&sensor=0")

    assert transport.connect_count == 0


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (
            "/set_users.cgi?pwd_change_realtime=1&OwnerUser=x&OwnerPwd=y",
            "guarded high-level API",
        ),
        (
            "/set_users.cgi?pwd_change_realtime=1&user3=x&pwd3=y",
            "dangerous_do_not_run",
        ),
    ],
)
async def test_parameterized_account_write_keeps_absolute_block_policy(path, message):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match=message):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            recovery_ready=True,
            retry_requests=False,
        )
    assert transport.connect_count == 0
    assert transport.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/restore_factory.cgi",
        "/set_formatsd.cgi",
        "/del_file.cgi",
        "/auto_download_file.cgi",
        "/callstatus.cgi",
        "/set_ipc_binding_info.cgi",
        "/set_update_push_user.cgi",
        "/get_factory_param.cgi",
    ],
)
async def test_blocked_catalog_endpoint_rejects_arbitrary_query_fields(path):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="dangerous_do_not_run"):
        await camera.send_raw_cgi(
            f"{path}?arbitrary=value",
            experimental=True,
            confirm=True,
            recovery_ready=True,
            retry_requests=False,
        )
    assert transport.connect_count == 0


async def test_percent_encoded_endpoint_cannot_bypass_catalog_policy():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(RawCommandError, match="canonical root-level"):
        await camera.send_raw_cgi(
            "/%72estore_factory.cgi?arbitrary=value",
            experimental=True,
            confirm=True,
            retry_requests=False,
        )
    assert transport.connect_count == 0


@pytest.mark.parametrize("command_id", [4120, 8000])
async def test_catalog_unknown_write_requires_confirmation_and_one_shot(command_id):
    path = f"/trans_cmd_string.cgi?cmd={command_id}&command=0"
    transport = FakeTransport([TimeoutError("ack lost")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ConfirmationRequiredError, match="--confirm"):
        await camera.send_raw_cgi(path, experimental=True, retry_requests=False)
    with pytest.raises(ConfirmationRequiredError, match="retries are forbidden"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)
    with pytest.raises(TransportTimeoutError):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            retry_requests=False,
        )
    assert len(transport.requests) == 1
    assert transport.connect_count == 1


async def test_confirmed_siren_command_needs_no_opt_in():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_siren(True) == {"result": 0}
    assert len(transport.requests) == 1


async def test_motion_detection_preserves_current_fields_and_writes_once():
    transport = FakeTransport(
        [
            _complete_alarm_response(),
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(retries=3), transport=transport)
    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await camera.set_motion_detection(True, experimental=True, confirm=True)
    assert len(transport.requests) == 2
    assert "GET /get_params.cgi?" in transport.requests[0]
    assert "GET /set_alarm.cgi?enable_alarm_audio=1&motion_armed=1" in transport.requests[1]
    assert "&motion_sensitivity=6&input_armed=1&ioin_level=0&iolinkage=1" in transport.requests[1]
    assert "&schedule_sat_2=62&defense_plan1=1" in transport.requests[1]
    assert "&defense_plan21=1&CloudVideoDuration=20&" in transport.requests[1]


async def test_motion_detection_refuses_incomplete_profile_before_write():
    transport = FakeTransport(
        ["var alarm_motion_armed=0; var alarm_motion_sensitivity=6; var alarm_audio=1;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(DetectionConfigurationError, match="wire-named alarm profile"):
        await camera.set_motion_detection(True, experimental=True, confirm=True)
    assert len(transport.requests) == 1


async def test_motion_detection_requires_recoverable_enabled_and_sensitivity_prestate():
    response = _complete_alarm_response().replace("var alarm_motion_armed=0; ", "")
    transport = FakeTransport([response])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(DetectionConfigurationError, match="recoverable"):
        await camera.set_motion_detection(
            True,
            sensitivity=6,
            experimental=True,
            confirm=True,
        )

    assert len(transport.requests) == 1
    assert all("set_alarm.cgi" not in request for request in transport.requests)


async def test_human_status_combines_detection_and_tracking_getters():
    transport = FakeTransport(
        [
            "var HumanoidDetection=1; var sensitive=2; var bHumanoidFrame=1;",
            "var enable=0; var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.get_human_detection_settings(include_tracking=True) == {
        "available": True,
        "enabled": True,
        "sensitivity": 2,
        "sensitivity_scale": "device-specific 0 to 3",
        "frame_enabled": True,
        "tracking_setting_enabled": False,
    }
    assert "cmd=2126&command=1" in transport.requests[0]
    assert "cmd=2127&command=1" in transport.requests[1]


async def test_human_write_requires_both_safety_gates_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_human_tracking(True)
    with pytest.raises(ConfirmationRequiredError, match="pass --confirm"):
        await camera.set_human_tracking(True, experimental=True)
    assert transport.connect_count == 0


async def test_human_tracking_explicitly_blocks_fixed_camera_before_write():
    transport = FakeTransport(["var haveMotor=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="haveMotor"):
        await camera.set_human_tracking(True, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert "cmd=2127&command=0" not in transport.requests[0]


async def test_human_tracking_unknown_capability_can_send_but_never_claims_success():
    transport = FakeTransport(["var firmware_version='test';", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await camera.set_human_tracking(True, experimental=True, confirm=True)

    assert len(transport.requests) == 2
    assert "cmd=2127&command=0&enable=1" in transport.requests[1]


async def test_main_human_write_can_send_but_never_claims_unproven_success():
    transport = FakeTransport(["var support_humanDetect=1;", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="inverse getter are not proven"):
        await camera.set_human_detection(
            True,
            sensitivity=2,
            distance=3,
            experimental=True,
            confirm=True,
        )

    assert len(transport.requests) == 2
    assert "cmd=2106&command=4&humanDetection=2" in transport.requests[1]


async def test_human_zoom_tracking_requires_reported_inverse_before_write():
    transport = FakeTransport(["var support_humanoid_zoom=1;", "var bHumanoidFrame=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(DetectionConfigurationError, match="inverse state"):
        await camera.set_human_zoom_tracking(True, experimental=True, confirm=True)

    assert len(transport.requests) == 2
    assert not any("humanoid_zoom=1" in request for request in transport.requests)


async def test_human_zoom_tracking_with_inverse_sends_but_never_claims_success():
    transport = FakeTransport(
        [
            "var support_humanoid_zoom=1;",
            "var humanoid_zoom=0;",
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="not proven"):
        await camera.set_human_zoom_tracking(True, experimental=True, confirm=True)

    assert len(transport.requests) == 3
    assert "cmd=2126&command=0&humanoid_zoom=1" in transport.requests[2]


async def test_live_confirmed_human_writes_need_no_safety_flags():
    transport = FakeTransport(
        ["var firmware_version='test';", "var result=0; var bHumanoidFrame=1;"]
    )
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_human_frame(True) == {
        "result": 0,
        "bHumanoidFrame": 1,
    }
    assert len(transport.requests) == 2

    transport = FakeTransport(["var firmware_version='test';", "var result=0; var sensitive=1;"])
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_human_sensitivity(1) == {
        "result": 0,
        "sensitive": 1,
    }
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("method", "argument", "response"),
    [
        ("set_human_frame", True, "var result=0; var bHumanoidFrame=0;"),
        ("set_human_sensitivity", 1, "var result=0; var sensitive=0;"),
    ],
)
async def test_confirmed_human_writes_reject_mismatched_echoes(method, argument, response):
    transport = FakeTransport(["var firmware_version='test';", response])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await getattr(camera, method)(argument)

    assert len(transport.requests) == 2


async def test_confirmed_human_write_timeout_is_not_retried():
    transport = FakeTransport(
        ["var firmware_version='test';", TimeoutError("command-2126 timeout")]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="sent once without retry"):
        await camera.set_human_frame(True)

    assert len(transport.requests) == 2


async def test_explicit_false_capability_blocks_model_specific_writes():
    transport = FakeTransport(["var support_humanoidFrame=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(CapabilityUnavailableError, match="support_humanoidFrame"):
        await camera.set_human_frame(True)
    assert len(transport.requests) == 1

    transport = FakeTransport(["var support_manual_light=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(CapabilityUnavailableError, match="support_manual_light"):
        await camera.set_light(True)
    assert len(transport.requests) == 1

    for mode in ("smart", "black-white"):
        transport = FakeTransport(["var support_full_color_night_vision_mode=0;"])
        camera = VStarcamCamera(config(), transport=transport)
        with pytest.raises(CapabilityUnavailableError, match="full_color"):
            await camera.set_night_vision(mode, experimental=True)
        assert len(transport.requests) == 1


async def test_missing_or_malformed_capability_does_not_block_validated_profile():
    transport = FakeTransport(
        ["var support_humanoidFrame='unknown';", "var result=0; var bHumanoidFrame=1;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_human_frame(True) == {"result": 0, "bHumanoidFrame": 1}
    assert len(transport.requests) == 2

    transport = FakeTransport(
        ["var support_manual_light=0; var support_pure_white_light=1;", "var result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)
    assert await camera.set_light(True) == {"result": 0}
    assert len(transport.requests) == 2


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
        "logical_control_enabled": False,
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
            "var result='ok';",
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)
    assert await camera.set_time_settings(unix_time=1_700_000_000) == {"result": "ok"}
    assert len(transport.requests) == 2
    assert "GET /get_params.cgi?" in transport.requests[0]
    assert (
        "GET /set_datetime.cgi?tz=-19800&ntp_enable=1&ntp_svr=time.windows.com&now=1700000000&"
    ) in transport.requests[1]


async def test_time_write_invalid_acknowledgement_is_uncertain_and_not_retried():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_time_settings(
            timezone_offset_seconds=0,
            ntp_enabled=False,
            ntp_server="",
            unix_time=1_700_000_000,
        )

    assert len(transport.requests) == 1


async def test_one_shot_write_is_not_uncertain_when_connection_never_opened():
    transport = FlakyConnectTransport(1)
    camera = VStarcamCamera(config(retries=0), transport=transport)
    with pytest.raises(TransportTimeoutError):
        await camera.set_time_settings(
            timezone_offset_seconds=0,
            ntp_enabled=False,
            ntp_server="",
            unix_time=1_700_000_000,
        )
    assert transport.requests == []


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
            "var firmware_version='test';",
            "var result=0;",
            "var result=0;",
            "var result=0; var cmd=2120; var command=0; var InfraredLaser=1;",
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)
    assert await camera.set_night_vision("smart", experimental=True) == [
        {"result": 0},
        {"result": 0},
    ]
    assert await camera.set_infrared_light(True, experimental=True) == {
        "result": 0,
        "cmd": 2120,
        "command": 0,
        "InfraredLaser": 1,
    }
    assert len(transport.requests) == 4
    assert "camera_control.cgi?param=14&value=1" in transport.requests[1]
    assert "camera_control.cgi?param=33&value=2" in transport.requests[2]
    assert "cmd=2120&command=0&InfraredLaser=1" in transport.requests[3]


async def test_night_vision_transitions_are_serialized_per_camera():
    class GatedNightTransport(FakeTransport):
        def __init__(self):
            super().__init__(
                [
                    "var firmware_version='test';",
                    "var result=0;",
                    "var result=0;",
                    "var firmware_version='test';",
                    "var result=0;",
                    "var result=0;",
                ]
            )
            self.first_step_seen = asyncio.Event()
            self.release_first_step = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if "camera_control.cgi" in command and not self.first_step_seen.is_set():
                self.requests.append(command)
                response = self.responses.popleft()
                assert isinstance(response, str)
                self.first_step_seen.set()
                await self.release_first_step.wait()
                return response
            return await super().request(command, timeout=timeout)

    transport = GatedNightTransport()
    camera = VStarcamCamera(config(), transport=transport)
    first = asyncio.create_task(camera.set_night_vision("smart", experimental=True))
    await transport.first_step_seen.wait()
    second = asyncio.create_task(camera.set_night_vision("black-white", experimental=True))
    await asyncio.sleep(0)

    assert len(transport.requests) == 2
    transport.release_first_step.set()
    await asyncio.gather(first, second)

    transitions = [request for request in transport.requests if "camera_control.cgi" in request]
    assert "param=14&value=1" in transitions[0]
    assert "param=33&value=2" in transitions[1]
    assert "param=33&value=0" in transitions[2]
    assert "param=14&value=1" in transitions[3]


async def test_infrared_invalid_acknowledgement_is_an_uncertain_one_shot():
    transport = FakeTransport(["var result=0; var cmd=2120; var command=0; var InfraredLaser=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_infrared_light(True, experimental=True)

    assert len(transport.requests) == 1
    assert "cmd=2120&command=0&InfraredLaser=1" in transport.requests[0]


async def test_infrared_timeout_is_an_uncertain_one_shot():
    transport = FakeTransport([TimeoutError("no acknowledgement")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_infrared_light(True, experimental=True)

    assert len(transport.requests) == 1


async def test_invalid_night_acknowledgement_stops_before_second_step():
    transport = FakeTransport(["var firmware_version='test';", "var result=1;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_night_vision("black-white", experimental=True)

    assert len(transport.requests) == 2
    assert "camera_control.cgi?param=33&value=0" in transport.requests[1]


async def test_night_transition_timeout_is_not_retried():
    transport = FakeTransport(
        ["var firmware_version='test';", "var result=0;", TimeoutError("no acknowledgement")]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)
    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_night_vision("smart", experimental=True)
    assert len(transport.requests) == 3
    assert "camera_control.cgi?param=14&value=1" in transport.requests[1]
    assert "camera_control.cgi?param=33&value=2" in transport.requests[2]


async def test_night_transition_reconnect_failure_preserves_partial_write_outcome():
    failure = TransportTimeoutError("camera did not become ready")

    class DisconnectAfterFirstStepTransport(FakeTransport):
        async def connect(self):
            if self.requests:
                self.connect_count += 1
                raise failure
            await super().connect()

        async def request(self, command: str, *, timeout: float) -> str:
            response = await super().request(command, timeout=timeout)
            if "camera_control.cgi" in command:
                self._connected = False
            return response

    transport = DisconnectAfterFirstStepTransport(["var firmware_version='test';", "var result=0;"])
    camera = VStarcamCamera(config(retries=0), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="after one step completed") as caught:
        await camera.set_night_vision("smart", experimental=True)

    assert caught.value.__cause__ is failure
    assert transport.connect_count == 2
    assert len(transport.requests) == 2
    assert "camera_control.cgi?param=14&value=1" in transport.requests[1]


async def test_night_cancellation_after_first_step_reports_uncertain_mode():
    cancellation = asyncio.CancelledError("second step cancelled")
    transport = FakeTransport(["var firmware_version='test';", "var result=0;", cancellation])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeCancelledError, match="after one step completed") as caught:
        await camera.set_night_vision("smart", experimental=True)

    assert isinstance(caught.value, asyncio.CancelledError)
    assert caught.value.__cause__ is cancellation
    assert len(transport.requests) == 3


async def test_malformed_known_wifi_write_fails_closed():
    path = "/set_wifi.cgi?ssid=x&channel=6&authtype=4&wpa_psk=y&enable=1&vendor_extra=1"
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(account_id="test-account"), transport=transport)

    with pytest.raises(CatalogError, match="extra fields"):
        await camera.send_raw_cgi(path)
    assert transport.requests == []
