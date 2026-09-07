from __future__ import annotations

import asyncio

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.account import (
    build_camera_account_password_set_path,
    build_camera_account_plaintext_enable_path,
    build_camera_owner_set_path,
    validate_account_step_response,
)
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.cli import main
from vstarcamctl.errors import (
    AccountChangeCancelledError,
    AccountChangeUncertainError,
    AccountConfigurationError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
    ResponseParseError,
    TransportCommandCancelledError,
    TransportTimeoutError,
)

_NEW_PASSWORD = "replacement-secret"
_OBSERVED_ACCOUNT = {
    "auth_mode": "observed",
    "account_id": "account-id",
    "login_hash": "owner-credential",
    "login_token": "account-token",
}
_FIRST_ENABLE_REQUEST = {
    "params": (
        "GET /get_params.cgi?loginuse=admin&userId=account-id&loginpas=owner-credential"
        "&user=admin&pwd=camera-secret&"
    ),
    "status": (
        "GET /get_status.cgi?name=admin&loginuse=admin&userId=account-id"
        "&loginpas=owner-credential&user=admin&pwd=camera-secret&"
    ),
    "owner": (
        "GET /set_users.cgi?pwd_change_realtime=1&OwnerUser=account-id"
        "&OwnerPwd=owner-credential&loginuse=admin&userId=account-id"
        "&loginpas=owner-credential&user=admin&pwd=camera-secret&"
    ),
    "owner_readback": (
        "GET /get_status.cgi?name=admin&loginuse=admin&userId=account-id"
        "&loginpas=owner-credential&user=admin&pwd=camera-secret&"
    ),
    "empty_enable": (
        "GET /set_users.cgi?pwd_change_realtime=1&ExUser=admin&ExPwd=&ExUserSwitch=1"
        "&loginuse=admin&userId=account-id&loginpas=owner-credential"
        "&user=admin&pwd=camera-secret&"
    ),
    "password": (
        "GET /set_users.cgi?pwd_change_realtime=1&ExUser=admin"
        "&ExPwd=replacement-secret&ExUserSwitch=1&loginuse=admin"
        "&userId=account-id&loginpas=owner-credential&user=admin"
        "&pwd=camera-secret&"
    ),
    "reboot": (
        "GET /reboot.cgi?loginuse=admin&userId=account-id&loginpas=owner-credential"
        "&user=admin&pwd=camera-secret&"
    ),
}
_FIRST_ENABLE_STAGES = {
    0: ("params", "status", "owner", "owner_readback", "empty_enable", "password", "reboot"),
    1: ("params", "status", "empty_enable", "password", "reboot"),
    2: ("params", "status", "password", "reboot"),
}
_FIRST_ENABLE_FAILURE_STAGES = {
    0: ("owner", "owner_readback", "empty_enable", "password", "reboot"),
    1: ("empty_enable", "password", "reboot"),
    2: ("password", "reboot"),
}


def _successful_first_enable_response(stage: str, initial_state: int) -> str:
    return {
        "params": "var user3_name='admin';",
        "status": f"var DualAuthentication={initial_state};",
        "owner": "var result=0; var DualAuthentication=1;",
        "owner_readback": "var DualAuthentication=1;",
        "empty_enable": "var result=0; var DualAuthentication=2;",
        "password": "var result=0; var DualAuthentication=2;",
        "reboot": "var result='ok';",
    }[stage]


def _first_enable_responses(
    initial_state: int,
    *,
    failed_stage: str | None = None,
    failure: str | BaseException | None = None,
    verify: bool = False,
) -> list[str | BaseException]:
    responses: list[str | BaseException] = []
    for stage in _FIRST_ENABLE_STAGES[initial_state]:
        if stage == failed_stage:
            assert failure is not None
            responses.append(failure)
            break
        responses.append(_successful_first_enable_response(stage, initial_state))
    if verify:
        assert failed_stage is None
        responses.append(f"var WebPwd='{_NEW_PASSWORD}';")
    return responses


def _first_enable_failure(kind: str) -> BaseException | str:
    if kind == "malformed":
        return "var broken='unterminated;"
    if kind == "timeout":
        return TimeoutError("synthetic first-enable timeout")
    if kind == "cancellation":
        return TransportCommandCancelledError("synthetic post-send cancellation")
    raise AssertionError(f"unknown failure kind: {kind}")


_FIRST_ENABLE_FAILURE_CASES = [
    pytest.param(state, stage, kind, id=f"state-{state}-{stage}-{kind}")
    for state, stages in _FIRST_ENABLE_FAILURE_STAGES.items()
    for stage in stages
    for kind in ("malformed", "timeout", "cancellation")
]


