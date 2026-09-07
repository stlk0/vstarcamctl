from __future__ import annotations

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.device_info import parse_device_software_info
from vstarcamctl.errors import DeviceInfoConfigurationError


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "result": 0,
                "sys_ver": "system-test",
                "app_version": "application-test",
                "kernel_version": "kernel-test",
                "vuid": "must-not-leak",
                "wifi_ssid": "must-not-leak",
            },
            {
                "available": True,
                "system_version": "system-test",
                "application_version": "application-test",
                "kernel_version": "kernel-test",
            },
        ),
        (
            {"result": "0", "app_version": "application-test"},
            {"available": True, "application_version": "application-test"},
        ),
        ({"result": 0}, {"available": False}),
    ],
)
def test_device_software_info_is_narrow_and_textual(payload, expected):
    assert parse_device_software_info(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"result": 1, "sys_ver": "system-test"},
        {"result": 0, "sys_ver": 1},
        {"result": 0, "app_version": "  "},
        {"result": 0, "kernel_version": "unsafe\nvalue"},
    ],
)
def test_device_software_info_rejects_malformed_responses(payload):
    with pytest.raises(DeviceInfoConfigurationError):
        parse_device_software_info(payload)


@pytest.mark.parametrize("timeouts", [0, 1])
async def test_camera_device_software_info_uses_exact_confirmed_status_request(timeouts):
    transport = FakeTransport(
        [TimeoutError("read timed out")] * timeouts
        + [
            "var result=0; var sys_ver='system-test'; "
            "var app_version='application-test'; var kernel_version='kernel-test';"
        ]
    )
    camera = VStarcamCamera(config(retries=1), transport=transport)

    assert await camera.get_device_software_info() == {
        "available": True,
        "system_version": "system-test",
        "application_version": "application-test",
        "kernel_version": "kernel-test",
    }
    assert transport.requests == [
        "GET /get_status.cgi?name=admin&loginuse=admin&user=admin&pwd=camera-secret&"
    ] * (timeouts + 1)
    assert transport.connect_count == timeouts + 1
    assert transport.close_count == timeouts
