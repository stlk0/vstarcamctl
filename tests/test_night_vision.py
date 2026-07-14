from __future__ import annotations

import pytest

from vstarcamctl.errors import NightVisionConfigurationError
from vstarcamctl.night_vision import (
    build_infrared_light_set_path,
    build_night_vision_set_paths,
    parse_infrared_light_status,
    parse_night_vision_status,
)


@pytest.mark.parametrize(
    "mode,expected",
    [
        (
            "black-white",
            (
                "/camera_control.cgi?param=33&value=0",
                "/camera_control.cgi?param=14&value=1",
            ),
        ),
        (
            "starlight",
            (
                "/camera_control.cgi?param=33&value=0",
                "/camera_control.cgi?param=14&value=0",
            ),
        ),
        (
            "full-color",
            (
                "/camera_control.cgi?param=14&value=1",
                "/camera_control.cgi?param=33&value=1",
            ),
        ),
        (
            "smart",
            (
                "/camera_control.cgi?param=14&value=1",
                "/camera_control.cgi?param=33&value=2",
            ),
        ),
    ],
)
def test_night_vision_builder_preserves_documented_transition_order(mode, expected):
    assert build_night_vision_set_paths(mode) == expected


def test_night_vision_builder_rejects_unknown_modes():
    with pytest.raises(NightVisionConfigurationError, match="must be one of"):
        build_night_vision_set_paths("infrared")


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {"night_vision_mode": 0, "ircut": 1, "password": "ignored"},
            {
                "available": True,
                "low_light_mode": "black-white",
                "mode": "black-white",
            },
        ),
        (
            {"night_vision_mode": "0", "ircut": "0"},
            {
                "available": True,
                "low_light_mode": "starlight",
                "mode": "starlight",
            },
        ),
        (
            {"night_vision_mode": 1, "ircut": 1},
            {
                "available": True,
                "low_light_mode": "black-white",
                "mode": "full-color",
            },
        ),
        (
            {"night_vision_mode": 2},
            {"available": True, "mode": "smart"},
        ),
        ({}, {"available": False}),
    ],
)
def test_night_vision_status_is_normalized_without_vendor_extras(payload, expected):
    assert parse_night_vision_status(payload) == expected


def test_night_vision_status_rejects_unknown_values():
    with pytest.raises(NightVisionConfigurationError, match="0, 1, or 2"):
        parse_night_vision_status({"night_vision_mode": 3})
    with pytest.raises(NightVisionConfigurationError, match="must be 0 or 1"):
        parse_night_vision_status({"ircut": 7})


def test_infrared_builder_and_status_use_command_2120_shape():
    assert build_infrared_light_set_path(True) == (
        "/trans_cmd_string.cgi?cmd=2120&command=0&InfraredLaser=1"
    )
    assert build_infrared_light_set_path(False).endswith("InfraredLaser=0")
    assert parse_infrared_light_status({"InfraredLaser": "1", "vendor_extra": "ignored"}) == {
        "available": True,
        "enabled": True,
    }
    assert parse_infrared_light_status({}) == {"available": False}
