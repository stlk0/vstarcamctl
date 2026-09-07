from __future__ import annotations

import asyncio

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.actuator_state import (
    ACTUATOR_STATUS_PATH,
    build_alarm_led_set_path,
    parse_actuator_set_response,
    parse_alarm_led_set_response,
    parse_alarm_led_state,
    parse_light_state,
    parse_siren_state,
)
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    ActuatorStateConfigurationError,
    CapabilityUnavailableError,
    CatalogError,
    ExperimentalCommandError,
    ServiceChangeCancelledError,
    ServiceChangeUncertainError,
    TransportCommandCancelledError,
)

STATE_PARSERS = (
    ("sirenStatus", parse_siren_state),
    ("lightStatus", parse_light_state),
    ("alarmLedStatus", parse_alarm_led_state),
)


@pytest.mark.parametrize(("enabled", "value"), [(False, 0), (True, 1)])
def test_alarm_led_builder_emits_exact_paths(enabled, value):
    assert build_alarm_led_set_path(enabled) == (
        f"/trans_cmd_string.cgi?cmd=2109&command=0&alarmLed={value}"
    )


@pytest.mark.parametrize("value", [0, 1, "0", "1", None])
def test_alarm_led_builder_rejects_non_booleans(value):
    with pytest.raises(ActuatorStateConfigurationError, match="boolean"):
        build_alarm_led_set_path(value)


@pytest.mark.parametrize(("field", "parser"), STATE_PARSERS)
@pytest.mark.parametrize(("state", "expected"), [(0, False), ("0", False), (1, True), ("1", True)])
def test_actuator_state_parsers_normalize_only_their_exact_field(
    field,
    parser,
    state,
    expected,
):
    payload = {
        "result": "0",
        "cmd": "2109",
        "command": "2",
        "sirenStatus": 0,
        "lightStatus": 0,
        "alarmLedStatus": 0,
        field: state,
    }
    assert parser(payload) is expected


@pytest.mark.parametrize(("field", "parser"), STATE_PARSERS)
def test_actuator_state_parsers_do_not_alias_other_fields(field, parser):
    payload = {
        "result": 0,
        "cmd": 2109,
        "command": 2,
        "sirenStatus": 1,
        "lightStatus": 1,
        "alarmLedStatus": 1,
    }
    del payload[field]
    with pytest.raises(ActuatorStateConfigurationError, match=field):
        parser(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": 2109, "command": 2, "alarmLedStatus": 0},
        {"result": 1, "cmd": 2109, "command": 2, "alarmLedStatus": 0},
        {"result": 0, "cmd": 2110, "command": 2, "alarmLedStatus": 0},
        {"result": 0, "cmd": 2109, "command": 0, "alarmLedStatus": 0},
        {"result": 0, "cmd": 2109, "command": 2, "alarmLedStatus": 2},
        {"result": 0, "cmd": 2109, "command": 2, "alarmLedStatus": True},
    ],
)
def test_actuator_status_rejects_invalid_response(payload):
    with pytest.raises(ActuatorStateConfigurationError):
        parse_alarm_led_state(payload)


def test_alarm_led_set_response_requires_matching_echo_and_boolean_expectation():
    payload = {"result": 0, "cmd": 2109, "command": 0, "alarmLedStatus": 0}
    with pytest.raises(ActuatorStateConfigurationError, match="does not match"):
        parse_alarm_led_set_response(payload, True)
    with pytest.raises(ActuatorStateConfigurationError, match="boolean"):
        parse_alarm_led_set_response(payload, 0)


def test_confirmed_actuator_set_response_requires_exact_zero_result():
    assert parse_actuator_set_response({"result": "0", "vendor": "ignored"}) == {"result": 0}
    for payload in ({}, {"result": 1}, {"result": "ok"}):
        with pytest.raises(ActuatorStateConfigurationError):
            parse_actuator_set_response(payload)


async def test_alarm_led_permissions_and_input_fail_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.set_alarm_led(True)
    with pytest.raises(ActuatorStateConfigurationError, match="boolean"):
        await camera.set_alarm_led(1, experimental=True)

    assert transport.connect_count == 0
    assert transport.requests == []


