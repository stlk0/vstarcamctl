from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError

import pytest

from vstarcamctl.config import VStarcamConfig, load_config, save_discovered_profile
from vstarcamctl.errors import ConfigError


def test_config_precedence_and_env_reference(tmp_path):
    config_file = tmp_path / "camera.local.yaml"
    config_file.write_text(
        "host: 10.0.0.10\nusername: yaml-user\npassword: '${CAMERA_SECRET}'\ntimeout: 2\n",
        encoding="utf-8",
    )
    config = load_config(
        yaml_path=config_file,
        env={
            "CAMERA_SECRET": "from-ref",
            "VSTARCAM_USERNAME": "env-user",
            "VSTARCAM_TIMEOUT": "3.5",
        },
        cli={"username": "cli-user", "host": "10.0.0.20"},
    )
    assert config.host == "10.0.0.20"
    assert config.username == "cli-user"
    assert config.password == "from-ref"
    assert config.timeout == 3.5


def test_source_address_loads_from_environment():
    config = load_config(
        env={"VSTARCAM_SOURCE_ADDRESS": "192.0.2.44"},
        use_default_file=False,
    )
    assert config.source_address == "192.0.2.44"


def test_psk_is_unset_until_the_caller_selects_or_discovers_one():
    assert VStarcamConfig().psk is None
    assert load_config(env={}, use_default_file=False).psk is None


@pytest.mark.parametrize("document", ["", "null", "{}"])
def test_empty_yaml_uses_defaults(document, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(document, encoding="utf-8")

    assert load_config(yaml_path=profile, env={}) == VStarcamConfig()


@pytest.mark.parametrize("document", ["false", "0", "[]", '""', "synthetic-secret"])
def test_yaml_root_must_be_a_mapping(document, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="configuration root must be a mapping") as caught:
        load_config(yaml_path=profile, env={})
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("key", ["1", "false", "null"])
def test_yaml_field_names_must_be_text(key, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(f"{key}: synthetic-secret\nunknown: other\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="configuration field names must be text") as caught:
        load_config(yaml_path=profile, env={})
    assert "synthetic-secret" not in str(caught.value)


def test_device_id_uses_only_the_new_config_and_environment_names(tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text("vuid: legacy-name\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown configuration field.*vuid"):
        load_config(yaml_path=profile, env={})

    config = load_config(
        env={
            "VSTARCAM_DEVICE_ID": "VSTJ-000001-AAAAA",
            "VSTARCAM_VUID": "must-not-be-used",
        },
        use_default_file=False,
    )
    assert config.device_id == "VSTJ-000001-AAAAA"


@pytest.mark.parametrize("source_address", ["", "192.0.2.999", "::1", 1234])
def test_source_address_must_be_an_ipv4_literal(source_address):
    config = VStarcamConfig(source_address=source_address)
    with pytest.raises(ConfigError, match="source_address must be an IPv4 address"):
        config.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("udp_port", True, "UDP ports"),
        ("discovery_port", 32108.0, "UDP ports"),
        ("timeout", True, "timeout"),
        ("timeout", "8", "timeout"),
        ("retries", False, "retries"),
        ("retries", 1.0, "retries"),
        ("psk", "", "ASCII"),
        ("psk", "seed-\N{SNOWMAN}", "ASCII"),
    ],
)
def test_direct_config_rejects_ambiguous_numeric_types_and_invalid_psk(field, value, message):
    config = VStarcamConfig(**{field: value})

    with pytest.raises(ConfigError, match=message):
        config.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("host", True, "host must be text"),
        ("host", "", "host cannot be empty"),
        ("host", " 192.0.2.40 ", "host cannot have surrounding whitespace"),
        ("username", 123, "username must be text"),
        ("username", "   ", "username cannot be empty"),
        ("username", " admin ", "username cannot have surrounding whitespace"),
        ("password", object(), "password must be text"),
        ("device_id", 123, "device_id must be text"),
        ("device_id", "", "device_id cannot be empty"),
        (
            "device_id",
            " VSTG-000001-AAAAA ",
            "device_id cannot have surrounding whitespace",
        ),
        ("account_id", 123, "account_id must be text"),
        ("login_hash", "", "login_hash cannot be empty"),
        ("login_token", object(), "login_token must be text"),
    ],
)
def test_direct_config_rejects_invalid_text_fields(field, value, message):
    config = VStarcamConfig(**{field: value})

    with pytest.raises(ConfigError, match=message):
        config.validate()


