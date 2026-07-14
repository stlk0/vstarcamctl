from pathlib import Path

import pytest

from vstarcamctl.command_catalog import CommandCatalog
from vstarcamctl.errors import CatalogError, ExperimentalCommandError

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "src" / "vstarcamctl" / "data" / "catalog.yaml"


def test_catalog_validates_and_matches_specific_command():
    catalog = CommandCatalog.load(CATALOG_PATH)
    command = catalog.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1")
    assert command is not None
    assert command.name == "siren_on"
    assert command.category == "siren"


def test_default_load_uses_the_packaged_catalog():
    by_path = CommandCatalog.load(CATALOG_PATH)
    packaged = CommandCatalog.load()
    assert by_path.commands == packaged.commands


def test_white_light_uses_physically_confirmed_app_command():
    catalog = CommandCatalog.load()
    assert catalog.get("white_light_on").path == (
        "/trans_cmd_string.cgi?cmd=2109&command=0&light=1"
    )
    assert catalog.get("white_light_off").path == (
        "/trans_cmd_string.cgi?cmd=2109&command=0&light=0"
    )


def test_live_confirmed_write_does_not_require_experimental_flag():
    command = CommandCatalog.load().get("siren_off")
    command.require_permission(experimental=False, confirm=False)


def test_unconfirmed_write_requires_experimental_flag():
    command = CommandCatalog.load().get("trans_cmd_4120")
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        command.require_permission(experimental=False, confirm=False)
    command.require_permission(experimental=True, confirm=False)


def test_wifi_catalog_entries_remain_unconfirmed_and_risky():
    catalog = CommandCatalog.load()
    scan = catalog.get("wifi_scan")
    wifi_set = catalog.get("wifi_set")
    assert scan.read_or_write == "read"
    assert scan.tested_on_device is False
    assert wifi_set.risk_level == "risky_write"
    assert wifi_set.confirmation_status == "hypothesis"
    assert wifi_set.retry_policy == "one_shot"
    assert wifi_set.recovery_required is True
    assert (
        catalog.match_path("/set_wifi.cgi?ssid=x&channel=6&authtype=4&wpa_psk=y&enable=1")
        == wifi_set
    )


def test_catalog_matches_exact_query_shape_and_rejects_ambiguity():
    catalog = CommandCatalog.load()
    assert catalog.match_path("/get_status.cgi").name == "status"
    assert catalog.match_path("/get_status.cgi?vuid=placeholder").name == "status"
    assert (
        catalog.match_path("/set_users.cgi?pwd_change_realtime=1&OwnerUser=x&OwnerPwd=y").name
        == "camera_owner_password_set"
    )
    assert (
        catalog.match_path("/set_users.cgi?pwd_change_realtime=1&user3=x&pwd3=y").name
        == "camera_user3_password_set"
    )
    assert catalog.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1&light=1") is None
    with pytest.raises(CatalogError, match="duplicate query"):
        catalog.match_path("/trans_cmd_string.cgi?cmd=2109&command=0&siren=1&siren=0")


def test_catalog_carries_transport_policy_for_risky_writes():
    catalog = CommandCatalog.load()
    for name in ("rtsp_set", "onvif_set", "record_audio_set"):
        command = catalog.get(name)
        assert command.retry_policy == "one_shot"
        assert command.recovery_required is False

    password = catalog.get("camera_account_password_set")
    assert password.retry_policy == "one_shot"
    assert password.recovery_required is True


def test_detection_catalog_entries_are_guarded_and_match_exact_shapes():
    catalog = CommandCatalog.load()
    for name in (
        "motion_detection_set",
        "human_detection_set",
        "human_tracking_set",
        "human_zoom_tracking_set",
    ):
        command = catalog.get(name)
        assert command.risk_level == "risky_write"
        assert command.retry_policy == "one_shot"

    for name in ("motion_detection_set",):
        assert catalog.get(name).confirmation_status == "observed_unconfirmed"
        assert catalog.get(name).tested_on_device is True

    for name in ("human_detection_set", "human_zoom_tracking_set"):
        assert catalog.get(name).confirmation_status == "hypothesis"
        assert catalog.get(name).tested_on_device is False

    for name in ("human_detection_status", "human_tracking_status"):
        assert catalog.get(name).confirmation_status == "confirmed"
        assert catalog.get(name).tested_on_device is True

    frame = catalog.get("human_frame_set")
    assert frame.confirmation_status == "confirmed"
    assert frame.risk_level == "safe_write"
    assert frame.tested_on_device is True

    sensitivity = catalog.get("human_sensitivity_set")
    assert sensitivity.confirmation_status == "confirmed"
    assert sensitivity.risk_level == "safe_write"
    assert sensitivity.tested_on_device is True

    tracking = catalog.get("human_tracking_set")
    assert tracking.confirmation_status == "failed"
    assert tracking.tested_on_device is True

    assert (
        catalog.match_path(
            "/trans_cmd_string.cgi?cmd=2106&command=4&humanDetection=2"
            "&DistanceAdjust=3&HumanoidDetection=1"
        ).name
        == "human_detection_set"
    )
    assert (
        catalog.match_path("/trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=1").name
        == "human_frame_set"
    )


