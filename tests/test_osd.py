from __future__ import annotations

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    CapabilityUnavailableError,
    CatalogError,
    ExperimentalCommandError,
    OSDConfigurationError,
    ServiceChangeUncertainError,
)
from vstarcamctl.osd import (
    LOGO_OSD_STATUS_PATH,
    OSD_12H_STATUS_PATH,
    build_logo_osd_set_path,
    build_osd_12h_set_path,
    parse_logo_osd_set_response,
    parse_logo_osd_status,
    parse_osd_12h_set_response,
    parse_osd_12h_status,
    parse_timestamp_osd_status,
)


@pytest.mark.parametrize(
    ("twelve_hour", "value"),
    [(False, 0), (True, 1)],
)
def test_osd_builder_emits_exact_paths(twelve_hour, value):
    assert build_osd_12h_set_path(twelve_hour) == (
        f"/trans_cmd_string.cgi?cmd=4109&command=1&osd_12h_mode={value}"
    )


@pytest.mark.parametrize("value", [0, 1, "0", "1", None])
def test_osd_builder_rejects_non_booleans(value):
    with pytest.raises(OSDConfigurationError, match="boolean"):
        build_osd_12h_set_path(value)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(0, False), ("0", False), (1, True), ("1", True)],
)
def test_osd_response_parsers_normalize_exact_operation(mode, expected):
    getter = {"result": "0", "cmd": "4109", "command": "0", "osd_12h_mode": mode}
    setter = {"result": 0, "cmd": 4109, "command": 1, "osd_12h_mode": mode}
    assert parse_osd_12h_status(getter) is expected
    assert parse_osd_12h_set_response(setter, expected) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": 4109, "command": 0, "osd_12h_mode": 0},
        {"result": 1, "cmd": 4109, "command": 0, "osd_12h_mode": 0},
        {"result": 0, "cmd": 4110, "command": 0, "osd_12h_mode": 0},
        {"result": 0, "cmd": 4109, "command": 1, "osd_12h_mode": 0},
        {"result": 0, "cmd": 4109, "command": 0, "osd_12h_mode": 2},
        {"result": 0, "cmd": 4109, "command": 0, "osd_12h_mode": True},
    ],
)
def test_osd_status_rejects_invalid_response(payload):
    with pytest.raises(OSDConfigurationError):
        parse_osd_12h_status(payload)


def test_osd_set_response_requires_matching_echo_and_boolean_expectation():
    payload = {"result": 0, "cmd": 4109, "command": 1, "osd_12h_mode": 0}
    with pytest.raises(OSDConfigurationError, match="does not match"):
        parse_osd_12h_set_response(payload, True)
    with pytest.raises(OSDConfigurationError, match="boolean"):
        parse_osd_12h_set_response(payload, 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, False), ("0", False), (1, True), ("1", True)],
)
def test_timestamp_osd_status_normalizes_exact_field(value, expected):
    assert parse_timestamp_osd_status({"result": 0, "osdenable": value}) is expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"result": 1, "osdenable": 1},
        {"result": 0, "OSDEnable": 1},
        {"result": 0, "osdenable": 2},
        {"result": 0, "osdenable": True},
    ],
)
def test_timestamp_osd_status_rejects_malformed_response(payload):
    with pytest.raises(OSDConfigurationError):
        parse_timestamp_osd_status(payload)


