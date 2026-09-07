from __future__ import annotations

import builtins
import json
import os
import stat

import pytest

from vstarcamctl.cli import main
from vstarcamctl.errors import ProvisioningConfigurationError
from vstarcamctl.provisioning import build_static_wifi_qr_payload, write_static_wifi_qr_svg


@pytest.mark.parametrize(
    ("account_options", "expected_account_id"),
    [
        ({}, "0"),
        ({"account_id": None}, "0"),
        ({"account_id": "0"}, "0"),
        ({"account_id": "test-account"}, "test-account"),
    ],
)
def test_static_qr_payload_matches_vendor_field_order_and_null_bssid_fallback(
    account_options, expected_account_id
):
    payload = build_static_wifi_qr_payload(
        "setup network",
        "correcthorse",
        **account_options,
    )

    assert payload == (
        f'{{"BS":"NULL","P":"correcthorse","U":"{expected_account_id}","RS":"setup network"}}'
    )


def test_static_qr_payload_preserves_explicit_bssid():
    payload = build_static_wifi_qr_payload(
        "network",
        "correcthorse",
        account_id="test-account",
        bssid="020000000001",
    )

    assert json.loads(payload) == {
        "BS": "020000000001",
        "P": "correcthorse",
        "U": "test-account",
        "RS": "network",
    }


def test_static_qr_payload_rejects_non_text_bssid():
    with pytest.raises(ProvisioningConfigurationError, match="BSSID must be text"):
        build_static_wifi_qr_payload(
            "network",
            "correcthorse",
            account_id="test-account",
            bssid=123,
        )


@pytest.mark.parametrize(
    ("ssid", "password", "message"),
    [
        ('net"work', "correcthorse", "SSID cannot contain"),
        ("network", 'correct"horse', "password cannot contain"),
        (r"net\\work", "correcthorse", "SSID cannot contain"),
    ],
)
def test_static_qr_payload_rejects_characters_unsafe_for_vendor_string_template(
    ssid, password, message
):
    with pytest.raises(ProvisioningConfigurationError, match=message):
        build_static_wifi_qr_payload(ssid, password, account_id="test-account")


@pytest.mark.parametrize(
    ("account_id", "message"),
    [(0, "account ID must be text"), ("bad\naccount", "account ID contains control")],
)
def test_static_qr_payload_rejects_unsafe_account_id(account_id, message):
    with pytest.raises(ProvisioningConfigurationError, match=message):
        build_static_wifi_qr_payload(
            "setup network",
            "correcthorse",
            account_id=account_id,
        )


@pytest.mark.parametrize("account_id", ["", "   "])
def test_static_qr_payload_rejects_empty_explicit_account_id(account_id):
    with pytest.raises(ProvisioningConfigurationError, match="account ID"):
        build_static_wifi_qr_payload(
            "setup network",
            "correcthorse",
            account_id=account_id,
        )


def test_svg_artifact_is_private_and_does_not_overwrite_by_default(tmp_path):
    output = tmp_path / "camera-wifi.svg"
    payload = build_static_wifi_qr_payload(
        "setup network",
        "correcthorse",
        account_id="test-account",
    )

    assert write_static_wifi_qr_svg(payload, output) == output
    assert b"<svg" in output.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(ProvisioningConfigurationError, match="already exists"):
        write_static_wifi_qr_svg(payload, output)


def test_svg_overwrite_is_atomic_and_keeps_private_permissions(tmp_path):
    output = tmp_path / "camera-wifi.svg"
    output.write_bytes(b"old artifact")
    output.chmod(0o644)
    payload = build_static_wifi_qr_payload(
        "setup network",
        "correcthorse",
        account_id="test-account",
    )

    assert write_static_wifi_qr_svg(payload, output, overwrite=True) == output
    assert b"<svg" in output.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_svg_failed_overwrite_preserves_destination_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    output = tmp_path / "camera-wifi.svg"
    output.write_bytes(b"old artifact")
    payload = build_static_wifi_qr_payload(
        "setup network",
        "correcthorse",
        account_id="test-account",
    )

    def fail_replace(*_args, **_kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("vstarcamctl._private_file.os.replace", fail_replace)

    with pytest.raises(ProvisioningConfigurationError, match="cannot write QR output"):
        write_static_wifi_qr_svg(payload, output, overwrite=True)

    assert output.read_bytes() == b"old artifact"
    assert list(tmp_path.iterdir()) == [output]


def test_svg_commit_uses_the_resolved_parent_when_a_directory_symlink_changes(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    original_replace = os.replace

    def retarget_then_replace(source, destination):
        linked_parent.unlink()
        linked_parent.symlink_to(second, target_is_directory=True)
        original_replace(source, destination)

    monkeypatch.setattr("vstarcamctl._private_file.os.replace", retarget_then_replace)
    payload = build_static_wifi_qr_payload(
        "setup network",
        "correcthorse",
        account_id="test-account",
    )

    write_static_wifi_qr_svg(payload, linked_parent / "camera-wifi.svg", overwrite=True)

    assert (first / "camera-wifi.svg").exists()
    assert not (second / "camera-wifi.svg").exists()


def test_svg_renderer_reports_missing_optional_extra(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def import_without_qrcode(name, *args, **kwargs):
        if name == "qrcode" or name.startswith("qrcode."):
            raise ImportError("synthetic missing optional dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_qrcode)
    output = tmp_path / "camera-wifi.svg"

    with pytest.raises(ProvisioningConfigurationError, match="provisioning extra"):
        write_static_wifi_qr_svg(
            '{"BS":"NULL","P":"correcthorse","U":"test-account","RS":"network"}',
            output,
        )

    assert not output.exists()


@pytest.mark.parametrize("account_id", [None, "test-account"])
def test_cli_writes_redacted_qr_artifact(monkeypatch, tmp_path, capsys, account_id):
    password = "private-passphrase"
    ssid = "private network"
    output = tmp_path / "camera-wifi.svg"
    monkeypatch.delenv("VSTARCAM_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("VSTARCAM_CONFIG", raising=False)
    monkeypatch.setenv("VSTARCAM_WIFI_PASSWORD", password)
    account_args = ["--account-id", account_id] if account_id is not None else []

    result = main(
        [
            *account_args,
            "provision",
            "qr",
            "--ssid",
            ssid,
            "--output",
            str(output),
            "--experimental",
            "--confirm",
            "--recovery-ready",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert b"<svg" in output.read_bytes()
    assert all(
        value is None or value not in captured.out + captured.err
        for value in (password, ssid, account_id)
    )
    assert '"payload": "redacted"' in captured.out


def test_cli_requires_the_provisioning_safety_gates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("VSTARCAM_WIFI_PASSWORD", "private-passphrase")
    result = main(
        [
            "--account-id",
            "test-account",
            "provision",
            "qr",
            "--ssid",
            "private network",
            "--output",
            str(tmp_path / "camera-wifi.svg"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "--experimental" in captured.err
    assert "private-passphrase" not in captured.err