@pytest.mark.parametrize(
    ("method_name", "field"),
    [("get_siren_state", "sirenStatus"), ("get_alarm_led", "alarmLedStatus")],
)
async def test_ungated_actuator_getters_use_one_shared_status_path(method_name, field):
    transport = FakeTransport([f"result=0; cmd=2109; command=2; {field}=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await getattr(camera, method_name)() is True
    assert len(transport.requests) == 1
    assert ACTUATOR_STATUS_PATH in transport.requests[0]


async def test_light_state_explicit_unsupported_capability_blocks_status_request():
    transport = FakeTransport(["var support_WhiteLed_Ctrl=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="white_light_on"):
        await camera.get_light_state()

    assert len(transport.requests) == 1
    assert "/get_status.cgi" in transport.requests[0]


@pytest.mark.parametrize(
    "capability",
    ["var firmware_version='test';", "var support_WhiteLed_Ctrl='invalid';"],
)
async def test_light_state_unknown_capability_allows_exact_status_request(capability):
    transport = FakeTransport([capability, "result=0; cmd=2109; command=2; lightStatus=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.get_light_state() is False
    assert "/get_status.cgi" in transport.requests[0]
    assert ACTUATOR_STATUS_PATH in transport.requests[1]


async def test_actuator_status_retries_a_read_timeout():
    response = "result=0; cmd=2109; command=2; sirenStatus=0;"
    transport = FakeTransport([TimeoutError("read timed out"), response])
    camera = VStarcamCamera(config(retries=1), transport=transport)

    assert await camera.get_siren_state() is False
    assert len(transport.requests) == 2
    assert transport.connect_count == 2


@pytest.mark.parametrize("method_name", ["set_siren", "set_light"])
async def test_confirmed_actuator_inputs_fail_before_connect(method_name):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ActuatorStateConfigurationError, match="boolean"):
        await getattr(camera, method_name)(1)

    assert transport.connect_count == 0


async def test_confirmed_siren_write_timeout_is_not_retried():
    transport = FakeTransport([TimeoutError("siren acknowledgement lost")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="sent once without retry"):
        await camera.set_siren(True)

    assert len(transport.requests) == 1


async def test_confirmed_siren_cancellation_after_send_is_an_uncertain_cancellation():
    transport = FakeTransport([TransportCommandCancelledError("cancelled after send")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeCancelledError, match="outcome is unknown") as caught:
        await camera.set_siren(True)

    assert isinstance(caught.value, asyncio.CancelledError)
    assert isinstance(caught.value, ServiceChangeUncertainError)
    assert len(transport.requests) == 1


async def test_confirmed_light_invalid_acknowledgement_is_uncertain():
    transport = FakeTransport(["var firmware_version='test';", "var result=1;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_light(True)

    assert len(transport.requests) == 2


@pytest.mark.parametrize(("enabled", "wire"), [(False, 0), (True, 1)])
async def test_alarm_led_set_returns_exact_echo(enabled, wire):
    transport = FakeTransport([f"result=0; cmd=2109; command=0; alarmLedStatus={wire};"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_alarm_led(enabled, experimental=True) is enabled
    assert build_alarm_led_set_path(enabled) in transport.requests[0]


async def test_alarm_led_setter_is_not_retried_after_lost_acknowledgement():
    transport = FakeTransport([TimeoutError("acknowledgement lost")])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_alarm_led(True, experimental=True)

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "acknowledgement",
    [
        "result=0; cmd=2109; command=0; alarmLedStatus=0;",
        "result=0; cmd=2109; command=0;",
        "result=1; cmd=2109; command=0; alarmLedStatus=1;",
    ],
)
async def test_alarm_led_invalid_setter_acknowledgement_is_uncertain(acknowledgement):
    transport = FakeTransport([acknowledgement])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.set_alarm_led(True, experimental=True)

    assert len(transport.requests) == 1


async def test_raw_alarm_led_setter_requires_high_level_api_and_rejects_bad_shapes():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)
    exact = build_alarm_led_set_path(True)

    with pytest.raises(ExperimentalCommandError, match="high-level API"):
        await camera.send_raw_cgi(exact, experimental=True, retry_requests=False)

    for malformed in (
        "/trans_cmd_string.cgi?cmd=2109&command=0",
        f"{exact}&vendor_extra=1",
    ):
        with pytest.raises(CatalogError):
            await camera.send_raw_cgi(
                malformed,
                experimental=True,
                retry_requests=False,
            )
    assert transport.requests == []
