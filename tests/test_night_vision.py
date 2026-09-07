from __future__ import annotations

import pytest

from vstarcamctl.errors import NightVisionConfigurationError
from vstarcamctl.night_vision import (
    build_infrared_light_set_path,
    build_night_vision_set_paths,
    parse_infrared_light_set_response,
    parse_infrared_light_status,
    parse_night_vision_set_response,
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
        (
            {"ircut": 0},
            {"available": True, "low_light_mode": "starlight", "mode": "starlight"},
        ),
        (
            {"night_vision_mode": 0},
            {"available": True, "mode": "black-white-or-starlight"},
        ),
        ({}, {"available": False}),
    ],
)
def test_night_vision_status_is_normalized_without_vendor_extras(payload, expected):
    assert parse_night_vision_status(payload) == expected


def test_night_vision_status_rejects_unknown_values():
    with pytest.raises(NightVisionConfigurationError, match="0 to 2"):
        parse_night_vision_status({"night_vision_mode": 3})
    with pytest.raises(NightVisionConfigurationError, match="must be 0 or 1"):
        parse_night_vision_status({"ircut": 7})


def test_night_vision_set_response_requires_success():
    assert parse_night_vision_set_response({"result": "0", "vendor": "ignored"}) == {"result": 0}
    for payload in ({}, {"result": 1}, {"result": True}):
        with pytest.raises(NightVisionConfigurationError):
            parse_night_vision_set_response(payload)


def test_infrared_builder_and_status_use_command_2120_shape():
    assert build_infrared_light_set_path(True) == (
        "/trans_cmd_string.cgi?cmd=2120&command=0&InfraredLaser=1"
    )
    assert build_infrared_light_set_path(False) == (
        "/trans_cmd_string.cgi?cmd=2120&command=0&InfraredLaser=0"
    )
    assert parse_infrared_light_status({"InfraredLaser": "1", "vendor_extra": "ignored"}) == {
        "available": True,
        "logical_control_enabled": True,
    }
    assert parse_infrared_light_status({}) == {"available": False}


@pytest.mark.parametrize(
    "payload",
    [
        {"result": 1, "InfraredLaser": 1},
        {"cmd": 2121, "InfraredLaser": 1},
        {"command": 0, "InfraredLaser": 1},
    ],
)
def test_infrared_status_rejects_contradictory_response_markers(payload):
    with pytest.raises(NightVisionConfigurationError):
        parse_infrared_light_status(payload)


@pytest.mark.parametrize(("state", "expected"), [(0, False), ("1", True)])
def test_infrared_set_response_requires_exact_command_and_echo(state, expected):
    assert parse_infrared_light_set_response(
        {
            "result": "0",
            "cmd": 2120,
            "command": "0",
            "InfraredLaser": state,
            "uid": "ignored",
        },
        expected,
    ) == {
        "result": 0,
        "cmd": 2120,
        "command": 0,
        "InfraredLaser": int(expected),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": 2120, "command": 0, "InfraredLaser": 1},
        {"result": 1, "cmd": 2120, "command": 0, "InfraredLaser": 1},
        {"result": 0, "cmd": 2121, "command": 0, "InfraredLaser": 1},
        {"result": 0, "cmd": 2120, "command": 1, "InfraredLaser": 1},
        {"result": 0, "cmd": 2120, "command": 0},
        {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": 2},
        {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": 0},
        {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": True},
        {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": 1.0},
    ],
)
def test_infrared_set_response_rejects_invalid_or_mismatched_ack(payload):
    with pytest.raises(NightVisionConfigurationError):
        parse_infrared_light_set_response(payload, True)


def test_infrared_set_response_rejects_non_boolean_expectation():
    with pytest.raises(NightVisionConfigurationError, match="boolean"):
        parse_infrared_light_set_response(
            {"result": 0, "cmd": 2120, "command": 0, "InfraredLaser": 1},
            1,
        )
