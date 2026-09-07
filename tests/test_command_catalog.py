from dataclasses import asdict, replace
from pathlib import Path

import pytest

from vstarcamctl.command_catalog import CommandCatalog
from vstarcamctl.errors import (
    CatalogError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "src" / "vstarcamctl" / "data" / "catalog.yaml"
CATALOG = CommandCatalog.load(CATALOG_PATH)


@pytest.mark.parametrize(
    ("name", "risk", "status"),
    [
        ("login_status", "read_only", "confirmed"),
        ("siren_off", "safe_write", "confirmed"),
        ("datetime_set", "safe_write", "confirmed"),
        ("night_vision_color_set", "safe_write", "observed_unconfirmed"),
        ("infrared_light_set", "safe_write", "observed_unconfirmed"),
        ("camera_account_password_set", "risky_write", "observed_unconfirmed"),
        ("camera_owner_password_set", "risky_write", "observed_unconfirmed"),
        ("camera_reboot", "risky_write", "observed_unconfirmed"),
        ("rtsp_set", "risky_write", "observed_unconfirmed"),
        ("record_audio_set", "risky_write", "hypothesis"),
        ("microphone_volume_set", "safe_write", "confirmed"),
        ("audio_talk_start", "risky_write", "observed_unconfirmed"),
        ("audio_talk_adpcm_channel", "risky_write", "failed"),
        ("pppp_livestream_start", "safe_write", "observed_unconfirmed"),
        ("pppp_livestream_stop", "safe_write", "observed_unconfirmed"),
        ("audio_talk_adpcm_with_livestream", "risky_write", "hypothesis"),
        ("motion_detection_set", "risky_write", "hypothesis"),
        ("motion_regions_status", "read_only", "confirmed"),
        ("human_detection_set", "risky_write", "hypothesis"),
        ("human_sensitivity_set", "safe_write", "confirmed"),
        ("human_frame_set", "safe_write", "confirmed"),
        ("human_tracking_set", "risky_write", "observed_unconfirmed"),
        ("ptz_up_start", "risky_write", "hypothesis"),
        ("ptz_up_stop", "safe_write", "hypothesis"),
        ("osd_12h_status", "read_only", "confirmed"),
        ("osd_12h_set", "safe_write", "confirmed"),
        ("actuator_status", "read_only", "confirmed"),
        ("alarm_led_set", "safe_write", "observed_unconfirmed"),
        ("logo_osd_set", "safe_write", "hypothesis"),
        ("wifi_scan", "read_only", "confirmed"),
        ("wifi_set", "risky_write", "hypothesis"),
        ("trans_cmd_4120", "unknown", "observed_unconfirmed"),
    ],
)
def test_catalog_safety_matrix(name, risk, status):
    command = CATALOG.get(name)
    assert (command.risk_level, command.confirmation_status) == (risk, status)


@pytest.mark.parametrize(
    ("name", "experimental", "confirm", "error"),
    [
        ("siren_off", False, False, None),
        ("wifi_scan", False, False, None),
        ("trans_cmd_4120", False, False, ExperimentalCommandError),
        ("trans_cmd_4120", True, False, ConfirmationRequiredError),
        ("trans_cmd_4120", True, True, None),
        ("onvif_set", True, False, ConfirmationRequiredError),
        ("onvif_set", True, True, None),
        ("microphone_volume_set", False, False, None),
        ("speaker_volume_set", False, False, ExperimentalCommandError),
        ("speaker_volume_set", True, False, None),
        ("camera_owner_password_set", True, True, None),
        ("audio_talk_adpcm_channel", True, True, ExperimentalCommandError),
    ],
)
def test_permission_matrix(name, experimental, confirm, error):
    command = CATALOG.get(name)
    if error is None:
        command.require_permission(experimental=experimental, confirm=confirm)
    else:
        with pytest.raises(error):
            command.require_permission(experimental=experimental, confirm=confirm)


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/get_status.cgi", "status"),
        ("/get_status.cgi?vuid=placeholder", "status"),
        ("/get_status.cgi?name=admin", "login_status"),
        ("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1", "siren_on"),
        ("/camera_control.cgi?param=33&value=2", "night_vision_color_set"),
        ("/camera_control.cgi?param=14&value=1", "night_vision_low_light_set"),
        ("/trans_cmd_string.cgi?cmd=2120&command=0&InfraredLaser=1", "infrared_light_set"),
        (
            "/set_datetime.cgi?tz=-19800&ntp_enable=1&ntp_svr=time.windows.com&now=1700000000",
            "datetime_set",
        ),
        ("/camera_control.cgi?param=24&value=12", "microphone_volume_set"),
        ("/camera_control.cgi?param=25&value=23", "speaker_volume_set"),
        ("/audiostream.cgi?streamid=7", "audio_talk_start"),
        ("/audiostream.cgi?streamid=16", "audio_talk_stop"),
        ("/livestream.cgi?streamid=10&substream=0", "pppp_livestream_start"),
        ("/livestream.cgi?streamid=10&substream=1", "pppp_livestream_start"),
        ("/livestream.cgi?streamid=16&substream=0", "pppp_livestream_stop"),
        (
            "/trans_cmd_string.cgi?cmd=2106&command=4&humanDetection=2"
            "&DistanceAdjust=3&HumanoidDetection=1",
            "human_detection_set",
        ),
        (
            "/trans_cmd_string.cgi?cmd=2123&command=1&sensor=0",
            "motion_regions_status",
        ),
        ("/trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=1", "human_frame_set"),
        ("/decoder_control.cgi?command=0&onestep=0", "ptz_up_start"),
        ("/decoder_control.cgi?command=1&onestep=0", "ptz_up_stop"),
        ("/decoder_control.cgi?command=2&onestep=0", "ptz_down_start"),
        ("/decoder_control.cgi?command=3&onestep=0", "ptz_down_stop"),
        ("/decoder_control.cgi?command=4&onestep=0", "ptz_left_start"),
        ("/decoder_control.cgi?command=5&onestep=0", "ptz_left_stop"),
        ("/decoder_control.cgi?command=6&onestep=0", "ptz_right_start"),
        ("/decoder_control.cgi?command=7&onestep=0", "ptz_right_stop"),
        ("/trans_cmd_string.cgi?cmd=4109&command=0", "osd_12h_status"),
        (
            "/trans_cmd_string.cgi?cmd=4109&command=1&osd_12h_mode=1",
            "osd_12h_set",
        ),
        ("/trans_cmd_string.cgi?cmd=2109&command=2", "actuator_status"),
        (
            "/trans_cmd_string.cgi?cmd=2109&command=0&alarmLed=1",
            "alarm_led_set",
        ),
        ("/camera_control.cgi?param=11&value=1", "logo_osd_set"),
        ("/set_wifi.cgi?ssid=x&channel=6&authtype=4&wpa_psk=y&enable=1", "wifi_set"),
    ],
)
def test_catalog_matches_exact_query_shapes(path, name):
    assert CATALOG.match_path(path).name == name