def test_night_vision_and_ir_catalog_entries_are_guarded_one_shot_candidates():
    catalog = CommandCatalog.load()
    for name in ("night_vision_status", "infrared_light_status"):
        command = catalog.get(name)
        assert command.risk_level == "read_only"
        assert command.confirmation_status == "confirmed"
        assert command.tested_on_device is True
    for name in (
        "night_vision_color_set",
        "night_vision_low_light_set",
        "infrared_light_set",
    ):
        command = catalog.get(name)
        assert command.risk_level == "safe_write"
        assert command.confirmation_status == "observed_unconfirmed"
        assert command.retry_policy == "one_shot"
        assert command.tested_on_device is True

    assert (
        catalog.match_path("/camera_control.cgi?param=33&value=2").name == "night_vision_color_set"
    )
    assert (
        catalog.match_path("/camera_control.cgi?param=14&value=1").name
        == "night_vision_low_light_set"
    )
    assert (
        catalog.match_path("/trans_cmd_string.cgi?cmd=2120&command=0&InfraredLaser=1").name
        == "infrared_light_set"
    )


def test_datetime_catalog_entry_is_a_confirmed_one_shot_safe_write():
    catalog = CommandCatalog.load()
    command = catalog.get("datetime_set")
    assert command.risk_level == "safe_write"
    assert command.confirmation_status == "confirmed"
    assert command.retry_policy == "one_shot"
    assert command.tested_on_device is True
    assert (
        catalog.match_path(
            "/set_datetime.cgi?tz=-19800&ntp_enable=1&ntp_svr=time.windows.com&now=1700000000"
        )
        == command
    )


def test_audio_volume_and_talk_candidates_are_guarded_and_match_exact_paths():
    catalog = CommandCatalog.load()
    for name in ("microphone_volume_set", "speaker_volume_set"):
        command = catalog.get(name)
        assert command.risk_level == "safe_write"
        assert command.confirmation_status == "observed_unconfirmed"
        assert command.retry_policy == "one_shot"
        assert command.tested_on_device is True
    for name in ("audio_talk_start", "audio_talk_stop"):
        command = catalog.get(name)
        assert command.risk_level == "risky_write"
        assert command.confirmation_status == "observed_unconfirmed"
        assert command.retry_policy == "one_shot"
        assert command.tested_on_device is True
    assert (
        catalog.match_path("/camera_control.cgi?param=24&value=12").name == "microphone_volume_set"
    )
    assert catalog.match_path("/camera_control.cgi?param=25&value=23").name == "speaker_volume_set"
    assert catalog.match_path("/audiostream.cgi?streamid=7").name == "audio_talk_start"
    assert catalog.match_path("/audiostream.cgi?streamid=16").name == "audio_talk_stop"
    direct = catalog.get("audio_talk_adpcm_channel")
    assert direct.path == "pppp://channel/3?codec=ima_adpcm"
    assert direct.risk_level == "risky_write"
    assert direct.confirmation_status == "failed"
    assert direct.tested_on_device is True
    for name in (
        "pppp_livestream_start",
        "pppp_livestream_stop",
        "audio_talk_adpcm_with_livestream",
    ):
        command = catalog.get(name)
        assert command.confirmation_status == "hypothesis"
        assert command.retry_policy == "one_shot"
        assert command.tested_on_device is False
    assert (
        catalog.match_path("/livestream.cgi?streamid=10&substream=0").name
        == "pppp_livestream_start"
    )
    assert (
        catalog.match_path("/livestream.cgi?streamid=10&substream=1").name
        == "pppp_livestream_start"
    )
    assert (
        catalog.match_path("/livestream.cgi?streamid=16&substream=0").name == "pppp_livestream_stop"
    )