def test_account_password_path_is_validated_and_encoded():
    assert build_camera_account_password_set_path("admin user", "replacement+secret") == (
        "/set_users.cgi?pwd_change_realtime=1&ExUser=admin+user"
        "&ExPwd=replacement%2Bsecret&ExUserSwitch=1"
    )
    with pytest.raises(AccountConfigurationError, match="8 to 31"):
        build_camera_account_password_set_path("admin", "short")
    assert build_camera_account_plaintext_enable_path("admin") == (
        "/set_users.cgi?pwd_change_realtime=1&ExUser=admin&ExPwd=&ExUserSwitch=1"
    )
    assert build_camera_owner_set_path("account id", "owner+credential") == (
        "/set_users.cgi?pwd_change_realtime=1&OwnerUser=account+id&OwnerPwd=owner%2Bcredential"
    )


@pytest.mark.parametrize(
    ("payload", "expected_state"),
    [
        ({"result": False, "DualAuthentication": 2}, 2),
        ({"result": 0, "DualAuthentication": True}, 1),
    ],
)
def test_account_acknowledgement_rejects_boolean_integer_lookalikes(payload, expected_state):
    with pytest.raises(AccountConfigurationError, match="acknowledgement"):
        validate_account_step_response(payload, dual_authentication=expected_state)


