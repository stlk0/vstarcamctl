from __future__ import annotations

import pytest

from vstarcamctl.config import VStarcamConfig, load_config
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
    config = load_config(env={"VSTARCAM_PASSWORD": "secret", "VSTARCAM_PSK": "seed"})
    rendered = config.as_dict()
    assert rendered["password"] == "***"
    assert rendered["psk"] == "***"


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
