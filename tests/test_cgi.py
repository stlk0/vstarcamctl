from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from vstarcamctl.cgi import (
    append_auth,
    format_eye4_auth_request,
    format_get_request,
    validate_raw_path,
)
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import RawCommandError


def config(**overrides):
    values = {"host": "192.0.2.10", "username": "admin", "password": "s3cret"}
    values.update(overrides)
    return VStarcamConfig(**values)


@pytest.mark.parametrize(
    "path",
    [
        "http://camera/get_status.cgi",
        "//camera/get_status.cgi",
        "/get_status.cgi#fragment",
        "/get_status.cgi\r\nINJECTED",
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
    assert query["pwd"] == ["s3cret"]


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
        "loginuse=admin&userId=account&loginpas=digest&user=admin&pwd=s3cret&"
    )


def test_exact_get_request_shape():
    assert format_get_request("/get_params.cgi?x=1", config()) == (
        "GET /get_params.cgi?x=1&loginuse=admin&user=admin&pwd=s3cret&"
    )
