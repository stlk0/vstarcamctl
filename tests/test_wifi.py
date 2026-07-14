from __future__ import annotations

import pytest

from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    WifiChangeUncertainError,
    WifiConfigurationError,
)
from vstarcamctl.parser import parse_vstarcam_response
from vstarcamctl.transport import FakeTransport
from vstarcamctl.wifi import (
    build_wifi_set_path,
    extract_wifi_status,
    normalize_wifi_scan,
)


def config(**overrides):
    values = {
        "host": "192.0.2.10",
        "vuid": "VE123456",
        "username": "admin",
        "password": "secret",
        "transport": "fake",
        "retries": 3,
    }
    values.update(overrides)
    return VStarcamConfig(**values)


SCAN_RESPONSE = """
var ap_ssid=new Array();
var ap_bssid=new Array();
var ap_channel=new Array();
var ap_security=new Array();
ap_ssid[0]='first network';
ap_bssid[0]='001122334455';
ap_channel[0]=6;
ap_security[0]=4;
ap_ssid[1]='target network';
ap_bssid[1]='aabbccddeeff';
ap_channel[1]=11;
ap_security[1]=5;
"""


def test_parser_and_normalizer_support_wifi_scan_arrays():
    parsed = parse_vstarcam_response(SCAN_RESPONSE)
    assert parsed["ap_ssid"] == ["first network", "target network"]
    assert parsed["ap_channel"] == [6, 11]
    assert normalize_wifi_scan(parsed) == [
        {
            "index": 0,
            "ssid": "first network",
            "bssid": "001122334455",
            "channel": 6,
            "auth_type": 4,
        },
        {
            "index": 1,
            "ssid": "target network",
            "bssid": "aabbccddeeff",
            "channel": 11,
            "auth_type": 5,
        },
    ]


def test_wifi_status_returns_only_wifi_fields():
    assert extract_wifi_status(
        {
            "alias": "camera",
            "wifi_enable": 1,
            "wifi_ssid": "private network",
            "wifi_channel": 11,
            "wifi_encrypt": 4,
            "wifi_authtype": 5,
        }
    ) == {
        "available": True,
        "enabled": 1,
        "ssid": "private network",
        "channel": 11,
        "encryption": 4,
        "auth_type": 5,
    }


@pytest.mark.parametrize(
    ("ssid", "password", "message"),
    [
        ("", "valid-passphrase", "SSID cannot be empty"),
        ("x" * 33, "valid-passphrase", "at most 32"),
        ("network", "short", "8-63"),
    ],
)
def test_wifi_path_rejects_unsafe_credentials(ssid, password, message):
    with pytest.raises(WifiConfigurationError, match=message):
        build_wifi_set_path(ssid, password, 6, 4)


def test_wifi_path_urlencodes_credentials():
    assert build_wifi_set_path("network name", "correct+horse", 6, 4) == (
        "/set_wifi.cgi?ssid=network+name&channel=6&authtype=4&wpa_psk=correct%2Bhorse&enable=1"
    )


async def test_scan_wifi_uses_expected_endpoint_and_normalizes_response():
    transport = FakeTransport([SCAN_RESPONSE])
    camera = VStarcamCamera(config(), transport=transport)
    result = await camera.scan_wifi(experimental=True)
    assert result["networks"][1]["channel"] == 11
    assert transport.requests == ["GET /wifi_scan.cgi?loginuse=admin&user=admin&pwd=secret&"]


async def test_wifi_write_requires_all_three_safety_gates_before_connecting():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.set_wifi("network", "valid-passphrase", channel=6, auth_type=4)
    with pytest.raises(ConfirmationRequiredError, match="--confirm"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
        )
    with pytest.raises(ConfirmationRequiredError, match="--recovery-ready"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
        )

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_raw_wifi_write_cannot_bypass_recovery_or_one_shot_policy():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    path = build_wifi_set_path("network", "valid-passphrase", 6, 4)

    with pytest.raises(ConfirmationRequiredError, match="guarded wifi set"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)
    with pytest.raises(ConfirmationRequiredError, match="retries are forbidden"):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_wifi_write_is_sent_once_and_closes_the_old_session():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.set_wifi(
        "network name",
        "correct+horse",
        channel=6,
        auth_type=4,
        experimental=True,
        confirm=True,
        recovery_ready=True,
    )

    assert result == {"result": 0}
    assert transport.requests == [
        "GET /set_wifi.cgi?ssid=network+name&channel=6&authtype=4"
        "&wpa_psk=correct%2Bhorse&enable=1&loginuse=admin&user=admin&pwd=secret&"
    ]
    assert transport.close_count == 1
    assert not transport.connected


async def test_wifi_write_never_retries_when_acknowledgement_is_lost():
    transport = FakeTransport([TimeoutError("session disappeared")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(WifiChangeUncertainError, match="outcome is unknown"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 1
    assert transport.connect_count == 1
    assert transport.close_count == 1


async def test_wifi_write_can_fill_channel_and_auth_type_from_exact_scan_match():
    transport = FakeTransport([SCAN_RESPONSE, "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_wifi(
        "target network",
        "valid-passphrase",
        experimental=True,
        confirm=True,
        recovery_ready=True,
    ) == {"result": 0}

    assert len(transport.requests) == 2
    assert "channel=11&authtype=5" in transport.requests[1]


async def test_wifi_scan_requires_experimental_before_connecting():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    with pytest.raises(ExperimentalCommandError, match="wifi_scan"):
        await camera.scan_wifi()
    assert transport.connect_count == 0
