"""Internal evaluation of catalog capability aliases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

CapabilityState = Literal["supported", "unsupported", "unknown"]


def _reported_state(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return None if number < 0 else number > 0


def capability_state(status: Mapping[str, object], aliases: tuple[str, ...]) -> CapabilityState:
    """Evaluate aliases without treating missing or invalid values as false."""

    states = tuple(_reported_state(status.get(alias)) for alias in aliases)
    if any(state is True for state in states):
        return "supported"
    if any(state is False for state in states):
        return "unsupported"
    return "unknown"
