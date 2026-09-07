from __future__ import annotations

import asyncio

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    RawCommandError,
    TransportCommandCancelledError,
    WifiChangeCancelledError,
    WifiChangeUncertainError,
    WifiConfigurationError,
)
from vstarcamctl.parser import parse_vstarcam_response
from vstarcamctl.wifi import (
    build_wifi_set_path,
    extract_wifi_status,
    normalize_wifi_scan,
    parse_wifi_scan_response,
)


def wifi_config(**overrides):
    overrides.setdefault("account_id", "test-account")
    return config(**overrides)


SCAN_RESPONSE = """
var result=0;
var ap_number=2;
var ap_ssid=new Array();
var ap_mac=new Array();
var ap_channel=new Array();
var ap_security=new Array();
var ap_mode=new Array();
var ap_dbm0=new Array();
var ap_dbm1=new Array();
ap_ssid[0]='first network';
ap_mac[0]='001122334455';
ap_channel[0]=6;
ap_security[0]=4;
ap_mode[0]=0;
ap_dbm0[0]=-42;
ap_dbm1[0]=-43;
ap_ssid[1]='target network';
ap_mac[1]='aabbccddeeff';
ap_channel[1]=11;
ap_security[1]=5;
ap_mode[1]=1;
ap_dbm0[1]=-51;
ap_dbm1[1]=-53;
"""


