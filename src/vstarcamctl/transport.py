"""Transport contract and discovery metadata."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol

_PSK_BY_DEVICE_PREFIX = {
    "VSTG": "vstarcam2018",
    "VSTH": "vstarcam2018",
    "VSTJ": "vstarcam2019",
    "VSTK": "vstarcam2019",
    "VSTL": "vstarcam2019",
    "VSTM": "vstarcam2019",
    "VSTN": "vstarcam2019",
    "VSTP": "vstarcam2019",
    "VSGG": "vstarcam2021",
    "VSGM": "vstarcam2021",
    "VSGS": "vstarcam2021",
}
KNOWN_PSKS = tuple(dict.fromkeys(_PSK_BY_DEVICE_PREFIX.values()))


def psk_for_device_id(device_id: str | None) -> str | None:
    """Return the known transport seed for a PPPP DID prefix."""

    if not device_id or not isinstance(device_id, str):
        return None
    return _PSK_BY_DEVICE_PREFIX.get(device_id[:4])


@dataclass(frozen=True, slots=True)
class DiscoveredCamera:
    host: str
    port: int
    device_id: str
    protocol: str
    encryption: str
    psk: str | None = field(default=None, repr=False)


class CameraTransport(Protocol):
    name: str

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def session_lease(self) -> AbstractAsyncContextManager[None]: ...

    async def request(self, command: str, *, timeout: float) -> str: ...

    async def send_channel_parts(
        self, channel: int, parts: Iterable[bytes], *, timeout: float
    ) -> None: ...

    async def start_video_capture(self) -> None: ...

    async def receive_video_keyframe(self, *, timeout: float) -> bytes: ...

    async def stop_video_capture(self) -> None: ...
