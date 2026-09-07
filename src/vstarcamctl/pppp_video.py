"""Bounded reassembly for one PPPP video key access unit."""

from __future__ import annotations

import struct

from .errors import TransportError

VIDEO_BOUNDARY_MAGIC = b"\x55\xaa\x15\xa8"
VIDEO_BOUNDARY_HEADER_SIZE = 32
MAX_VIDEO_AU_SIZE = 16 * 1024 * 1024


class _PpppVideoFrameError(TransportError):
    """A malformed or unsafe PPPP video access unit."""


class PpppVideoKeyframeAssembler:
    """Keep at most one AU and return the first complete keyframe."""

    def __init__(self) -> None:
        self._latest_index = -1
        self._next_index: int | None = None
        self._frame_type: int | None = None
        self._declared_size = 0
        self._received_size = 0
        self._payload: bytearray | None = None

    def _drop_current(self) -> None:
        self._next_index = None
        self._frame_type = None
        self._declared_size = 0
        self._received_size = 0
        self._payload = None

    def _reject(self, absolute_index: int, message: str) -> None:
        self._latest_index = max(self._latest_index, absolute_index)
        self._drop_current()
        raise _PpppVideoFrameError(message)

    def _finish_if_complete(self) -> bytes | None:
        if self._received_size != self._declared_size:
            return None
        keyframe = bytes(self._payload) if self._frame_type == 0 else None
        self._drop_current()
        return keyframe

    def feed(self, absolute_index: int, payload: bytes) -> bytes | None:
        """Consume one ordered DRW video chunk."""

        if (
            not isinstance(absolute_index, int)
            or isinstance(absolute_index, bool)
            or absolute_index < 0
        ):
            raise _PpppVideoFrameError("PPPP video chunk index must be a non-negative integer")
        if not isinstance(payload, bytes):
            self._reject(absolute_index, "PPPP video chunk payload must be bytes")

        # Retransmits and late chunks must not reset or enlarge the current AU.
        if absolute_index <= self._latest_index:
            return None

        if payload.startswith(VIDEO_BOUNDARY_MAGIC):
            self._latest_index = absolute_index
            self._drop_current()
            if len(payload) < VIDEO_BOUNDARY_HEADER_SIZE:
                self._reject(absolute_index, "PPPP video boundary header is incomplete")

            frame_type = struct.unpack_from("<H", payload, 4)[0]
            declared_size = struct.unpack_from("<I", payload, 16)[0]
            if frame_type not in (0, 1):
                self._reject(absolute_index, "PPPP video frame type is unsupported")
            if not 0 < declared_size <= MAX_VIDEO_AU_SIZE:
                self._reject(absolute_index, "PPPP video AU has an unsafe declared size")

            body = memoryview(payload)[VIDEO_BOUNDARY_HEADER_SIZE:]
            if len(body) > declared_size:
                self._reject(absolute_index, "PPPP video boundary exceeds its declared AU size")

            self._frame_type = frame_type
            self._declared_size = declared_size
            self._received_size = len(body)
            self._payload = bytearray(body) if frame_type == 0 else None
            self._next_index = absolute_index + 1
            return self._finish_if_complete()

        if self._next_index is None:
            self._latest_index = absolute_index
            return None
        if absolute_index != self._next_index:
            # A missing fragment makes the current AU unusable. Wait for the
            # next boundary instead of retaining a partial frame.
            self._latest_index = absolute_index
            self._drop_current()
            return None

        self._latest_index = absolute_index
        received_size = self._received_size + len(payload)
        if received_size > self._declared_size:
            self._reject(absolute_index, "PPPP video AU exceeds its declared size")
        if self._payload is not None:
            self._payload.extend(payload)
        self._received_size = received_size
        self._next_index = absolute_index + 1
        return self._finish_if_complete()