def _single_scan_response(ssid: str, channel: int = 6, auth_type: int = 4) -> str:
    return f"""
var result=0;
var ap_number=1;
var ap_ssid=new Array();
var ap_channel=new Array();
var ap_security=new Array();
ap_ssid[0]='{ssid}';
ap_channel[0]={channel};
ap_security[0]={auth_type};
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
            "mode": 0,
            "signal": -42,
            "ap_dbm1": -43,
        },
        {
            "index": 1,
            "ssid": "target network",
            "bssid": "aabbccddeeff",
            "channel": 11,
            "auth_type": 5,
            "mode": 1,
            "signal": -51,
            "ap_dbm1": -53,
        },
    ]
    assert parse_wifi_scan_response(parsed) == normalize_wifi_scan(parsed)


@pytest.mark.parametrize(
    "payload",
    [
        {"result": 1, "ap_number": 0, "ap_ssid": [], "ap_channel": [], "ap_security": []},
        {"result": 0, "ap_ssid": [], "ap_channel": [], "ap_security": []},
        {"result": 0, "ap_number": 1, "ap_ssid": [], "ap_channel": [], "ap_security": []},
        {"result": 0, "ap_number": 0, "ap_ssid": "bad", "ap_channel": [], "ap_security": []},
        {"result": 0, "ap_number": 0, "ap_ssid": [], "ap_channel": []},
    ],
)
def test_wifi_scan_rejects_malformed_response(payload):
    with pytest.raises(WifiConfigurationError):
        parse_wifi_scan_response(payload)


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
    camera = VStarcamCamera(wifi_config(), transport=transport)
    result = await camera.scan_wifi()
    assert result["networks"][1]["channel"] == 11
    assert set(result) == {"networks"}
    assert transport.requests == [
        "GET /wifi_scan.cgi?loginuse=admin&userId=test-account"
        "&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]


async def test_wifi_write_requires_all_three_safety_gates_before_connecting():
    transport = FakeTransport()
    camera = VStarcamCamera(wifi_config(), transport=transport)

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
    camera = VStarcamCamera(wifi_config(), transport=transport)
    path = build_wifi_set_path("network", "valid-passphrase", 6, 4)

    with pytest.raises(ExperimentalCommandError, match="guarded high-level API"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)
    with pytest.raises(ExperimentalCommandError, match="guarded high-level API"):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_wifi_write_is_sent_once_and_closes_the_old_session():
    transport = FakeTransport([_single_scan_response("network name"), "var result=0;"])
    camera = VStarcamCamera(wifi_config(), transport=transport)

    with pytest.raises(WifiChangeUncertainError, match="not proven"):
        await camera.set_wifi(
            "network name",
            "correct+horse",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 2
    assert "/wifi_scan.cgi?" in transport.requests[0]
    assert transport.requests[1] == (
        "GET /set_wifi.cgi?ssid=network+name&channel=6&authtype=4"
        "&wpa_psk=correct%2Bhorse&enable=1&userid=test-account&"
        "loginuse=admin&userId=test-account&loginpas=camera-secret"
        "&user=admin&pwd=camera-secret&"
    )
    assert transport.close_count == 1
    assert not transport.connected


async def test_wifi_write_refuses_to_send_without_trusted_account_id():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(account_id=None), transport=transport)

    with pytest.raises(RawCommandError, match="non-empty account_id"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert transport.requests == []
    assert transport.connect_count == 0
    assert transport.close_count == 0


async def test_wifi_write_never_retries_when_acknowledgement_is_lost():
    transport = FakeTransport(
        [_single_scan_response("network"), TimeoutError("session disappeared")]
    )
    camera = VStarcamCamera(wifi_config(retries=5), transport=transport)

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

    assert len(transport.requests) == 2
    assert transport.connect_count == 1
    assert transport.close_count == 1


async def test_wifi_write_can_fill_channel_and_auth_type_from_exact_scan_match():
    transport = FakeTransport([SCAN_RESPONSE, "var result=0;"])
    camera = VStarcamCamera(wifi_config(), transport=transport)

    with pytest.raises(WifiChangeUncertainError, match="not proven"):
        await camera.set_wifi(
            "target network",
            "valid-passphrase",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 2
    assert "channel=11&authtype=5" in transport.requests[1]


@pytest.mark.parametrize("response", ["var result=1;", "var status='ok';", "var broken="])
async def test_wifi_write_never_treats_unproven_or_malformed_ack_as_success(response):
    transport = FakeTransport([_single_scan_response("network"), response])
    camera = VStarcamCamera(wifi_config(), transport=transport)

    with pytest.raises(WifiChangeUncertainError, match="unknown"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 2
    assert transport.close_count == 1


async def test_wifi_cancellation_after_send_remains_cancelled_and_uncertain():
    transport = FakeTransport(
        [
            _single_scan_response("network"),
            TransportCommandCancelledError("cancelled after send"),
        ]
    )
    camera = VStarcamCamera(wifi_config(), transport=transport)

    with pytest.raises(WifiChangeCancelledError, match="outcome is unknown") as caught:
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=4,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert isinstance(caught.value, asyncio.CancelledError)
    assert isinstance(caught.value, WifiChangeUncertainError)
    assert len(transport.requests) == 2


async def test_wifi_scan_and_one_shot_write_are_serialized_per_camera():
    class GatedWifiScanTransport(FakeTransport):
        def __init__(self):
            super().__init__(
                [
                    _single_scan_response("first network"),
                    "var result=0;",
                    _single_scan_response("second network"),
                    "var result=0;",
                ]
            )
            self.first_scan_seen = asyncio.Event()
            self.release_first_scan = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if not self.requests:
                self.requests.append(command)
                response = self.responses.popleft()
                assert isinstance(response, str)
                self.first_scan_seen.set()
                await self.release_first_scan.wait()
                return response
            return await super().request(command, timeout=timeout)

    transport = GatedWifiScanTransport()
    camera = VStarcamCamera(wifi_config(), transport=transport)

    async def change(ssid: str):
        await camera.set_wifi(
            ssid,
            "valid-passphrase",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    first = asyncio.create_task(change("first network"))
    await transport.first_scan_seen.wait()
    second = asyncio.create_task(change("second network"))
    await asyncio.sleep(0)

    assert len(transport.requests) == 1
    transport.release_first_scan.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(outcome, WifiChangeUncertainError) for outcome in outcomes)
    assert "ssid=first+network" in transport.requests[1]
    assert "/wifi_scan.cgi" in transport.requests[2]
    assert "ssid=second+network" in transport.requests[3]


async def test_wifi_write_requires_supplied_metadata_to_match_the_camera_scan():
    transport = FakeTransport([_single_scan_response("network", channel=11, auth_type=5)])
    camera = VStarcamCamera(wifi_config(), transport=transport)

    with pytest.raises(WifiConfigurationError, match="exactly match"):
        await camera.set_wifi(
            "network",
            "valid-passphrase",
            channel=6,
            auth_type=5,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 1
    assert "/wifi_scan.cgi?" in transport.requests[0]
    assert all("set_wifi.cgi" not in request for request in transport.requests)


async def test_wifi_scan_retries_a_read_timeout():
    transport = FakeTransport([TimeoutError("read timed out"), SCAN_RESPONSE])
    camera = VStarcamCamera(wifi_config(retries=1), transport=transport)

    assert len((await camera.scan_wifi())["networks"]) == 2
    assert len(transport.requests) == 2
    assert transport.connect_count == 2
