from __future__ import annotations

import struct

import pytest

from vstarcamctl.framing import (
    CGI_HEADER,
    CGI_START_CODE,
    CgiFrameError,
    decode_cgi_frame,
    encode_cgi_request,
    expected_frame_size,
)
from vstarcamctl.transport_aiopppp import AioppppTransport


def test_encode_request_matches_wire_header():
    command = "GET /get_params.cgi?loginuse=admin&user=admin&pwd=test&"
    wire = encode_cgi_request(command)

    assert wire[:8] == struct.pack("<HHHH", 0x0A01, 0, len(command), 0)
    assert wire[8:] == command.encode()


def test_decode_fragmented_get_params_response_with_protocol_flags():
    body = b'var result=0;\r\nvar alias="camera";\r\n'
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6002, len(body), 0x0100) + body

    assert expected_frame_size(wire[:7]) is None
    assert expected_frame_size(wire[:8]) == len(wire)
    frame = decode_cgi_frame(wire + b"next")
    assert frame.command_code == 0x6002
    assert frame.flags == 0x0100
    assert frame.payload == body
    assert frame.trailing == b"next"
    assert AioppppTransport._decode_response([wire[:5], wire[5:]]) == (
        'var result=0;\r\nvar alias="camera";'
    )


def test_decode_rejects_bad_or_incomplete_frame():
    with pytest.raises(CgiFrameError, match="start code"):
        expected_frame_size(struct.pack("<HHHH", 0x1234, 0x6002, 0, 0))
    with pytest.raises(CgiFrameError, match="incomplete"):
        decode_cgi_frame(CGI_HEADER.pack(CGI_START_CODE, 0x6002, 10, 0x0100) + b"short")


def test_encode_rejects_payload_larger_than_16_bit_length():
    with pytest.raises(CgiFrameError, match="16-bit"):
        encode_cgi_request("GET /" + ("x" * 0x10000))
