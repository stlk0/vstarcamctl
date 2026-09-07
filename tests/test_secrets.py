import logging

import pytest

from vstarcamctl.secrets import SecretMaskingFilter, mask_identifier, mask_secrets, redact_data


def test_masks_query_and_mapping_secrets():
    raw = (
        "GET /x.cgi?LOGINUSE=private-user&user=private-user&pwd=hunter2"
        "&loginToken=abc&vuid=VE123456 "
        "var uid=\"VE654321\"; {'password': 'another', 'safe': 'visible'}"
    )
    masked = mask_secrets(raw)
    for secret in ("private-user", "hunter2", "abc", "VE123456", "VE654321", "another"):
        assert secret not in masked
    assert "visible" in masked


def test_recursive_redaction():
    assert redact_data(
        {
            "password": "secret",
            "LoGiNuSe": "private-user",
            "user": "private-user",
            "device_id": "VSTG-000001-AAAAA",
            "nested": {"userId": "123", "ok": 1},
        }
    ) == {
        "password": "***",
        "LoGiNuSe": "***",
        "user": "***",
        "device_id": "***",
        "nested": {"userId": "***", "ok": 1},
    }


def test_masks_unquoted_mapping_values():
    masked = mask_secrets("{'userId': 123, 'password': None}")
    assert "123" not in masked
    assert "None" not in masked


@pytest.mark.parametrize(
    "value",
    [
        "var WebPwd='first;SECOND_PART'; var safe=1;",
        'var wifi_pwd="first&SECOND_PART"; var safe=1;',
        r"""{"password":"first\",SECOND_PART", "safe":1}""",
        r"{'wifi_password': 'first\';SECOND_PART', 'safe':1}",
        "pwd=first%3BSECOND_PART&safe=1",
    ],
)
def test_masks_complete_quoted_and_encoded_secrets(value):
    masked = mask_secrets(value)
    assert "first" not in masked
    assert "SECOND_PART" not in masked
    assert "***" in masked
    assert "safe" in masked


@pytest.mark.parametrize(
    "value",
    [
        "var WebPwd='first;SECOND_PART",
        'var wifi_pwd="first&SECOND_PART',
        '{"password":"first' + "\\",
        "{'wifi_password': 'first\\",
    ],
)
def test_masks_rest_of_unterminated_quoted_secret(value):
    masked = mask_secrets(value)
    assert "first" not in masked
    assert "SECOND_PART" not in masked
    assert "***" in masked


def test_logging_filter_rewrites_formatted_message():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "pwd=%s REALDEVICEID=%s",
        ("secret", "VSTG-000002-BBBBB"),
        None,
    )
    assert SecretMaskingFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "VSTG-000002-BBBBB" not in record.getMessage()


def test_identifier_masking():
    assert mask_identifier("VE123456789") == "VE1***789"


def test_masks_aiopppp_parenthesized_device_identifier():
    masked = mask_secrets("Device DevID(VENDOR-PRIVATE-SERIAL) lost")
    assert masked == "Device DevID(***) lost"
    assert "VENDOR-PRIVATE-SERIAL" not in masked


def test_masks_wifi_and_network_identifiers():
    raw = (
        "GET /set_wifi.cgi?ssid=private-network&wpa_psk=private-pass "
        "GET /set_rtsp.cgi?rtspuser=stream-user&rtsppwd=stream-secret "
        "{'ap_ssid': 'private-network', 'ap_bssid': '001122334455', "
        "'ap_mac': 'ffeeddccbbaa', "
        "'mac': 'aabbccddeeff'}"
    )
    masked = mask_secrets(raw)
    for secret in (
        "private-network",
        "private-pass",
        "stream-user",
        "stream-secret",
        "001122334455",
        "ffeeddccbbaa",
        "aabbccddeeff",
    ):
        assert secret not in masked
    assert redact_data({"ssid": "private-network", "bssid": "001122334455"}) == {
        "ssid": "***",
        "bssid": "***",
    }


def test_masks_known_params_aliases_ip_fields_and_values_with_spaces():
    payload = {
        "WebPwd": "web-secret",
        "user3_pwd": "slot-secret",
        "loginAccount": "account-id",
        "wlan_ssid": "private network",
        "source_ip": "203.0.113.10",
        "destination_ip": "198.51.100.7",
        "ReAlDeViCeId": "VSTG-000002-BBBBB",
    }
    assert redact_data(payload) == {key: "***" for key in payload}
    masked = mask_secrets("pwd=two words&ssid=private network&realdeviceid=VSTG-000002-BBBBB&")
    assert "two words" not in masked
    assert "private network" not in masked
    assert "VSTG-000002-BBBBB" not in masked