@pytest.mark.parametrize("field", ["udp_port", "discovery_port", "timeout", "retries"])
def test_yaml_boolean_is_not_coerced_to_a_number(field, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(f"{field}: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid numeric value"):
        load_config(yaml_path=profile, env={})


@pytest.mark.parametrize("field", ["udp_port", "discovery_port", "retries"])
def test_fractional_yaml_value_is_not_truncated_to_an_integer(field, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(f"{field}: 123.9\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid numeric value"):
        load_config(yaml_path=profile, env={})


@pytest.mark.parametrize("field", ["udp_port", "discovery_port", "retries"])
def test_fractional_cli_value_is_not_truncated_to_an_integer(field):
    with pytest.raises(ConfigError, match="invalid numeric value"):
        load_config(cli={field: 123.9}, env={}, use_default_file=False)


def test_huge_timeout_is_reported_as_a_configuration_error():
    with pytest.raises(ConfigError, match="finite"):
        VStarcamConfig(timeout=10**1000).validate()
    with pytest.raises(ConfigError, match="invalid numeric value"):
        load_config(cli={"timeout": 10**1000}, env={}, use_default_file=False)


@pytest.mark.parametrize("field", ["host", "device_id", "username", "password", "psk"])
def test_yaml_non_text_value_is_not_stringified(field, tmp_path):
    profile = tmp_path / "camera.local.yaml"
    profile.write_text(f"{field}: 123\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=f"{field} must be text"):
        load_config(yaml_path=profile, env={})


def test_missing_referenced_environment_variable_fails(tmp_path):
    config_file = tmp_path / "camera.local.yaml"
    config_file.write_text("password: '${MISSING_SECRET}'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="MISSING_SECRET"):
        load_config(yaml_path=config_file, env={})


def test_observed_auth_requires_local_values():
    config = VStarcamConfig(auth_mode="observed", password="secret")
    with pytest.raises(ConfigError, match="account_id"):
        config.validate(require_auth=True)


def test_config_dict_masks_secrets():
    config = load_config(
        env={
            "VSTARCAM_PASSWORD": "secret",
            "VSTARCAM_PSK": "seed",
            "VSTARCAM_DEVICE_ID": "VSTG-000001-AAAAA",
            "VSTARCAM_LOGIN_HASH": "hash-secret",
            "VSTARCAM_LOGIN_TOKEN": "token-secret",
        }
    )
    rendered = config.as_dict()
    assert rendered["password"] == "***"
    assert rendered["psk"] == "***"
    assert rendered["device_id"] == "***"
    assert rendered["login_hash"] == "***"
    assert rendered["login_token"] == "***"


def test_config_repr_redacts_private_values_and_instances_are_immutable():
    private_values = {
        "host": "192.0.2.40",
        "source_address": "192.0.2.41",
        "device_id": "PRIVATE-DEVICE-ID",
        "username": "private-user",
        "password": "private-password",
        "psk": "private-seed",
        "account_id": "private-account",
        "login_hash": "private-hash",
        "login_token": "private-token",
    }
    config = VStarcamConfig(**private_values)

    rendered = repr(config)
    assert all(value not in rendered for value in private_values.values())
    assert "timeout=8.0" in rendered
    with pytest.raises(FrozenInstanceError):
        config.timeout = 1.0


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_timeout_must_be_finite(timeout):
    with pytest.raises(ConfigError, match="finite"):
        load_config(
            env={"VSTARCAM_TIMEOUT": timeout},
            use_default_file=False,
        )


def test_yaml_error_does_not_echo_secret_source_line(tmp_path):
    config_file = tmp_path / "broken.local.yaml"
    config_file.write_text(
        "password: synthetic-secret\nbroken: [unterminated\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as caught:
        load_config(yaml_path=config_file, env={})
    assert "synthetic-secret" not in str(caught.value)


def test_default_local_file_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.local.yaml").write_text(
        "host: 192.0.2.10\n",
        encoding="utf-8",
    )
    assert load_config(env={}, use_default_file=False).host is None


@pytest.mark.parametrize("new_device_id", ["VSTG-000001-AAAAA", "VSTG-000002-BBBBB"])
def test_discovery_profile_refuses_existing_file_without_touching_it(tmp_path, new_device_id):
    profile = tmp_path / "camera.local.yaml"
    original = (
        b"# keep this profile byte-for-byte\n"
        b"host: 192.0.2.10\n"
        b"device_id: VSTG-000001-AAAAA\n"
        b"login_token: '${TEST_LOGIN_TOKEN}'\n"
    )
    profile.write_bytes(original)

    with pytest.raises(ConfigError, match="camera profile already exists"):
        save_discovered_profile(
            profile,
            host="192.0.2.12",
            device_id=new_device_id,
            psk="vstarcam2018",
            udp_port=24680,
            discovery_port=32108,
        )

    assert profile.read_bytes() == original


def test_discovery_profile_link_failure_leaves_no_files(tmp_path, monkeypatch):
    def fail_link(*_args, **_kwargs):
        raise OSError("synthetic link failure")

    monkeypatch.setattr("vstarcamctl.config.os.link", fail_link)
    profile = tmp_path / "camera.local.yaml"

    with pytest.raises(ConfigError, match="cannot write camera profile"):
        save_discovered_profile(
            profile,
            host="192.0.2.12",
            device_id="VSTG-000001-AAAAA",
            psk="vstarcam2018",
            udp_port=24680,
            discovery_port=32108,
        )

    assert list(tmp_path.iterdir()) == []


def test_discovery_profile_write_failure_leaves_no_temporary_file(tmp_path, monkeypatch):
    def fail_fsync(_descriptor):
        raise OSError("synthetic flush failure")

    monkeypatch.setattr("vstarcamctl._private_file.os.fsync", fail_fsync)
    profile = tmp_path / "camera.local.yaml"

    with pytest.raises(ConfigError, match="cannot write camera profile"):
        save_discovered_profile(
            profile,
            host="192.0.2.12",
            device_id="VSTG-000001-AAAAA",
            psk="vstarcam2018",
            udp_port=24680,
            discovery_port=32108,
        )

    assert list(tmp_path.iterdir()) == []


def test_discovery_profile_preserves_source_address(tmp_path):
    profile = tmp_path / "camera.local.yaml"
    save_discovered_profile(
        profile,
        host="192.0.2.12",
        source_address="192.0.2.44",
        device_id="VSTG-000001-AAAAA",
        psk="vstarcam2018",
        udp_port=24680,
        discovery_port=32108,
    )

    assert load_config(yaml_path=profile, env={}).source_address == "192.0.2.44"
    if os.name == "posix":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o600


def test_discovery_profile_omits_a_psk_derived_from_device_id(tmp_path):
    profile = tmp_path / "camera.local.yaml"
    save_discovered_profile(
        profile,
        host="192.0.2.12",
        device_id="VSTJ-000001-AAAAA",
        psk="vstarcam2019",
        udp_port=24680,
        discovery_port=32108,
    )

    assert load_config(yaml_path=profile, env={}).psk is None
    assert "psk:" not in profile.read_text(encoding="utf-8")


def test_discovery_profile_keeps_an_explicit_custom_psk(tmp_path):
    profile = tmp_path / "camera.local.yaml"
    save_discovered_profile(
        profile,
        host="192.0.2.12",
        device_id="CUSTOM-000001-AAAAA",
        psk="custom-seed",
        udp_port=24680,
        discovery_port=32108,
    )

    assert load_config(yaml_path=profile, env={}).psk == "custom-seed"


def test_discovery_profile_validates_the_supplied_psk(tmp_path):
    with pytest.raises(ConfigError, match="ASCII"):
        save_discovered_profile(
            tmp_path / "camera.local.yaml",
            host="192.0.2.12",
            device_id="VSTG-000001-AAAAA",
            psk="seed-\N{SNOWMAN}",
            udp_port=24680,
            discovery_port=32108,
        )

    assert list(tmp_path.iterdir()) == []
