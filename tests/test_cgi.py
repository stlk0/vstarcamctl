from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers import camera_config as config
from vstarcamctl.cgi import (
    append_auth,
    build_login_status_path,
    format_eye4_auth_request,
    format_get_request,
    format_login_status_request,
    validate_raw_path,
)
from vstarcamctl.errors import ConfigError, RawCommandError


@pytest.mark.parametrize(
    "path",
    [
        "http://camera/get_status.cgi",
        "//camera/get_status.cgi",
        "/get_status.cgi#fragment",
        "/get_status.cgi\r\nINJECTED",
        "/%72estore_factory.cgi?x=1",
        "/safe/../restore_factory.cgi?x=1",
        "/not-cgi",
    ],
)
def test_raw_path_rejects_external_or_malformed_values(path):
    with pytest.raises(RawCommandError):
        validate_raw_path(path)


def test_auth_preserves_query_and_replaces_caller_credentials():
    result = append_auth("/get_status.cgi?vuid=VE123&pwd=attacker&user=other", config())
    query = parse_qs(urlsplit(result).query, keep_blank_values=True)
    assert query["vuid"] == ["VE123"]
    assert query["loginuse"] == ["admin"]
    assert query["user"] == ["admin"]
    assert query["pwd"] == ["camera-secret"]


def test_observed_auth_suffix_excludes_preflight_token():
    result = append_auth(
        "/get_params.cgi",
        config(
            auth_mode="observed",
            account_id="account",
            login_hash="digest",
            login_token="token-value",
        ),
    )
    query = parse_qs(urlsplit(result).query)
    assert query["userId"] == ["account"]
    assert query["loginpas"] == ["digest"]
    assert "loginToken" not in query


def test_basic_auth_uses_apk_factory_suffix_when_account_id_is_available():
    result = append_auth(
        "/get_params.cgi",
        config(account_id="account"),
    )
    query = parse_qs(urlsplit(result).query)
    assert query["userId"] == ["account"]
    assert query["loginpas"] == ["camera-secret"]


@pytest.mark.parametrize("account_id", [None, "0"])
def test_no_account_auth_uses_zero_identity_and_local_password(account_id):
    assert format_get_request("/get_params.cgi", config(account_id=account_id)) == (
        "GET /get_params.cgi?loginuse=admin&userId=0&loginpas=camera-secret"
        "&user=admin&pwd=camera-secret&"
    )
    assert format_eye4_auth_request(config(account_id=account_id)) == (
        "GET /eye4_authentication.cgi?loginAccount=0&loginToken=&"
        "loginuse=admin&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    )


def test_nonzero_account_preflight_requires_observed_credentials():
    with pytest.raises(RawCommandError, match="observed account credentials"):
        format_eye4_auth_request(config(account_id="account"))


@pytest.mark.parametrize("missing", ["account_id", "login_hash", "login_token"])
def test_observed_preflight_still_requires_all_account_credentials(missing):
    values = {"account_id": "account", "login_hash": "digest", "login_token": "token"}
    values[missing] = None
    with pytest.raises(ConfigError, match="observed auth requires"):
        format_eye4_auth_request(config(auth_mode="observed", **values))


def test_wifi_set_uses_trusted_lowercase_endpoint_userid():
    result = append_auth(
        "/set_wifi.cgi?ssid=network&channel=6&authtype=4&wpa_psk=secret123&"
        "enable=1&userid=attacker",
        config(account_id="trusted-account"),
    )
    query = parse_qs(urlsplit(result).query)
    assert query["userid"] == ["trusted-account"]
    assert query["userId"] == ["trusted-account"]


def test_wifi_set_observed_auth_keeps_endpoint_and_session_identity_fields_distinct():
    result = append_auth(
        "/set_wifi.cgi?ssid=network&channel=6&authtype=4&wpa_psk=secret123&enable=1",
        config(
            auth_mode="observed",
            account_id="account",
            login_hash="digest",
            login_token="token-value",
        ),
    )
    query = parse_qs(urlsplit(result).query)
    assert query["userid"] == ["account"]
    assert query["userId"] == ["account"]


def test_wifi_set_refuses_missing_trusted_account_id():
    with pytest.raises(RawCommandError, match="non-empty account_id"):
        append_auth(
            "/set_wifi.cgi?ssid=network&channel=6&authtype=4&wpa_psk=secret123&enable=1",
            config(account_id=None),
        )


def test_eye4_preflight_matches_parameter_order():
    request = format_eye4_auth_request(
        config(
            auth_mode="observed",
            account_id="account",
            login_hash="digest",
            login_token="token-value",
        )
    )
    assert request == (
        "GET /eye4_authentication.cgi?loginAccount=account&loginToken=token-value&"
        "loginuse=admin&userId=account&loginpas=digest&user=admin&pwd=camera-secret&"
    )


def test_observed_login_status_matches_official_same_session_shape():
    request = format_login_status_request(
        config(
            auth_mode="observed",
            account_id="account",
            login_hash="digest",
            login_token="token-value",
        )
    )
    assert request == (
        "GET /get_status.cgi?name=admin&loginuse=admin&userId=account&loginpas=digest"
        "&user=admin&pwd=camera-secret&"
    )


def test_login_status_path_encodes_the_trusted_username():
    assert build_login_status_path("admin user") == "/get_status.cgi?name=admin+user"


@pytest.mark.parametrize("username", [None, "", "admin\nother"])
def test_login_status_path_rejects_invalid_username(username):
    with pytest.raises(RawCommandError):
        build_login_status_path(username)


def test_exact_get_request_shape():
    assert format_get_request("/get_params.cgi?x=1", config()) == (
        "GET /get_params.cgi?x=1&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    )