def test_default_load_uses_packaged_catalog():
    assert CommandCatalog.load().commands == CATALOG.commands


def test_white_light_uses_physically_confirmed_app_command():
    assert CATALOG.get("white_light_on").path == (
        "/trans_cmd_string.cgi?cmd=2109&command=0&light=1"
    )
    assert CATALOG.get("white_light_off").path == (
        "/trans_cmd_string.cgi?cmd=2109&command=0&light=0"
    )


def test_catalog_rejects_extra_and_duplicate_query_fields():
    with pytest.raises(CatalogError, match="known write"):
        CATALOG.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1&light=1")
    with pytest.raises(CatalogError, match="extra fields"):
        CATALOG.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1&vendor_extra=1")
    with pytest.raises(CatalogError, match="duplicate query"):
        CATALOG.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1&siren=0")


def test_known_protected_write_with_extra_field_fails_closed():
    with pytest.raises(CatalogError, match="extra fields"):
        CATALOG.match_path(
            "/set_wifi.cgi?ssid=x&channel=6&authtype=4&wpa_psk=y&enable=1&vendor_extra=1"
        )


def test_unrecognized_shape_cannot_bypass_protected_endpoint_policy():
    with pytest.raises(CatalogError, match="protected write shape"):
        CATALOG.match_path("/set_users.cgi?ignored=1")


def test_partial_known_write_never_inherits_confirmed_policy():
    with pytest.raises(CatalogError, match="incomplete known write shape"):
        CATALOG.match_path("/trans_cmd_string.cgi?light=1")


