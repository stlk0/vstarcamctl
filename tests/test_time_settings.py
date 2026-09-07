from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers import FakeTransport, camera_config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import TimeConfigurationError
from vstarcamctl.time_settings import (
    build_time_settings_set_path,
    format_utc_offset,
    parse_time_settings,
    parse_time_settings_set_response,
    parse_utc_offset,
    validate_ntp_server,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("UTC", 0),
        ("Z", 0),
        ("+05:30", 19800),
        ("UTC-05:30", -19800),
        ("gmt+1245", 45900),
        ("UTC+14:00", 50400),
    ],
)
def test_fixed_timezone_offsets_are_parsed_and_formatted(value, expected):
    assert parse_utc_offset(value) == expected
    assert parse_utc_offset(format_utc_offset(expected)) == expected


@pytest.mark.parametrize("value", ["Europe/Berlin", "+15:00", "+12:60", "2", "+01:00:00"])
def test_timezone_parser_rejects_unsupported_or_out_of_range_values(value):
    with pytest.raises(TimeConfigurationError):
        parse_utc_offset(value)


def test_datetime_builder_uses_vendor_timezone_sign_and_unix_seconds():
    assert build_time_settings_set_path(
        19800,
        True,
        "time.windows.com",
        1_700_000_000,
    ) == ("/set_datetime.cgi?tz=-19800&ntp_enable=1&ntp_svr=time.windows.com&now=1700000000")


def test_datetime_builder_validates_ntp_and_legacy_timestamp_limits():
    with pytest.raises(TimeConfigurationError, match="required"):
        build_time_settings_set_path(0, True, "", 1)
    with pytest.raises(TimeConfigurationError, match="2147483647"):
        build_time_settings_set_path(0, False, "", 2_147_483_648)
    with pytest.raises(TimeConfigurationError, match="hostname or IP"):
        validate_ntp_server("https://time.example", enabled=True)


def test_datetime_acknowledgement_requires_exact_ok_result():
    assert parse_time_settings_set_response({"result": "ok", "vendor": "ignored"}) == {
        "result": "ok"
    }
    for payload in ({}, {"result": 0}, {"result": "OK"}):
        with pytest.raises(TimeConfigurationError):
            parse_time_settings_set_response(payload)


def test_time_status_is_normalized_and_vendor_extras_are_ignored():
    assert parse_time_settings(
        {
            "now": "1700000000",
            "tz": "-19800",
            "ntp_enable": "1",
            "ntp_svr": "time.windows.com",
            "WebPwd": "ignored",
        }
    ) == {
        "available": True,
        "timezone": "UTC+05:30",
        "timezone_offset_seconds": 19800,
        "unix_time": 1_700_000_000,
        "utc_time": "2023-11-14T22:13:20Z",
        "local_time": "2023-11-15T03:43:20+05:30",
        "ntp_enabled": True,
        "ntp_server": "time.windows.com",
    }
    assert parse_time_settings({}) == {"available": False}


def test_time_status_masks_a_literal_public_ntp_ip():
    result = parse_time_settings({"ntp_enable": 1, "ntp_svr": "8.8.8.8"})
    assert result["ntp_server"] == "***"


async def test_concurrent_time_changes_preserve_each_others_fields():
    class TimeTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.state = {"tz": "0", "ntp_enable": "1", "ntp_svr": "old.example"}

        async def request(self, command, *, timeout):
            self.requests.append(command)
            if command.startswith("GET /get_params.cgi"):
                response = ";".join(f"var {k}='{v}'" for k, v in self.state.items())
                await asyncio.sleep(0)  # Let the other caller attempt its read.
                return response
            query = parse_qs(urlsplit(command.removeprefix("GET ")).query)
            self.state.update({key: query[key][0] for key in self.state})
            return "result=ok;"

    transport = TimeTransport()
    async with VStarcamCamera(camera_config(), transport=transport) as camera:
        await asyncio.gather(
            camera.set_time_settings(ntp_enabled=False),
            camera.set_time_settings(ntp_server="new.example"),
        )

    assert transport.state == {"tz": "0", "ntp_enable": "0", "ntp_svr": "new.example"}
    assert len(transport.requests) == 4
