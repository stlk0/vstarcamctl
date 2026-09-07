import struct

import pytest

from vstarcamctl.errors import TransportError
from vstarcamctl.pppp_video import (
    MAX_VIDEO_AU_SIZE,
    VIDEO_BOUNDARY_HEADER_SIZE,
    VIDEO_BOUNDARY_MAGIC,
    PpppVideoKeyframeAssembler,
)


def boundary(frame_type: int, body: bytes, *, declared_size: int | None = None) -> bytes:
    header = bytearray(VIDEO_BOUNDARY_HEADER_SIZE)
    header[:4] = VIDEO_BOUNDARY_MAGIC
    struct.pack_into("<H", header, 4, frame_type)
    struct.pack_into("<I", header, 16, declared_size if declared_size is not None else len(body))
    return bytes(header) + body


def test_reassembles_split_key_access_unit():
    assembler = PpppVideoKeyframeAssembler()

    assert assembler.feed(10, boundary(0, b"key", declared_size=9)) is None
    assert assembler.feed(11, b"-frame") == b"key-frame"


def test_discards_complete_p_frame_before_keyframe():
    assembler = PpppVideoKeyframeAssembler()

    assert assembler.feed(1, boundary(1, b"predicted")) is None
    assert assembler.feed(2, boundary(0, b"key")) == b"key"


def test_gap_drops_partial_access_unit_until_next_boundary():
    assembler = PpppVideoKeyframeAssembler()

    assert assembler.feed(20, boundary(0, b"part", declared_size=8)) is None
    assert assembler.feed(22, b"lost-gap") is None
    assert assembler.feed(23, b"tail") is None
    assert assembler.feed(24, boundary(0, b"fresh")) == b"fresh"


def test_absolute_indexes_cross_epoch_rollover():
    assembler = PpppVideoKeyframeAssembler()

    assert assembler.feed(0xFFFF, boundary(0, b"roll", declared_size=8)) is None
    assert assembler.feed(0x10000, b"over") == b"rollover"


def test_duplicate_and_old_chunks_do_not_inflate_access_unit():
    assembler = PpppVideoKeyframeAssembler()
    first = boundary(0, b"abc", declared_size=6)

    assert assembler.feed(100, first) is None
    assert assembler.feed(100, first) is None
    assert assembler.feed(99, b"old") is None
    assert assembler.feed(101, b"def") == b"abcdef"


@pytest.mark.parametrize(
    "payload",
    [
        boundary(0, b"", declared_size=0),
        boundary(0, b"", declared_size=MAX_VIDEO_AU_SIZE + 1),
        boundary(9, b"x"),
        VIDEO_BOUNDARY_MAGIC,
        boundary(0, b"too-long", declared_size=1),
    ],
)
def test_rejects_malformed_or_oversized_boundary(payload):
    assembler = PpppVideoKeyframeAssembler()

    with pytest.raises(TransportError):
        assembler.feed(1, payload)


def test_rejects_continuation_past_declared_size():
    assembler = PpppVideoKeyframeAssembler()
    assert assembler.feed(1, boundary(0, b"a", declared_size=2)) is None

    with pytest.raises(TransportError, match="exceeds"):
        assembler.feed(2, b"bc")


def test_long_gop_has_no_frame_boundary_limit():
    assembler = PpppVideoKeyframeAssembler()

    for index in range(100):
        assert assembler.feed(index, boundary(1, b"p")) is None
    assert assembler.feed(100, boundary(0, b"key")) == b"key"
