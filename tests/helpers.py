import asyncio
import math
from collections import deque
from collections.abc import Iterable
from contextlib import asynccontextmanager

from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import TransportError, TransportTimeoutError


def camera_config(**overrides) -> VStarcamConfig:
    values = {
        "host": "192.0.2.10",
        "device_id": "VSTG-000001-AAAAA",
        "username": "admin",
        "password": "camera-secret",
        "retries": 3,
    }
    return VStarcamConfig(**(values | overrides))


class FakeTransport:
    """A deterministic transport that records exact camera I/O."""

    name = "fake"

    def __init__(self, responses: list[str | BaseException] | None = None):
        self.responses = deque(responses or [])
        self.requests: list[str] = []
        self.channel_writes: list[tuple[int, bytes]] = []
        self.channel_batches: list[tuple[int, tuple[bytes, ...]]] = []
        self.connect_count = 0
        self.close_count = 0
        self._connected = False
        self._session_lease_lock = asyncio.Lock()
        self._video_keyframe_future: asyncio.Future[bytes | TransportError] | None = None
        self._video_capture_waiting = False
        self._video_capture_delivered = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        async with self._session_lease_lock:
            self.connect_count += 1
            self._connected = True

    async def close(self) -> None:
        async with self._session_lease_lock:
            self.close_count += 1
            self._abort_video_capture("fake transport closed during video capture")
            self._video_keyframe_future = None
            self._video_capture_waiting = False
            self._video_capture_delivered = False
            self._connected = False

    @asynccontextmanager
    async def session_lease(self):
        async with self._session_lease_lock:
            if not self.connected:
                raise TransportError("fake transport is not connected")
            yield

    async def request(self, command: str, *, timeout: float) -> str:
        if not self.connected:
            raise RuntimeError("fake transport is not connected")
        self.requests.append(command)
        if not self.responses:
            raise AssertionError("unexpected fake transport request with no queued response")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    async def send_channel_parts(
        self, channel: int, parts: Iterable[bytes], *, timeout: float
    ) -> None:
        if not self.connected:
            raise RuntimeError("fake transport is not connected")
        batch = tuple(parts)
        self.channel_batches.append((channel, batch))
        self.channel_writes.extend((channel, part) for part in batch)

    def _abort_video_capture(self, message: str) -> None:
        future = self._video_keyframe_future
        if future is not None and not future.done():
            future.set_result(TransportError(message))

    async def start_video_capture(self) -> None:
        if not self.connected:
            raise TransportError("fake transport is not connected")
        if self._video_keyframe_future is not None:
            raise TransportError("PPPP video capture is already active")
        self._video_keyframe_future = asyncio.get_running_loop().create_future()
        self._video_capture_waiting = False
        self._video_capture_delivered = False

    async def receive_video_keyframe(self, *, timeout: float) -> bytes:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise TransportError("PPPP video capture timeout must be a finite positive number")
        future = self._video_keyframe_future
        if future is None:
            raise TransportError("PPPP video capture is not active")
        if self._video_capture_delivered:
            raise TransportError("PPPP video keyframe was already delivered")
        if self._video_capture_waiting:
            raise TransportError("PPPP video capture already has a receiver")

        self._video_capture_waiting = True
        delivered = False
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TransportTimeoutError("camera did not provide a PPPP video keyframe") from exc
            if isinstance(result, TransportError):
                raise result
            if not self.connected or self._video_keyframe_future is not future:
                raise TransportError("PPPP session changed during video capture")
            delivered = True
            return result
        finally:
            if self._video_keyframe_future is future:
                self._video_capture_waiting = False
                self._video_capture_delivered = delivered

    async def stop_video_capture(self) -> None:
        self._abort_video_capture("PPPP video capture stopped")
        self._video_keyframe_future = None
        self._video_capture_waiting = False
        self._video_capture_delivered = False
