"""Configuration loading with CLI > environment > YAML > defaults precedence."""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigError

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

DEFAULTS: dict[str, Any] = {
    "host": None,
    "vuid": None,
    "username": "admin",
    "password": None,
    "psk": "vstarcam2018",
    "udp_port": 12833,
    "discovery_port": 32108,
    "auth_mode": "basic",
    "account_id": None,
    "login_hash": None,
    "login_token": None,
    "transport": "aiopppp",
    "timeout": 8.0,
    "retries": 1,
}

ENV_FIELDS = {
    "host": "VSTARCAM_HOST",
    "vuid": "VSTARCAM_VUID",
    "username": "VSTARCAM_USERNAME",
    "password": "VSTARCAM_PASSWORD",
    "psk": "VSTARCAM_PSK",
    "udp_port": "VSTARCAM_UDP_PORT",
    "discovery_port": "VSTARCAM_DISCOVERY_PORT",
    "auth_mode": "VSTARCAM_AUTH_MODE",
    "account_id": "VSTARCAM_ACCOUNT_ID",
    "login_hash": "VSTARCAM_LOGIN_HASH",
    "login_token": "VSTARCAM_LOGIN_TOKEN",
    "transport": "VSTARCAM_TRANSPORT",
    "timeout": "VSTARCAM_TIMEOUT",
    "retries": "VSTARCAM_RETRIES",
}

INT_FIELDS = {"udp_port", "discovery_port", "retries"}
FLOAT_FIELDS = {"timeout"}


@dataclass(slots=True)
class VStarcamConfig:
    host: str | None = None
    vuid: str | None = None
    username: str = "admin"
    password: str | None = None
    psk: str = "vstarcam2018"
    udp_port: int = 12833
    discovery_port: int = 32108
    auth_mode: str = "basic"
    account_id: str | None = None
    login_hash: str | None = None
    login_token: str | None = None
    transport: str = "aiopppp"
    timeout: float = 8.0
    retries: int = 1

    def validate(self, *, require_target: bool = False, require_auth: bool = False) -> None:
        if self.auth_mode not in {"basic", "observed", "auto"}:
            raise ConfigError("auth_mode must be one of: basic, observed, auto")
        if self.auth_mode == "auto":
            raise ConfigError(
                "auth_mode=auto is not implemented: local loginToken acquisition/refresh is unavailable"
            )
        if self.transport not in {"aiopppp", "fake"}:
            raise ConfigError("transport must be one of: aiopppp, fake")
        if not 1 <= self.udp_port <= 65535 or not 1 <= self.discovery_port <= 65535:
            raise ConfigError("UDP ports must be between 1 and 65535")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigError("timeout must be a finite number greater than zero")
        if self.retries < 0:
            raise ConfigError("retries cannot be negative")
        if require_target and not self.host:
            raise ConfigError("camera host is required (use --host, VSTARCAM_HOST, or local YAML)")
        if require_auth:
            if not self.username:
                raise ConfigError("username is required")
            if self.password is None:
                raise ConfigError("password is required; VSTARCAM_PASSWORD is recommended")
            if self.auth_mode == "observed" and (
                not self.account_id or not self.login_hash or not self.login_token
            ):
                raise ConfigError(
                    "observed auth requires account_id, login_hash, and login_token "
                    "in ignored local configuration"
                )

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        values = asdict(self)
        if not include_secrets:
            for key in ("password", "psk", "login_hash", "login_token", "account_id"):
                if values.get(key) is not None:
                    values[key] = "***"
        return values


def _read_yaml(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except (PermissionError, UnicodeError, OSError) as exc:
        raise ConfigError(f"configuration file cannot be read: {path}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"invalid YAML in {path}{location}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    unknown = sorted(set(loaded) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown configuration field(s): {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key, value in loaded.items():
        if isinstance(value, str):
            match = _ENV_PATTERN.fullmatch(value)
            if match:
                env_name = match.group(1)
                if env_name not in env:
                    raise ConfigError(
                        f"environment variable {env_name} referenced by {path} is not set"
                    )
                value = env[env_name]
        result[key] = value
    return result


def _coerce(key: str, value: Any) -> Any:
    if value is None:
        return None
    try:
        if key in INT_FIELDS:
            return int(value)
        if key in FLOAT_FIELDS:
            return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} has an invalid numeric value") from exc
    return str(value) if key in DEFAULTS and isinstance(DEFAULTS[key], (str, type(None))) else value


def load_config(
    *,
    cli: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    yaml_path: str | Path | None = None,
    use_default_file: bool = True,
) -> VStarcamConfig:
    """Load configuration using CLI > environment > YAML > defaults precedence."""

    cli = dict(cli or {})
    env = env if env is not None else os.environ
    selected_path = yaml_path or cli.pop("config", None) or env.get("VSTARCAM_CONFIG")
    if selected_path is None and use_default_file:
        default_path = Path("config.local.yaml")
        selected_path = default_path if default_path.exists() else None

    values = dict(DEFAULTS)
    if selected_path is not None:
        values.update(_read_yaml(Path(selected_path), env))
    for field, env_name in ENV_FIELDS.items():
        if env_name in env:
            values[field] = env[env_name]
    for key, value in cli.items():
        if key in DEFAULTS and value is not None:
            values[key] = value

    coerced = {key: _coerce(key, value) for key, value in values.items()}
    config = VStarcamConfig(**coerced)
    config.validate()
    return config