def test_catalog_keeps_capability_gates_independent_from_confirmation():
    frame = CATALOG.get("human_frame_set")
    assert frame.confirmation_status == "confirmed"
    assert frame.capability_flags == ("support_humanoidFrame",)

    tracking = CATALOG.get("human_tracking_set")
    assert tracking.confirmation_status == "observed_unconfirmed"
    assert tracking.capability_flags == ("haveMotor",)

    ptz_start = CATALOG.get("ptz_left_start")
    assert ptz_start.capability_flags == ("haveMotor",)
    assert CATALOG.get("ptz_left_stop").capability_flags == ()

    osd = CATALOG.get("osd_12h_set")
    assert osd.capability_flags == ("12h_mode_support",)

    logo = CATALOG.get("logo_osd_set")
    assert logo.capability_flags == ("support_custom_logo_show",)

    assert CATALOG.get("alarm_led_set").capability_flags == ()
    assert CATALOG.get("actuator_status").capability_flags == ()


def test_catalog_has_one_shared_actuator_status_path():
    path = "/trans_cmd_string.cgi?cmd=2109&command=2"
    matches = [command for command in CATALOG.commands if command.path == path]
    assert [command.name for command in matches] == ["actuator_status"]


def test_catalog_marks_sensitive_or_guarded_paths_raw_inaccessible():
    assert {command.name for command in CATALOG.commands if not command.raw_accessible} == {
        "audio_talk_start",
        "camera_account_password_set",
        "camera_owner_password_set",
        "camera_reboot",
        "login_status",
        "motion_regions_status",
        "pppp_livestream_start",
        "ptz_down_start",
        "ptz_left_start",
        "ptz_right_start",
        "ptz_up_start",
        "wifi_set",
    }


def test_catalog_rejects_non_boolean_raw_access_policy():
    item = asdict(CATALOG.get("status"))
    item["raw_accessible"] = "false"
    with pytest.raises(CatalogError, match="raw_accessible must be boolean"):
        CommandCatalog._validate_item(item, 0)


def test_catalog_rejects_ambiguous_matches():
    command = CATALOG.get("siren_on")
    ambiguous = CommandCatalog((command, replace(command, name="siren_alias")))
    with pytest.raises(CatalogError, match="ambiguously matches multiple"):
        ambiguous.match_path(command.path)


def test_unknown_risk_must_be_one_shot():
    command = replace(CATALOG.get("trans_cmd_4120"), retry_policy="retryable")
    with pytest.raises(CatalogError, match="unknown-risk commands must be one_shot"):
        CommandCatalog((command,))


def test_dangerous_match_takes_precedence_even_with_extra_fields():
    path = "/set_users.cgi?pwd_change_realtime=1&user3=x&pwd3=y&extra=1"
    assert CATALOG.match_path(path).name == "camera_user3_password_set"


def test_one_shot_and_recovery_policies():
    one_shot = {
        "datetime_set",
        "siren_on",
        "siren_off",
        "white_light_on",
        "white_light_off",
        "night_vision_color_set",
        "night_vision_low_light_set",
        "infrared_light_set",
        "camera_account_password_set",
        "camera_owner_password_set",
        "camera_reboot",
        "rtsp_set",
        "onvif_set",
        "microphone_volume_set",
        "speaker_volume_set",
        "audio_talk_start",
        "audio_talk_stop",
        "audio_talk_adpcm_channel",
        "pppp_livestream_start",
        "pppp_livestream_stop",
        "audio_talk_adpcm_with_livestream",
        "record_audio_set",
        "motion_detection_set",
        "human_detection_set",
        "human_sensitivity_set",
        "human_frame_set",
        "human_tracking_set",
        "human_zoom_tracking_set",
        "ptz_up_start",
        "ptz_up_stop",
        "ptz_down_start",
        "ptz_down_stop",
        "ptz_left_start",
        "ptz_left_stop",
        "ptz_right_start",
        "ptz_right_stop",
        "osd_12h_set",
        "alarm_led_set",
        "logo_osd_set",
        "wifi_set",
        "trans_cmd_4120",
        "trans_cmd_8000",
    }
    assert {
        command.name for command in CATALOG.commands if command.retry_policy == "one_shot"
    } == one_shot
    assert {command.name for command in CATALOG.commands if command.recovery_required} == {
        "camera_account_password_set",
        "camera_owner_password_set",
        "camera_reboot",
        "wifi_set",
    }


def test_infrared_applicability_has_no_guessed_capability_gate():
    assert CATALOG.get("infrared_light_set").capability_flags == ()
