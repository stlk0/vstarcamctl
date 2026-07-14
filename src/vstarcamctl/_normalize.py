"""Shared value coercion and validation helpers.

These centralize the small routines that were previously copied across the
media, detection, night-vision, and time modules. Each caller passes the
exception type to raise so error messages stay domain-specific.
"""

from __future__ import annotations

from typing import Any, Callable

ErrorFactory = Callable[[str], Exception]


def as_flag(value: Any, field: str, error: ErrorFactory) -> bool:
    """Coerce a camera 0/1 (or bool) field into a Python bool."""

    if value in (0, "0", False):
        return False
    if value in (1, "1", True):
        return True
    raise error(f"{field} must be 0 or 1")


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


def as_int_in_range(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
    error: ErrorFactory,
) -> int:
    """Coerce an integer-valued field and require ``minimum <= value <= maximum``."""

    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise error(f"{field} must be an integer") from exc
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise error(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def capability_enabled(value: Any) -> bool:
    """Interpret a capability field as enabled when it is a positive integer."""

    if isinstance(value, bool):
        return value
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def has_control_chars(text: str) -> bool:
    """Return True if the text contains ASCII control characters or DEL."""

    return any(ord(char) < 32 or ord(char) == 127 for char in text)
