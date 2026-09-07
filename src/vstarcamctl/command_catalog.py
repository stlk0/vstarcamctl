"""Structured command catalog loading, validation, matching, and safety gates."""

from __future__ import annotations

from collections import Counter
from dataclasses import MISSING, dataclass, fields
from importlib import resources
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

import yaml

from .errors import CatalogError, ConfirmationRequiredError, ExperimentalCommandError

STATUSES = {
    "confirmed",
    "observed_unconfirmed",
    "hypothesis",
    "failed",
    "dangerous_do_not_run",
}
RISK_LEVELS = {"read_only", "safe_write", "risky_write", "destructive", "unknown"}
ACCESS_TYPES = {"read", "write"}
RETRY_POLICIES = {"retryable", "one_shot"}


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    name: str
    path: str
    read_or_write: str
    risk_level: str
    confirmation_status: str
    tested_on_device: bool
    retry_policy: str = "retryable"
    recovery_required: bool = False
    raw_accessible: bool = True
    capability_flags: tuple[str, ...] = ()

    @property
    def is_experimental(self) -> bool:
        return self.confirmation_status != "confirmed"

    def require_permission(self, *, experimental: bool, confirm: bool) -> None:
        if self.confirmation_status == "dangerous_do_not_run":
            raise ExperimentalCommandError(f"{self.name}: marked dangerous_do_not_run")
        if self.confirmation_status == "failed":
            raise ExperimentalCommandError(f"{self.name}: marked failed and unavailable")
        if self.is_experimental and not experimental:
            raise ExperimentalCommandError(
                f"{self.name}: experimental/unconfirmed command; pass --experimental to test it"
            )
        if self.risk_level in {"risky_write", "destructive", "unknown"} and not confirm:
            raise ConfirmationRequiredError(f"{self.name}: pass --confirm after reviewing the risk")


_COMMAND_FIELDS = fields(CommandDefinition)
REQUIRED_FIELDS = {
    field.name
    for field in _COMMAND_FIELDS
    if field.default is MISSING and field.default_factory is MISSING
}
OPTIONAL_FIELDS = {field.name for field in _COMMAND_FIELDS} - REQUIRED_FIELDS


