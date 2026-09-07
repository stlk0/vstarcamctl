"""Parser for JavaScript-like VStarcam CGI responses."""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Iterator

from .errors import ResponseParseError

_NAME = r"[A-Za-z0-9_$][\w$]*"
_ARRAY_DECL_RE = re.compile(
    rf"^(?:var\s+)?({_NAME})\s*=\s*new\s+Array\s*\(\s*\)\s*$",
    re.DOTALL,
)
_ARRAY_ITEM_RE = re.compile(
    rf"^({_NAME})\s*\[\s*(\d+)\s*\]\s*=\s*(.*)$",
    re.DOTALL,
)
_ASSIGNMENT_RE = re.compile(rf"^(?:var\s+)?({_NAME})\s*=\s*(.*)$", re.DOTALL)
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)$")
_MAX_ARRAY_INDEX = 4095
_MAX_TOTAL_ARRAY_ITEMS = 16_384
_MAX_NUMERIC_LITERAL_LENGTH = 128


def _require_bounded_numeric_literal(raw: str, *, field: str = "numeric literal") -> None:
    if len(raw) > _MAX_NUMERIC_LITERAL_LENGTH:
        raise ResponseParseError(
            f"camera response {field} exceeds {_MAX_NUMERIC_LITERAL_LENGTH} characters"
        )


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return ""
    if len(raw) >= 2 and raw[0] in {'"', "'"} and raw[-1] == raw[0]:
        try:
            return ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return raw[1:-1]
    if _INT_RE.fullmatch(raw):
        _require_bounded_numeric_literal(raw)
        try:
            return int(raw)
        except (ValueError, OverflowError) as exc:
            raise ResponseParseError("camera response integer literal is invalid") from exc
    if _FLOAT_RE.fullmatch(raw):
        _require_bounded_numeric_literal(raw)
        try:
            value = float(raw)
        except (ValueError, OverflowError) as exc:
            raise ResponseParseError("camera response float literal is invalid") from exc
        if not math.isfinite(value):
            raise ResponseParseError("camera response float literal must be finite")
        return value
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    return raw


def _iter_statements(text: str) -> Iterator[str]:
    """Split on semicolons outside quoted values without executing JavaScript."""

    start = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ";":
            statement = text[start:index].strip()
            if statement:
                yield statement
            start = index + 1
    if quote is not None:
        raise ResponseParseError("camera response contains an unterminated quoted value")
    trailing = text[start:].strip()
    if trailing:
        yield trailing


def parse_vstarcam_response(text: str) -> dict[str, Any]:
    """Parse the camera's bounded assignment grammar with a text fallback."""

    if not isinstance(text, str):
        raise TypeError("response must be text")

    parsed: dict[str, Any] = {}
    arrays: dict[str, dict[int, Any]] = {}
    for statement in _iter_statements(text):
        if match := _ARRAY_DECL_RE.fullmatch(statement):
            name = match.group(1)
            arrays.setdefault(name, {})
            parsed.pop(name, None)
            continue
        if match := _ARRAY_ITEM_RE.fullmatch(statement):
            name, raw_index, raw_value = match.groups()
            _require_bounded_numeric_literal(raw_index, field="array index")
            try:
                index = int(raw_index)
            except (ValueError, OverflowError) as exc:
                raise ResponseParseError("camera response array index is invalid") from exc
            if index > _MAX_ARRAY_INDEX:
                raise ResponseParseError(f"camera response array index exceeds {_MAX_ARRAY_INDEX}")
            arrays.setdefault(name, {})[index] = _parse_value(raw_value)
            parsed.pop(name, None)
            continue
        if match := _ASSIGNMENT_RE.fullmatch(statement):
            name, raw_value = match.groups()
            if name not in arrays:
                parsed[name] = _parse_value(raw_value)

    if sum(max(items, default=-1) + 1 for items in arrays.values()) > _MAX_TOTAL_ARRAY_ITEMS:
        raise ResponseParseError(
            f"camera response total array items exceed {_MAX_TOTAL_ARRAY_ITEMS}"
        )

    for name, items in arrays.items():
        if not items:
            parsed[name] = []
            continue
        values: list[Any] = [None] * (max(items) + 1)
        for index, value in items.items():
            values[index] = value
        parsed[name] = values
    return parsed if parsed else {"raw": text.strip()}
