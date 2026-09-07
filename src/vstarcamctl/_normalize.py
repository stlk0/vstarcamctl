"""Shared value coercion and validation helpers.

These centralize the small routines that were previously copied across the
media, detection, night-vision, and time modules. Each caller passes the
exception type to raise so error messages stay domain-specific.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Callable

ErrorFactory = Callable[[str], Exception]


def as_flag(value: Any, field: str, error: ErrorFactory) -> bool:
    """Coerce a camera 0/1 (or bool) field into a Python bool."""

    if value in (0, "0", False):
        return False
    if value in (1, "1", True):
        return True
    raise error(f"{field} must be 0 or 1")


def as_exact_flag(value: Any, field: str, error: ErrorFactory) -> bool:
    """Parse an exact wire 0/1 echo without Python numeric lookalikes."""

    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    raise error(f"{field} must be an exact 0 or 1")


def as_int(value: Any, field: str, error: ErrorFactory) -> int:
    """Coerce an integer-valued field, rejecting bool and non-numeric text."""

    if isinstance(value, bool):
        raise error(f"{field} must be an integer")
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise error(f"{field} must be an integer") from exc
    if not isinstance(value, int):
        raise error(f"{field} must be an integer")
    return value


def require_positive_finite(value: Any, field: str, error: ErrorFactory) -> None:
    """Require a real built-in number that is finite and greater than zero."""

    if type(value) not in (int, float):
        raise error(f"{field} must be finite and greater than zero")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise error(f"{field} must be finite and greater than zero") from exc
    if not finite or value <= 0:
        raise error(f"{field} must be finite and greater than zero")


def as_int_in_range(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
    error: ErrorFactory,
) -> int:
    """Coerce an integer-valued field and require ``minimum <= value <= maximum``."""

    value = as_int(value, field, error)
    if not minimum <= value <= maximum:
        raise error(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def require_zero_result(payload: Any, response: str, error: ErrorFactory) -> None:
    """Require a response mapping with an integer-valued zero result."""

    if not isinstance(payload, Mapping):
        raise error(f"{response} response must be a mapping")
    if as_int(payload.get("result"), "result", error) != 0:
        raise error(f"{response} response result must be 0")


def has_control_chars(text: str) -> bool:
    """Return True if the text contains ASCII control characters or DEL."""

    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def require_safe_text(value: Any, field: str, error: ErrorFactory) -> None:
    """Require text without control characters."""

    if not isinstance(value, str):
        raise error(f"{field} must be text")
    if has_control_chars(value):
        raise error(f"{field} contains control characters")