async def test_timestamp_osd_uses_exact_confirmed_login_status_request():
    transport = FakeTransport(["var result=0; var osdenable=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_timestamp_osd() is True
    assert transport.requests == [
        "GET /get_status.cgi?name=admin&loginuse=admin&user=admin&pwd=camera-secret&"
    ]


async def test_timestamp_osd_retries_read_timeout():
    transport = FakeTransport([TimeoutError("read timed out"), "var result=0; var osdenable=0;"])
    camera = VStarcamCamera(config(retries=1), transport=transport)

    assert await camera.get_timestamp_osd() is False
    assert len(transport.requests) == 2
    assert transport.connect_count == 2


async def test_osd_set_invalid_input_fails_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(OSDConfigurationError, match="boolean"):
        await camera.set_osd_12h_mode(1)

    assert transport.connect_count == 0
    assert transport.requests == []


@pytest.mark.parametrize("operation", ["get", "set"])
async def test_osd_explicit_unsupported_capability_blocks(operation):
    transport = FakeTransport(["var 12h_mode_support=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="12h_mode_support"):
        if operation == "get":
            await camera.get_osd_12h_mode()
        else:
            await camera.set_osd_12h_mode(True)

    assert len(transport.requests) == 1
    assert "/get_params.cgi" in transport.requests[0]


async def test_osd_unknown_capability_follows_tri_state_policy():
    transport = FakeTransport(
        [
            "var firmware_version='test';",
            "result=0; cmd=4109; command=0; osd_12h_mode=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_osd_12h_mode() is False
    assert len(transport.requests) == 2
    assert "/get_params.cgi" in transport.requests[0]
    assert OSD_12H_STATUS_PATH in transport.requests[1]


@pytest.mark.parametrize(("mode", "echo"), [(False, 0), (True, 1)])
async def test_osd_supported_get_and_set_return_normalized_boolean(mode, echo):
    get_transport = FakeTransport(
        [
            "var 12h_mode_support=1;",
            f"result=0; cmd=4109; command=0; osd_12h_mode={echo};",
        ]
    )
    camera = VStarcamCamera(config(), transport=get_transport)
    assert await camera.get_osd_12h_mode() is mode
    assert sum("/get_params.cgi" in request for request in get_transport.requests) == 1
    assert OSD_12H_STATUS_PATH in get_transport.requests[1]

    set_transport = FakeTransport(
        [
            "var 12h_mode_support=1;",
            f"result=0; cmd=4109; command=1; osd_12h_mode={echo};",
        ]
    )
    camera = VStarcamCamera(config(), transport=set_transport)
    assert await camera.set_osd_12h_mode(mode) is mode
    assert sum("/get_params.cgi" in request for request in set_transport.requests) == 1
    assert build_osd_12h_set_path(mode) in set_transport.requests[1]


async def test_osd_status_retries_a_read_timeout():
    response = "result=0; cmd=4109; command=0; osd_12h_mode=0;"
    transport = FakeTransport(["var 12h_mode_support=1;", TimeoutError("read timed out"), response])
    camera = VStarcamCamera(config(retries=1), transport=transport)

    assert await camera.get_osd_12h_mode() is False
    assert len(transport.requests) == 3
    assert transport.connect_count == 2


async def test_osd_setter_is_not_retried_after_lost_acknowledgement():
    lost_ack = TimeoutError("acknowledgement lost")
    transport = FakeTransport(["var 12h_mode_support=1;", lost_ack])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_osd_12h_mode(True)

    assert len(transport.requests) == 2
    assert sum("command=1&osd_12h_mode=1" in request for request in transport.requests) == 1


@pytest.mark.parametrize(
    "acknowledgement",
    [
        "result=0; cmd=4109; command=1; osd_12h_mode=0;",
        "result=0; cmd=4109; command=1;",
        "result=0; cmd=4109; command=1; osd_12h_mode='unterminated;",
    ],
)
async def test_osd_invalid_setter_acknowledgement_is_an_uncertain_one_shot(acknowledgement):
    transport = FakeTransport(["var 12h_mode_support=1;", acknowledgement])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_osd_12h_mode(True)

    assert len(transport.requests) == 2
    assert sum("command=1&osd_12h_mode=1" in request for request in transport.requests) == 1


async def test_raw_osd_setter_requires_high_level_api_and_rejects_bad_shapes():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    exact = build_osd_12h_set_path(True)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi(exact, experimental=True, retry_requests=False)

    for malformed in (
        "/trans_cmd_string.cgi?cmd=4109&command=1",
        f"{exact}&vendor_extra=1",
    ):
        with pytest.raises(CatalogError):
            await camera.send_raw_cgi(
                malformed,
                experimental=True,
                retry_requests=False,
            )
    assert transport.requests == []


@pytest.mark.parametrize(("enabled", "value"), [(False, 0), (True, 1)])
def test_logo_osd_builder_emits_exact_paths(enabled, value):
    assert build_logo_osd_set_path(enabled) == f"/camera_control.cgi?param=11&value={value}"


@pytest.mark.parametrize("value", [0, 1, "0", "1", None])
def test_logo_osd_builder_rejects_non_booleans(value):
    with pytest.raises(OSDConfigurationError, match="boolean"):
        build_logo_osd_set_path(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, False), ("0", False), (1, True), ("1", True)],
)
def test_logo_osd_status_normalizes_exact_field(value, expected):
    assert parse_logo_osd_status({"logoOsdEnable": value}) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"logo_osd_enable": 1},
        {"logoOsdEnable": 2},
        {"logoOsdEnable": True},
    ],
)
def test_logo_osd_status_rejects_missing_or_invalid_exact_field(payload):
    with pytest.raises(OSDConfigurationError):
        parse_logo_osd_status(payload)


def test_logo_osd_set_response_returns_only_normalized_acknowledgement():
    assert parse_logo_osd_set_response({"result": "0"}) == {"result": 0}
    with pytest.raises(OSDConfigurationError, match="result must be 0"):
        parse_logo_osd_set_response({"result": 1})


async def test_logo_osd_permission_and_input_fail_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_logo_osd(True)
    with pytest.raises(OSDConfigurationError, match="boolean"):
        await camera.set_logo_osd(1, experimental=True)

    assert transport.connect_count == 0
    assert transport.requests == []


@pytest.mark.parametrize("operation", ["get", "set"])
async def test_logo_osd_explicit_unsupported_status_capability_blocks(operation):
    transport = FakeTransport(["var support_custom_logo_show=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="support_custom_logo_show"):
        if operation == "get":
            await camera.get_logo_osd()
        else:
            await camera.set_logo_osd(True, experimental=True)

    assert len(transport.requests) == 1
    assert "/get_status.cgi" in transport.requests[0]


@pytest.mark.parametrize(
    "capability",
    ["var firmware_version='test';", "var support_custom_logo_show='invalid';"],
)
async def test_logo_osd_unknown_capability_allows_confirmed_getter(capability):
    transport = FakeTransport([capability, "var logoOsdEnable=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_logo_osd() is True
    assert "/get_status.cgi" in transport.requests[0]
    assert LOGO_OSD_STATUS_PATH in transport.requests[1]


@pytest.mark.parametrize(("enabled", "wire"), [(False, 0), (True, 1)])
async def test_logo_osd_supported_get_returns_boolean_and_set_returns_ack(enabled, wire):
    getter_transport = FakeTransport(
        ["var support_custom_logo_show=1;", f"var logoOsdEnable={wire};"]
    )
    camera = VStarcamCamera(config(), transport=getter_transport)
    assert await camera.get_logo_osd() is enabled

    setter_transport = FakeTransport(
        ["var support_custom_logo_show=1;", f"var logoOsdEnable={1 - wire};", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=setter_transport)
    assert await camera.set_logo_osd(enabled, experimental=True) == {"result": 0}
    assert LOGO_OSD_STATUS_PATH in setter_transport.requests[1]
    assert build_logo_osd_set_path(enabled) in setter_transport.requests[2]


async def test_logo_osd_setter_requires_a_recoverable_prestate_before_write():
    transport = FakeTransport(["var firmware_version='test';", "var firmware_version='test';"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(OSDConfigurationError, match="logoOsdEnable"):
        await camera.set_logo_osd(True, experimental=True)

    assert len(transport.requests) == 2
    assert all("camera_control.cgi" not in request for request in transport.requests)


async def test_logo_osd_setter_is_not_retried_after_lost_acknowledgement():
    transport = FakeTransport(
        [
            "var support_custom_logo_show=1;",
            "var logoOsdEnable=0;",
            TimeoutError("acknowledgement lost"),
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_logo_osd(True, experimental=True)

    assert len(transport.requests) == 3
    assert sum("param=11&value=1" in request for request in transport.requests) == 1


@pytest.mark.parametrize("acknowledgement", ["result=1;", "var accepted=1;", "result=true;"])
async def test_logo_osd_invalid_setter_acknowledgement_is_uncertain(acknowledgement):
    transport = FakeTransport(
        ["var support_custom_logo_show=1;", "var logoOsdEnable=0;", acknowledgement]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_logo_osd(True, experimental=True)

    assert len(transport.requests) == 3
    assert sum("param=11&value=1" in request for request in transport.requests) == 1


async def test_raw_logo_osd_setter_requires_high_level_api_and_rejects_bad_shapes():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    exact = build_logo_osd_set_path(True)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi(exact, experimental=True, retry_requests=False)

    for malformed in (
        "/camera_control.cgi?param=11",
        f"{exact}&vendor_extra=1",
    ):
        with pytest.raises(CatalogError):
            await camera.send_raw_cgi(
                malformed,
                experimental=True,
                retry_requests=False,
            )
    assert transport.requests == []