async def test_account_password_write_requires_all_three_gates_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.set_camera_account_password("replacement-secret")
    with pytest.raises(ConfirmationRequiredError, match="--confirm"):
        await camera.set_camera_account_password("replacement-secret", experimental=True)
    with pytest.raises(ConfirmationRequiredError, match="--recovery-ready"):
        await camera.set_camera_account_password(
            "replacement-secret", experimental=True, confirm=True
        )

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_account_password_write_checks_slot_and_is_sent_once():
    transport = FakeTransport(
        [
            "var user3_name='admin'; var user3_pwd='legacy-secret'; var WebPwd='old-secret';",
            "var result=0; var DualAuthentication=2; var uid='ignored-metadata';",
            "var WebPwd='replacement-secret';",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_camera_account_password(
        "replacement-secret",
        experimental=True,
        confirm=True,
        recovery_ready=True,
    ) == {"result": 0, "DualAuthentication": 2, "web_password_verified": True}

    assert len(transport.requests) == 3
    assert "/get_params.cgi?" in transport.requests[0]
    assert transport.requests[1] == (
        "GET /set_users.cgi?pwd_change_realtime=1"
        "&ExUser=admin&ExPwd=replacement-secret&ExUserSwitch=1"
        "&loginuse=admin&user=admin&pwd=camera-secret&"
    )
    assert "/get_params.cgi?" in transport.requests[2]
    assert transport.close_count == 2


async def test_account_password_write_uses_required_first_enable_sequence():
    transport = FakeTransport(
        [
            "var user3_name='admin';",
            "var DualAuthentication=0;",
            "var result=0; var DualAuthentication=1;",
            "var DualAuthentication=1;",
            "var result=0; var DualAuthentication=2;",
            "var result=0; var DualAuthentication=2;",
            "var result='ok';",
            "var WebPwd='replacement-secret'; var rtsp_auth_enable=1;",
        ]
    )
    camera = VStarcamCamera(
        config(
            auth_mode="observed",
            account_id="account-id",
            login_hash="owner-credential",
            login_token="account-token",
        ),
        transport=transport,
    )

    assert await camera.set_camera_account_password(
        "replacement-secret",
        experimental=True,
        confirm=True,
        recovery_ready=True,
    ) == {"result": 0, "DualAuthentication": 2, "web_password_verified": True}

    assert len(transport.requests) == 8
    assert "/get_params.cgi?" in transport.requests[0]
    assert "/get_status.cgi?" in transport.requests[1]
    assert "OwnerUser=account-id&OwnerPwd=owner-credential" in transport.requests[2]
    assert "/get_status.cgi?" in transport.requests[3]
    assert "ExUser=admin&ExPwd=&ExUserSwitch=1" in transport.requests[4]
    assert "ExUser=admin&ExPwd=replacement-secret&ExUserSwitch=1" in transport.requests[5]
    assert "/reboot.cgi?" in transport.requests[6]
    assert "/get_params.cgi?" in transport.requests[7]
    assert transport.close_count == 2


@pytest.mark.parametrize("initial_state", [0, 1, 2])
async def test_account_first_enable_resumes_from_each_reported_state_with_exact_requests(
    initial_state,
):
    transport = FakeTransport(
        _first_enable_responses(initial_state, verify=True),
    )
    camera = VStarcamCamera(
        config(**_OBSERVED_ACCOUNT),
        transport=transport,
    )

    assert await camera.set_camera_account_password(
        _NEW_PASSWORD,
        experimental=True,
        confirm=True,
        recovery_ready=True,
    ) == {"result": 0, "DualAuthentication": 2, "web_password_verified": True}

    expected = [_FIRST_ENABLE_REQUEST[stage] for stage in _FIRST_ENABLE_STAGES[initial_state]]
    expected.append(_FIRST_ENABLE_REQUEST["params"])
    assert transport.requests == expected
    assert not transport.responses
    assert transport.close_count == 2


@pytest.mark.parametrize(
    ("initial_state", "failed_stage", "failure_kind"), _FIRST_ENABLE_FAILURE_CASES
)
async def test_account_first_enable_failure_stops_at_exact_one_shot_prefix(
    initial_state,
    failed_stage,
    failure_kind,
):
    failure = _first_enable_failure(failure_kind)
    transport = FakeTransport(
        _first_enable_responses(
            initial_state,
            failed_stage=failed_stage,
            failure=failure,
        )
    )
    camera = VStarcamCamera(
        config(retries=5, **_OBSERVED_ACCOUNT),
        transport=transport,
    )

    expected_error = (
        AccountChangeCancelledError
        if failure_kind == "cancellation"
        else AccountChangeUncertainError
    )
    expected_message = "unknown" if failure_kind == "cancellation" else "do not"
    with pytest.raises(expected_error, match=expected_message) as caught:
        await camera.set_camera_account_password(
            _NEW_PASSWORD,
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    expected_stages = _FIRST_ENABLE_STAGES[initial_state]
    failed_index = expected_stages.index(failed_stage)
    assert transport.requests == [
        _FIRST_ENABLE_REQUEST[stage] for stage in expected_stages[: failed_index + 1]
    ]
    assert not transport.responses
    assert transport.close_count == 1
    assert transport.connect_count == 1

    expected_cause = {
        "malformed": ResponseParseError,
        "timeout": TransportTimeoutError,
        "cancellation": TransportCommandCancelledError,
    }[failure_kind]
    assert isinstance(caught.value.__cause__, expected_cause)
    if failure_kind == "cancellation":
        assert isinstance(caught.value, asyncio.CancelledError)
        assert isinstance(caught.value, AccountChangeUncertainError)


@pytest.mark.parametrize("account_id", [None, "0"], ids=["missing-account", "zero-account"])
async def test_account_password_first_enable_requires_observed_account_credentials(account_id):
    transport = FakeTransport(
        [
            "var user3_name='admin';",
            "var DualAuthentication=0;",
        ]
    )
    camera = VStarcamCamera(config(account_id=account_id), transport=transport)

    with pytest.raises(AccountConfigurationError, match="observed account credentials"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 2
    assert all("/set_users.cgi" not in request for request in transport.requests)
    assert transport.close_count == 1


@pytest.mark.parametrize("status_response", ["var DualAuthentication=false;", "var result='ok';"])
async def test_account_password_first_enable_rejects_invalid_authentication_state(status_response):
    transport = FakeTransport(
        [
            "var user3_name='admin';",
            status_response,
        ]
    )
    camera = VStarcamCamera(
        config(
            auth_mode="observed",
            account_id="account-id",
            login_hash="owner-credential",
            login_token="account-token",
        ),
        transport=transport,
    )

    with pytest.raises(AccountConfigurationError, match="supported DualAuthentication"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert transport.requests == [
        _FIRST_ENABLE_REQUEST["params"],
        _FIRST_ENABLE_REQUEST["status"],
    ]
    assert transport.close_count == 1


async def test_account_password_write_refuses_mismatched_owner_metadata():
    transport = FakeTransport(["var user3_name='different-user'; var WebPwd='x';"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(AccountConfigurationError, match="WebPwd metadata"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 1
    assert transport.close_count == 1


@pytest.mark.parametrize("web_password", ["123", "null"])
async def test_account_password_write_refuses_non_text_webpwd_metadata(web_password):
    transport = FakeTransport([f"var user3_name='admin'; var WebPwd={web_password};"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(AccountConfigurationError, match="WebPwd metadata"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 1
    assert transport.close_count == 1


async def test_account_password_write_rejects_unconfirmed_acknowledgement():
    transport = FakeTransport(
        [
            "var user3_name='admin'; var WebPwd='old-secret';",
            "var result=0; var DualAuthentication=2;",
            "var user3_name='admin';",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(AccountChangeUncertainError, match="did not confirm"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 3
    assert transport.close_count == 2


@pytest.mark.parametrize(
    "acknowledgement",
    [
        "var result=1; var DualAuthentication=2;",
        "var result=false; var DualAuthentication=2;",
        "var result=0;",
        "var result=0; var DualAuthentication=1;",
    ],
)
async def test_existing_account_effect_does_not_override_invalid_acknowledgement(
    acknowledgement,
):
    transport = FakeTransport(
        [
            "var user3_name='admin'; var WebPwd='old-secret';",
            acknowledgement,
            "var WebPwd='replacement-secret';",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(AccountChangeUncertainError, match="acknowledgement"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 3
    assert transport.close_count == 2


@pytest.mark.parametrize(
    "write_failure",
    [
        pytest.param(TimeoutError("account service restarted"), id="timeout"),
        pytest.param("var result='unterminated;", id="malformed-response"),
        pytest.param(RuntimeError("unexpected dependency failure"), id="unexpected-error"),
    ],
)
async def test_account_password_write_failure_is_verified_without_retry(write_failure):
    transport = FakeTransport(
        [
            "var user3_name='admin'; var WebPwd='old-secret';",
            write_failure,
            "var WebPwd='replacement-secret';",
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(AccountChangeUncertainError, match="acknowledgement"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 3
    assert transport.close_count == 2


async def test_account_password_unexpected_verification_error_is_uncertain():
    transport = FakeTransport(
        [
            "var user3_name='admin'; var WebPwd='old-secret';",
            "var result=0; var DualAuthentication=2;",
            RuntimeError("unexpected dependency failure"),
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(AccountChangeUncertainError, match="could not be verified"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 3
    assert transport.close_count == 2


@pytest.mark.parametrize("blocked_stage", ["write", "first_close", "second_close"])
async def test_account_password_verification_survives_repeated_cancellation(blocked_stage):
    class BlockingTransport(FakeTransport):
        def __init__(self):
            super().__init__(
                [
                    "var user3_name='admin'; var WebPwd='old-secret';",
                    "var result=0; var DualAuthentication=2;",
                    "var WebPwd='replacement-secret';",
                ]
            )
            self.close_attempts = 0
            self.block_started = asyncio.Event()
            self.allow_blocked_stage = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if blocked_stage == "write" and len(self.requests) == 1:
                self.requests.append(command)
                response = self.responses.popleft()
                assert isinstance(response, str)
                self.block_started.set()
                await self.allow_blocked_stage.wait()
                return response
            return await super().request(command, timeout=timeout)

        async def close(self) -> None:
            self.close_attempts += 1
            stage = "first_close" if self.close_attempts == 1 else "second_close"
            if blocked_stage == stage:
                self.block_started.set()
                await self.allow_blocked_stage.wait()
            await super().close()

    transport = BlockingTransport()
    camera = VStarcamCamera(config(), transport=transport)
    task = asyncio.create_task(
        camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )
    )

    await asyncio.wait_for(transport.block_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    transport.allow_blocked_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(transport.requests) == 3
    assert transport.close_count == 2
    assert not transport.connected


async def test_account_password_workflows_are_serialized_per_camera():
    class GatedFirstRequestTransport(FakeTransport):
        def __init__(self):
            super().__init__(
                [
                    "var user3_name='admin'; var WebPwd='old-secret';",
                    "var result=0; var DualAuthentication=2;",
                    "var WebPwd='replacement-one';",
                    "var user3_name='admin'; var WebPwd='replacement-one';",
                    "var result=0; var DualAuthentication=2;",
                    "var WebPwd='replacement-two';",
                ]
            )
            self.first_request_seen = asyncio.Event()
            self.release_first_request = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if not self.requests:
                self.requests.append(command)
                response = self.responses.popleft()
                assert isinstance(response, str)
                self.first_request_seen.set()
                await self.release_first_request.wait()
                return response
            return await super().request(command, timeout=timeout)

    transport = GatedFirstRequestTransport()
    camera = VStarcamCamera(config(), transport=transport)
    first = asyncio.create_task(
        camera.set_camera_account_password(
            "replacement-one",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )
    )
    await transport.first_request_seen.wait()
    second = asyncio.create_task(
        camera.set_camera_account_password(
            "replacement-two",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )
    )
    await asyncio.sleep(0)

    assert len(transport.requests) == 1
    transport.release_first_request.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["web_password_verified"] is True
    assert second_result["web_password_verified"] is True
    assert len(transport.requests) == 6


async def test_raw_account_password_write_cannot_bypass_guards_or_retry():
    path = (
        "/set_users.cgi?pwd_change_realtime=1&ExUser=admin&ExPwd=replacement-secret&ExUserSwitch=1"
    )
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

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


def test_account_password_cli_dry_run_masks_both_fields(capsys):
    result = main(
        [
            "--host",
            "192.0.2.10",
            "--device-id",
            "VSTG-000001-AAAAA",
            "--password",
            "camera-admin-password",
            "account",
            "password",
            "--new-password",
            "replacement-secret",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "replacement-secret" not in output
    assert "camera-admin-password" not in output
    assert "ExUser=***" in output
    assert "ExPwd=***" in output
    assert "first_enable_steps" in output
    assert "camera reboot" in output
