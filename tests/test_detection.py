from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from vstarcamctl.detection import (
    MOTION_REGION_COLUMNS,
    MOTION_REGION_ROWS,
    build_human_detection_set_path,
    build_human_frame_set_path,
    build_human_sensitivity_set_path,
    build_human_tracking_set_path,
    build_human_zoom_tracking_set_path,
    build_motion_detection_set_path,
    parse_human_detection_status,
    parse_human_frame_set_response,
    parse_human_sensitivity_set_response,
    parse_motion_detection_regions,
    parse_motion_detection_status,
)
from vstarcamctl.errors import DetectionConfigurationError


def _complete_alarm_params() -> dict[str, int]:
    params = {
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
    params.update(
        {
            f"schedule_{day}_{slot}": day_index * 10 + slot
            for day_index, day in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
            for slot in range(3)
        }
    )
    params.update({f"defense_plan{index}": index % 2 for index in range(1, 22)})
    return params


def test_motion_status_is_normalized_without_leaking_the_snapshot():
    status = parse_motion_detection_status(
        {
            "alarm_motion_armed": "1",
            "alarm_motion_sensitivity": "4",
            "alarm_pwd": "must-not-be-returned",
        }
    )
    assert status == {
        "available": True,
        "sensitivity_available": True,
        "enabled": True,
        "sensitivity": 4,
        "sensitivity_scale": "0 (highest) to 9 (lowest)",
    }
    assert "must-not-be-returned" not in repr(status)


def _motion_region_payload(**overrides):
    payload = {
        "result": 0,
        "cmd": 2123,
        "command": 1,
        **{f"md_reign{row}": 0 for row in range(MOTION_REGION_ROWS)},
        "uid": "must-not-be-returned",
    }
    payload.update(overrides)
    return payload


def test_motion_regions_normalize_exact_grid_and_bit_order():
    status = parse_motion_detection_regions(
        _motion_region_payload(
            md_reign0=(1 << MOTION_REGION_COLUMNS) - 1,
            md_reign1=1 << (MOTION_REGION_COLUMNS - 1),
            md_reign2=1,
        )
    )
    assert status["row_count"] == 18
    assert status["column_count"] == 22
    assert status["detection_enabled"][0] == [True] * 22
    assert status["detection_enabled"][1] == [True] + [False] * 21
    assert status["detection_enabled"][2] == [False] * 21 + [True]
    assert status["detection_enabled"][3] == [False] * 22
    assert "must-not-be-returned" not in repr(status)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        _motion_region_payload(result=1),
        _motion_region_payload(cmd=2124),
        _motion_region_payload(command=0),
        _motion_region_payload(md_reign0=-1),
        _motion_region_payload(md_reign0=1 << MOTION_REGION_COLUMNS),
        _motion_region_payload(md_reign0=True),
        {key: value for key, value in _motion_region_payload().items() if key != "md_reign17"},
    ],
)
def test_motion_regions_reject_malformed_responses(payload):
    with pytest.raises(DetectionConfigurationError):
        parse_motion_detection_regions(payload)


def test_motion_builder_preserves_complete_alarm_profile_and_validates_scale():
    params = _complete_alarm_params()
    query = dict(
        parse_qsl(urlsplit(build_motion_detection_set_path(False, 7, params=params)).query)
    )
    assert query["enable_alarm_audio"] == "1"
    assert query["motion_armed"] == "0"
    assert query["motion_sensitivity"] == "7"
    assert query["iolinkage"] == "1"
    assert query["schedule_sat_2"] == "62"
    assert query["defense_plan21"] == "1"
    assert query["CloudVideoDuration"] == "20"
    with pytest.raises(DetectionConfigurationError, match="0 to 9"):
        build_motion_detection_set_path(True, 10, params=params)


def test_motion_builder_refuses_incomplete_replace_style_profile():
    with pytest.raises(DetectionConfigurationError, match="wire-named alarm profile"):
        build_motion_detection_set_path(False, 7, params={"alarm_audio": 1})


@pytest.mark.parametrize("cloud_duration", [None, -1])
def test_motion_builder_omits_absent_or_negative_sentinel_cloud_duration(cloud_duration):
    params = _complete_alarm_params()
    if cloud_duration is None:
        params.pop("CloudVideoDuration")
    else:
        params["CloudVideoDuration"] = cloud_duration

    query = dict(
        parse_qsl(urlsplit(build_motion_detection_set_path(False, 7, params=params)).query)
    )

    assert "CloudVideoDuration" not in query


