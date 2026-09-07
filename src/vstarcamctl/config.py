"""Configuration loading with CLI > environment > YAML > defaults precedence."""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Mapping

import yaml

from ._private_file import write_private_file
from .errors import ConfigError
from .secrets import redact_data
from .transport import psk_for_device_id

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True, slots=True)
class VStarcamConfig:
    host: str | None = field(default=None, repr=False)
    source_address: str | None = field(default=None, repr=False)
    device_id: str | None = field(default=None, repr=False)
    username: str = field(default="admin", repr=False)
    password: str | None = field(default=None, repr=False)
    psk: str | None = field(default=None, repr=False)
    udp_port: int = 12833
    discovery_port: int = 32108
    auth_mode: str = "basic"
    account_id: str | None = field(default=None, repr=False)
    login_hash: str | None = field(default=None, repr=False)
    login_token: str | None = field(default=None, repr=False)
    timeout: float = 8.0
    retries: int = 1

    def validate(self, *, require_target: bool = False, require_auth: bool = False) -> None:
        for name, value in (
            ("host", self.host),
            ("device_id", self.device_id),
            ("password", self.password),
            ("account_id", self.account_id),
            ("login_hash", self.login_hash),
            ("login_token", self.login_token),
        ):
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"{name} must be text")
        if not isinstance(self.username, str):
            raise ConfigError("username must be text")
        if not self.username.strip():
            raise ConfigError("username cannot be empty")
        if self.username != self.username.strip():
            raise ConfigError("username cannot have surrounding whitespace")
        for name, value in (
            ("host", self.host),
            ("device_id", self.device_id),
            ("account_id", self.account_id),
            ("login_hash", self.login_hash),
            ("login_token", self.login_token),
        ):
            if value is not None and not value.strip():
                raise ConfigError(f"{name} cannot be empty")
        for name, value in (("host", self.host), ("device_id", self.device_id)):
            if value is not None and value != value.strip():
                raise ConfigError(f"{name} cannot have surrounding whitespace")
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {"basic", "observed"}:
            raise ConfigError("auth_mode must be one of: basic, observed")
        ports = (self.udp_port, self.discovery_port)
        if any(isinstance(port, bool) or not isinstance(port, int) for port in ports) or not all(
            1 <= port <= 65535 for port in ports
        ):
            raise ConfigError("UDP ports must be between 1 and 65535")
        timeout_is_valid = False
        if type(self.timeout) in (int, float):
            try:
                timeout_is_valid = math.isfinite(self.timeout) and self.timeout > 0
            except OverflowError:
                pass
        if not timeout_is_valid:
            raise ConfigError("timeout must be a finite number greater than zero")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or self.retries < 0:
            raise ConfigError("retries must be a non-negative integer")
        if self.psk is not None:
            if not isinstance(self.psk, str) or not self.psk:
                raise ConfigError("psk must be a non-empty ASCII string")
            try:
                self.psk.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ConfigError("psk must be a non-empty ASCII string") from exc
        if self.source_address is not None:
            if not isinstance(self.source_address, str):
                raise ConfigError("source_address must be an IPv4 address")
            try:
                IPv4Address(self.source_address)
            except ValueError as exc:
                raise ConfigError("source_address must be an IPv4 address") from exc
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

    def as_dict(self) -> dict[str, Any]:
        return redact_data(asdict(self))


DEFAULTS = {field.name: field.default for field in fields(VStarcamConfig)}
ENV_FIELDS = {name: f"VSTARCAM_{name.upper()}" for name in DEFAULTS}
_INTEGER_FIELDS = frozenset({"udp_port", "discovery_port", "retries"})
_TEXT_FIELDS = frozenset(DEFAULTS) - _INTEGER_FIELDS - {"timeout"}


def _read_yaml(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except (UnicodeError, OSError) as exc:
        raise ConfigError(f"configuration file cannot be read: {path}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"invalid YAML in {path}{location}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    if any(not isinstance(key, str) for key in loaded):
        raise ConfigError(f"configuration field names must be text: {path}")
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


def save_discovered_profile(
    path: str | Path,
    *,
    host: str,
    device_id: str,
    psk: str | None,
    udp_port: int,
    discovery_port: int,
    source_address: str | None = None,
) -> Path:
    """Create a private profile from discovery without inventing auth values.

    Existing files are never replaced. A separate path is the unambiguous and
    credential-safe representation of another camera.
    """

    output = Path(path)
    if not output.name:
        raise ConfigError("camera profile path must name a file")
    if not output.parent.is_dir():
        raise ConfigError(f"camera profile directory does not exist: {output.parent}")
    if output.is_symlink():
        raise ConfigError("refusing to write a camera profile through a symlink")
    if output.exists():
        raise ConfigError(
            f"camera profile already exists: {output}; choose a new *.local.yaml file"
        )
    if not isinstance(host, str) or not host.strip() or host != host.strip():
        raise ConfigError("discovered camera host is invalid")
    if not isinstance(device_id, str) or not device_id.strip() or device_id != device_id.strip():
        raise ConfigError("discovered camera device ID is invalid")
    if psk is not None and (not isinstance(psk, str) or not psk):
        raise ConfigError("discovery PSK is invalid")

    candidate = VStarcamConfig(
        host=host,
        source_address=source_address,
        device_id=device_id,
        psk=psk,
        udp_port=udp_port,
        discovery_port=discovery_port,
    )
    candidate.validate(require_target=True)

    profile = {
        "host": host,
        "device_id": device_id,
        "udp_port": udp_port,
        "discovery_port": discovery_port,
    }
    if psk is not None and psk != psk_for_device_id(device_id):
        profile["psk"] = psk
    if source_address is not None:
        profile["source_address"] = source_address
    rendered = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
    try:
        write_private_file(output, rendered.encode("utf-8"), overwrite=False)
    except FileExistsError as exc:
        raise ConfigError(
            f"camera profile already exists: {output}; choose a new *.local.yaml file"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot write camera profile: {output}") from exc
    return output


def _coerce(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _TEXT_FIELDS:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be text")
        return value
    if key in _INTEGER_FIELDS:
        if type(value) is int:
            return value
        if not isinstance(value, str) or re.fullmatch(r"[+-]?\d+", value) is None:
            raise ConfigError(f"{key} has an invalid numeric value")
        try:
            return int(value)
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f"{key} has an invalid numeric value") from exc
    if type(value) not in (int, float, str):
        raise ConfigError(f"{key} has an invalid numeric value")
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"{key} has an invalid numeric value") from exc


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
    unknown = sorted(set(cli) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown CLI configuration field(s): {', '.join(unknown)}")
    selected_path = yaml_path or env.get("VSTARCAM_CONFIG")
    if selected_path is None and use_default_file:
        default_path = Path("config.local.yaml")
        selected_path = default_path if default_path.exists() else None

    values = dict(DEFAULTS)
    if selected_path is not None:
        values.update(_read_yaml(Path(selected_path), env))
    for name, env_name in ENV_FIELDS.items():
        if env_name in env:
            values[name] = env[env_name]
    for key, value in cli.items():
        if key in DEFAULTS and value is not None:
            values[key] = value

    coerced = {key: _coerce(key, value) for key, value in values.items()}
    config = VStarcamConfig(**coerced)
    config.validate()
    return config
