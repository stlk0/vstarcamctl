"""Transport contract plus a deterministic fake used by tests and development."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DiscoveredCamera:
    host: str
    port: int
    vuid: str
    protocol: str
    encryption: str


@runtime_checkable
class CameraTransport(Protocol):
    name: str

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def request(self, command: str, *, timeout: float) -> str: ...

    async def send_channel_data(self, channel: int, payload: bytes, *, timeout: float) -> None: ...


class FakeTransport:
    """A fake transport that records the exact PPPP channel command string."""

    name = "fake"

    def __init__(self, responses: list[str | BaseException] | None = None):
        self.responses = deque(responses or [])
        self.requests: list[str] = []
        self.channel_writes: list[tuple[int, bytes]] = []
        self.connect_count = 0
        self.close_count = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_count += 1
        self._connected = True

    async def close(self) -> None:
        self.close_count += 1
        self._connected = False

    async def request(self, command: str, *, timeout: float) -> str:
        if not self.connected:
            raise RuntimeError("fake transport is not connected")
        self.requests.append(command)
        if not self.responses:
            return "result=0;"
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    async def send_channel_data(self, channel: int, payload: bytes, *, timeout: float) -> None:
        if not self.connected:
            raise RuntimeError("fake transport is not connected")
        self.channel_writes.append((channel, payload))
