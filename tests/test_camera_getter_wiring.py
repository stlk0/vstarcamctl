"""Focused wiring checks for normalized camera getters."""

from __future__ import annotations

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera


async def test_motion_settings_read_and_narrow_the_protected_params_response():
    transport = FakeTransport(
        ["var alarm_motion_armed=1; var alarm_motion_sensitivity=3; var wifi_ssid='must-not-leak';"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_motion_detection_settings() == {
        "available": True,
        "sensitivity_available": True,
        "enabled": True,
        "sensitivity": 3,
        "sensitivity_scale": "0 (highest) to 9 (lowest)",
    }
    assert transport.requests == [
        "GET /get_params.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]


async def test_wifi_status_read_and_narrow_the_protected_params_response():
    transport = FakeTransport(
        [
            "var wifi_enable=1; var wifi_ssid='test-network'; var wifi_channel=6; "
            "var WebPwd='must-not-leak';"
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_wifi_status() == {
        "available": True,
        "enabled": 1,
        "ssid": "test-network",
        "channel": 6,
    }
    assert transport.requests == [
        "GET /get_params.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]
