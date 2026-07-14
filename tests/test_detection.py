from __future__ import annotations

import pytest

from vstarcamctl.detection import (
    build_human_detection_set_path,
    build_human_frame_set_path,
    build_human_sensitivity_set_path,
    build_human_tracking_set_path,
    build_human_zoom_tracking_set_path,
    build_motion_detection_set_path,
    parse_human_detection_status,
    parse_motion_detection_status,
)
from vstarcamctl.errors import DetectionConfigurationError


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


def test_motion_builder_preserves_alarm_audio_and_validates_scale():
    assert build_motion_detection_set_path(False, 7, alarm_audio_enabled=True) == (
        "/set_alarm.cgi?enable_alarm_audio=1&motion_armed=0&motion_sensitivity=7"
    )
    with pytest.raises(DetectionConfigurationError, match="0 to 9"):
        build_motion_detection_set_path(True, 10, alarm_audio_enabled=False)


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


@pytest.mark.parametrize("sensitivity,distance", [(0, 1), (4, 1), (1, 0), (1, 4)])
def test_human_detection_rejects_out_of_range_levels(sensitivity, distance):
    with pytest.raises(DetectionConfigurationError, match="1 to 3"):
        build_human_detection_set_path(True, sensitivity, distance)


def test_human_status_combines_2126_and_2127_without_raw_fields():
    status = parse_human_detection_status(
        {
            "HumanoidDetection": 1,
            "humanDetection": 2,
            "DistanceAdjust": 3,
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
        "distance": 3,
        "frame_enabled": True,
        "zoom_tracking_enabled": False,
        "tracking_enabled": True,
    }
