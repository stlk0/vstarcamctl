from __future__ import annotations

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import ImagingConfigurationError
from vstarcamctl.imaging import parse_image_adjustments


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"result": 0, "vbright": 64, "vcontrast": "32", "vendor": "ignored"},
            {"available": True, "brightness": 64, "contrast": 32},
        ),
        ({"result": "0", "vbright": "7"}, {"available": True, "brightness": 7}),
        ({"result": 0}, {"available": False}),
    ],
)
def test_image_adjustments_are_narrow_and_numeric(payload, expected):
    assert parse_image_adjustments(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"result": 1, "vbright": 1},
        {"result": 0, "vbright": True},
        {"result": 0, "vcontrast": "not-a-number"},
    ],
)
def test_image_adjustments_reject_malformed_responses(payload):
    with pytest.raises(ImagingConfigurationError):
        parse_image_adjustments(payload)


@pytest.mark.parametrize("timeouts", [0, 1])
async def test_camera_image_adjustments_use_exact_confirmed_endpoint(timeouts):
    transport = FakeTransport(
        [TimeoutError("read timed out")] * timeouts
        + ["var result=0; var vbright=64; var vcontrast=32;"]
    )
    camera = VStarcamCamera(config(retries=1), transport=transport)

    assert await camera.get_image_adjustments() == {
        "available": True,
        "brightness": 64,
        "contrast": 32,
    }
    assert transport.requests == [
        "GET /get_camera_params.cgi?loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ] * (timeouts + 1)
    assert transport.connect_count == timeouts + 1
    assert transport.close_count == timeouts
