from __future__ import annotations

import pytest

from vstarcamctl.account import build_camera_account_password_set_path
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.cli import main
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    AccountChangeUncertainError,
    AccountConfigurationError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
)
from vstarcamctl.transport import FakeTransport


def config(**overrides):
    values = {
        "host": "192.0.2.10",
        "vuid": "VE123456",
        "username": "admin",
        "password": "camera-secret",
        "transport": "fake",
        "retries": 3,
    }
    values.update(overrides)
    return VStarcamConfig(**values)


def test_account_password_path_is_validated_and_encoded():
    assert build_camera_account_password_set_path("admin user", "replacement+secret") == (
        "/set_users.cgi?pwd_change_realtime=1&ExUser=admin+user&ExPwd=replacement%2Bsecret"
    )
    with pytest.raises(AccountConfigurationError, match="8 to 31"):
        build_camera_account_password_set_path("admin", "short")


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
            "var result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.set_camera_account_password(
        "replacement-secret",
        experimental=True,
        confirm=True,
        recovery_ready=True,
    ) == {"result": 0}

    assert len(transport.requests) == 2
    assert "/get_params.cgi?" in transport.requests[0]
    assert transport.requests[1] == (
        "GET /set_users.cgi?pwd_change_realtime=1"
        "&ExUser=admin&ExPwd=replacement-secret"
        "&loginuse=admin&user=admin&pwd=camera-secret&"
    )
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


async def test_account_password_timeout_is_not_retried():
    transport = FakeTransport(
        [
            "var user3_name='admin'; var WebPwd='old-secret';",
            TimeoutError("account service restarted"),
        ]
    )
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(AccountChangeUncertainError, match="without retry"):
        await camera.set_camera_account_password(
            "replacement-secret",
            experimental=True,
            confirm=True,
            recovery_ready=True,
        )

    assert len(transport.requests) == 2
    assert transport.close_count == 1


async def test_raw_account_password_write_cannot_bypass_guards_or_retry():
    path = "/set_users.cgi?pwd_change_realtime=1&ExUser=admin&ExPwd=replacement-secret"
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ConfirmationRequiredError, match="guarded command"):
        await camera.send_raw_cgi(path, experimental=True, confirm=True)
    with pytest.raises(ConfirmationRequiredError, match="retries are forbidden"):
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
            "--vuid",
            "VE123456",
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