class CommandCatalog:
    def __init__(self, commands: Iterable[CommandDefinition]):
        self.commands = tuple(commands)
        self._by_name = {command.name: command for command in self.commands}
        if len(self._by_name) != len(self.commands):
            raise CatalogError("command names must be unique")
        if any(
            command.risk_level == "unknown" and command.retry_policy != "one_shot"
            for command in self.commands
        ):
            raise CatalogError("unknown-risk commands must be one_shot")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CommandCatalog":
        try:
            if path is not None:
                raw = Path(path).read_text(encoding="utf-8")
            else:
                raw = (
                    resources.files("vstarcamctl")
                    .joinpath("data/catalog.yaml")
                    .read_text(encoding="utf-8")
                )
        except (OSError, UnicodeError) as exc:
            raise CatalogError("command catalog cannot be read") from exc
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise CatalogError(f"invalid command catalog YAML{location}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("commands"), list):
            raise CatalogError("catalog must contain a commands list")
        commands = [
            cls._validate_item(item, index) for index, item in enumerate(payload["commands"])
        ]
        return cls(commands)

    @staticmethod
    def _validate_item(item: Any, index: int) -> CommandDefinition:
        if not isinstance(item, dict):
            raise CatalogError(f"commands[{index}] must be a mapping")
        missing = REQUIRED_FIELDS - set(item)
        unknown = set(item) - REQUIRED_FIELDS - OPTIONAL_FIELDS
        if missing:
            raise CatalogError(f"commands[{index}] missing: {', '.join(sorted(missing))}")
        if unknown:
            raise CatalogError(f"commands[{index}] unknown fields: {', '.join(sorted(unknown))}")
        choices = {
            "confirmation_status": STATUSES,
            "risk_level": RISK_LEVELS,
            "read_or_write": ACCESS_TYPES,
            "retry_policy": RETRY_POLICIES,
        }
        for field, allowed in choices.items():
            if item.get(field, "retryable") not in allowed:
                raise CatalogError(f"commands[{index}] has invalid {field}")
        if not isinstance(item.get("recovery_required", False), bool):
            raise CatalogError(f"commands[{index}] recovery_required must be boolean")
        if not isinstance(item.get("raw_accessible", True), bool):
            raise CatalogError(f"commands[{index}] raw_accessible must be boolean")
        if not isinstance(item["tested_on_device"], bool):
            raise CatalogError(f"commands[{index}] tested_on_device must be boolean")
        capability_flags = item.get("capability_flags", ())
        if not isinstance(capability_flags, (list, tuple)) or not all(
            isinstance(flag, str) and flag for flag in capability_flags
        ):
            raise CatalogError(f"commands[{index}] capability_flags must be a list of names")
        query_pairs = parse_qsl(urlsplit(item["path"]).query, keep_blank_values=True)
        query_keys = [key for key, _value in query_pairs]
        if len(query_keys) != len(set(query_keys)):
            raise CatalogError(f"commands[{index}] path contains duplicate query fields")
        return CommandDefinition(**{**item, "capability_flags": tuple(capability_flags)})

    def get(self, name: str) -> CommandDefinition:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise CatalogError(f"unknown catalog command: {name}") from exc

    def match_path(self, path: str) -> CommandDefinition | None:
        """Match a command without letting malformed known writes lose their policy."""

        parsed = urlsplit(path)
        actual_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        key_counts = Counter(key for key, _value in actual_pairs)
        duplicates = sorted(key for key, count in key_counts.items() if count > 1)
        if duplicates:
            raise CatalogError(
                "duplicate query parameter(s) are not allowed: " + ", ".join(duplicates)
            )
        actual = dict(actual_pairs)
        actual_key_set = set(actual)
        matches: list[CommandDefinition] = []
        dangerous_matches: list[CommandDefinition] = []

        for command in self.commands:
            template = urlsplit(command.path)
            if template.path != parsed.path:
                continue
            template_pairs = parse_qsl(template.query, keep_blank_values=True)
            template_values = dict(template_pairs)
            template_keys = set(template_values)
            fixed = {
                key: value
                for key, value in template_pairs
                if not (value.startswith("{") and value.endswith("}"))
            }
            fixed_matches = all(actual.get(key) == value for key, value in fixed.items())
            if not fixed_matches:
                continue

            if (
                command.confirmation_status == "dangerous_do_not_run"
                and template_keys <= actual_key_set
            ):
                dangerous_matches.append(command)

            optional_keys = {
                key
                for key, value in template_pairs
                if value.startswith("{") and value.endswith("?}")
            }
            required_keys = template_keys - optional_keys
            if required_keys <= actual_key_set <= template_keys:
                matches.append(command)

        if dangerous_matches:
            if len(dangerous_matches) > 1:
                raise CatalogError("raw CGI path ambiguously matches dangerous commands")
            return dangerous_matches[0]
        if len(matches) > 1:
            raise CatalogError("raw CGI path ambiguously matches multiple catalog commands")
        if matches:
            return matches[0]

        superset_writes: list[CommandDefinition] = []
        partial_writes: list[CommandDefinition] = []
        endpoint_writes: list[CommandDefinition] = []
        for command in self.commands:
            template = urlsplit(command.path)
            if template.path != parsed.path or command.read_or_write != "write":
                continue
            endpoint_writes.append(command)
            fixed = {
                key: value
                for key, value in parse_qsl(template.query, keep_blank_values=True)
                if not (value.startswith("{") and value.endswith("}"))
            }
            if any(key in actual and actual[key] != value for key, value in fixed.items()):
                continue
            template_keys = {
                key for key, _value in parse_qsl(template.query, keep_blank_values=True)
            }
            if template_keys <= actual_key_set:
                superset_writes.append(command)
            elif any(actual.get(key) == value for key, value in fixed.items()):
                partial_writes.append(command)

        if superset_writes:
            raise CatalogError("raw CGI path has extra fields for a known write shape")
        if any(
            not command.raw_accessible
            or command.recovery_required
            or command.confirmation_status == "dangerous_do_not_run"
            for command in endpoint_writes
        ):
            raise CatalogError("raw CGI path has an unrecognized protected write shape")
        if len(endpoint_writes) == 1 or partial_writes:
            raise CatalogError("raw CGI path has an incomplete known write shape")
        return None
