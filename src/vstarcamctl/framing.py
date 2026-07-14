"""Legacy VStarcam CGI framing carried inside PPPP command-channel DRW packets."""

from __future__ import annotations

import struct
from dataclasses import dataclass

CGI_START_CODE = 0x0A01
CGI_REQUEST_CODE = 0x0000
CGI_HEADER = struct.Struct("<HHHH")


class CgiFrameError(ValueError):
    """A CGI application frame is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class CgiFrame:
    command_code: int
    payload: bytes
    flags: int = 0
    trailing: bytes = b""


def encode_cgi_request(command: str) -> bytes:
    """Wrap an exact ``GET /...&`` command in the protocol's 8-byte header."""

    if not command.startswith("GET /"):
        raise CgiFrameError("CGI request must begin with 'GET /'")
    payload = command.encode("utf-8")
    if len(payload) > 0xFFFF:
        raise CgiFrameError("CGI request exceeds the protocol's 16-bit payload length")
    return CGI_HEADER.pack(CGI_START_CODE, CGI_REQUEST_CODE, len(payload), 0) + payload


def expected_frame_size(data: bytes | bytearray) -> int | None:
    """Return the complete frame size once its header is available."""

    if len(data) < CGI_HEADER.size:
        return None
    start_code, _command_code, payload_size, _flags = CGI_HEADER.unpack_from(data)
    if start_code != CGI_START_CODE:
        raise CgiFrameError(f"unexpected CGI start code 0x{start_code:04x}")
    return CGI_HEADER.size + payload_size


def decode_cgi_frame(data: bytes) -> CgiFrame:
    """Decode one complete frame and retain any following bytes."""

    size = expected_frame_size(data)
    if size is None:
        raise CgiFrameError("CGI frame header is incomplete")
    if len(data) < size:
        raise CgiFrameError(f"CGI frame is incomplete: expected {size} bytes, got {len(data)}")
    _start_code, command_code, _payload_size, flags = CGI_HEADER.unpack_from(data)
    return CgiFrame(
        command_code=command_code,
        payload=data[CGI_HEADER.size : size],
        flags=flags,
        trailing=data[size:],
    )