@pytest.mark.parametrize("cloud_duration", [True, "20"])
def test_motion_builder_rejects_non_integer_cloud_duration(cloud_duration):
    params = _complete_alarm_params()
    params["CloudVideoDuration"] = cloud_duration

    with pytest.raises(DetectionConfigurationError, match="non-integer CloudVideoDuration"):
        build_motion_detection_set_path(False, 7, params=params)


def test_human_detection_builders_use_expected_shapes():
    assert build_human_detection_set_path(True, 2, 3) == (
        "/trans_cmd_string.cgi?cmd=2106&command=4&humanDetection=2"
        "&DistanceAdjust=3&HumanoidDetection=1"
    )
    assert build_human_sensitivity_set_path(1) == (
        "/trans_cmd_string.cgi?cmd=2126&command=0&sensitive=1"
    )
    assert build_human_sensitivity_set_path(0).endswith("sensitive=0")
    assert build_human_frame_set_path(False) == (
        "/trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=0"
    )
    assert build_human_tracking_set_path(True) == (
        "/trans_cmd_string.cgi?cmd=2127&command=0&enable=1"
    )
    assert build_human_zoom_tracking_set_path(True) == (
        "/trans_cmd_string.cgi?cmd=2126&command=0&humanoid_zoom=1"
    )


def test_confirmed_human_write_acknowledgements_require_exact_echoes():
    assert parse_human_sensitivity_set_response({"result": "0", "sensitive": "1"}, 1) == {
        "result": 0,
        "sensitive": 1,
    }
    assert parse_human_frame_set_response({"result": 0, "bHumanoidFrame": 1}, True) == {
        "result": 0,
        "bHumanoidFrame": 1,
    }

    for payload in ({}, {"result": 1, "sensitive": 1}, {"result": 0, "sensitive": 0}):
        with pytest.raises(DetectionConfigurationError):
            parse_human_sensitivity_set_response(payload, 1)
    for payload in (
        {},
        {"result": 1, "bHumanoidFrame": 1},
        {"result": 0, "bHumanoidFrame": 0},
        {"result": 0, "bHumanoidFrame": True},
        {"result": 0, "bHumanoidFrame": 1.0},
    ):
        with pytest.raises(DetectionConfigurationError):
            parse_human_frame_set_response(payload, True)


@pytest.mark.parametrize("sensitivity,distance", [(0, 1), (4, 1), (1, 0), (1, 4)])
def test_human_detection_rejects_out_of_range_levels(sensitivity, distance):
    with pytest.raises(DetectionConfigurationError, match="1 to 3"):
        build_human_detection_set_path(True, sensitivity, distance)


def test_human_status_combines_2126_and_2127_without_raw_fields():
    status = parse_human_detection_status(
        {
            "HumanoidDetection": 1,
            "sensitive": 2,
            "bHumanoidFrame": 1,
            "humanoid_zoom": 0,
            "vendor_extra": "ignored",
        },
        tracking_payload={"enable": 1, "result": 0},
    )
    assert status == {
        "available": True,
        "enabled": True,
        "sensitivity": 2,
        "sensitivity_scale": "device-specific 0 to 3",
        "frame_enabled": True,
        "zoom_tracking_setting_enabled": False,
        "tracking_setting_enabled": True,
    }


@pytest.mark.parametrize(
    ("payload", "tracking_payload"),
    [
        ({"result": 1, "sensitive": 1}, None),
        ({"cmd": 2127, "sensitive": 1}, None),
        ({"command": 0, "sensitive": 1}, None),
        ({"sensitive": 1}, {"cmd": 2126, "enable": 1}),
        ({"sensitive": 1}, {"command": 0, "enable": 1}),
    ],
)
def test_human_status_rejects_contradictory_response_markers(payload, tracking_payload):
    with pytest.raises(DetectionConfigurationError):
        parse_human_detection_status(payload, tracking_payload=tracking_payload)


def test_human_2126_status_does_not_invent_main_2106_inverse_fields():
    assert parse_human_detection_status({"humanDetection": 2, "DistanceAdjust": 3}) == {
        "available": False
    }
