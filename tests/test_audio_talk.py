from __future__ import annotations

import struct

import pytest

from vstarcamctl.audio_talk import (
    ADPCM_FRAME_DURATION,
    ADPCM_PAYLOAD_SIZE,
    ADPCM_PCM_SIZE,
    G711A_PAYLOAD_SIZE,
    TALK_CODEC_ADPCM,
    TALK_CODEC_G711A,
    TALK_HEADER_MAGIC,
    TALK_HEADER_SIZE,
    TALK_HEADER_VERSION,
    ImaAdpcmState,
    build_adpcm_talk_frame,
    build_adpcm_talk_transcode_command,
    build_g711a_talk_frame,
    build_talk_transcode_command,
)
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import (
    ConfirmationRequiredError,
    ExperimentalCommandError,
    MediaConfigurationError,
    ServiceChangeUncertainError,
)
from vstarcamctl.transport import FakeTransport


def config() -> VStarcamConfig:
    return VStarcamConfig(
        host="192.0.2.10",
        vuid="VE123456",
        username="admin",
        password="camera-secret",
        transport="fake",
        retries=3,
    )


def test_g711a_frame_matches_binary_layout():
    payload = bytes(index & 0xFF for index in range(G711A_PAYLOAD_SIZE))
    frame = build_g711a_talk_frame(payload, 0x10203040)

    assert len(frame) == TALK_HEADER_SIZE + G711A_PAYLOAD_SIZE
    assert struct.unpack_from("<I", frame, 0)[0] == TALK_HEADER_MAGIC
    assert struct.unpack_from("<H", frame, 4)[0] == TALK_HEADER_VERSION
    assert struct.unpack_from("<I", frame, 12)[0] == 0x10203040
    assert struct.unpack_from("<I", frame, 16)[0] == G711A_PAYLOAD_SIZE
    assert frame[27] == TALK_CODEC_G711A
    assert frame[TALK_HEADER_SIZE:] == payload


def test_adpcm_frame_matches_header_state_and_payload_layout():
    frame, state = build_adpcm_talk_frame(
        b"\x00" * ADPCM_PCM_SIZE,
        0x10203040,
        ImaAdpcmState(),
    )

    assert len(frame) == TALK_HEADER_SIZE + ADPCM_PAYLOAD_SIZE
    assert struct.unpack_from("<I", frame, 12)[0] == 0x10203040
    assert struct.unpack_from("<I", frame, 16)[0] == ADPCM_PAYLOAD_SIZE
    assert frame[27] == TALK_CODEC_ADPCM
    assert struct.unpack_from("<hH", frame, 28) == (0, 0)
    assert frame[TALK_HEADER_SIZE:] == b"\x00" * ADPCM_PAYLOAD_SIZE
    assert state == ImaAdpcmState()


def test_adpcm_encoder_matches_regression_vector():
    pcm = b"".join(struct.pack("<h", index * 32 - 8192) for index in range(ADPCM_PCM_SIZE // 2))
    frame, state = build_adpcm_talk_frame(pcm, 7)

    assert state == ImaAdpcmState(predictor=8163, index=16)
    assert frame[28:32].hex() == "e31f1000"
    assert frame[TALK_HEADER_SIZE : TALK_HEADER_SIZE + 32].hex() == (
        "ffffffffb0080808008080080008000800000010010110121122223342342423"
    )


def test_talk_ffmpeg_command_produces_raw_8khz_mono_alaw():
    command = build_talk_transcode_command("default", input_format="pulse", duration=3)
    assert command[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f"]
    assert command[command.index("-ar") + 1] == "8000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-c:a") + 1] == "pcm_alaw"
    assert command[-2:] == ["alaw", "pipe:1"]

    adpcm = build_adpcm_talk_transcode_command("default", input_format="pulse", duration=3)
    assert adpcm[adpcm.index("-c:a") + 1] == "pcm_s16le"
    assert adpcm[-2:] == ["s16le", "pipe:1"]


async def _payloads(*items: bytes):
    for item in items:
        yield item


def _adpcm_frame(sequence: int = 0) -> bytes:
    return build_adpcm_talk_frame(
        b"\x00" * ADPCM_PCM_SIZE,
        sequence,
    )[0]


async def test_talk_requires_both_safety_flags_before_connecting():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.send_talk_audio(_payloads(b"x"))
    with pytest.raises(ConfirmationRequiredError):
        await camera.send_talk_audio(_payloads(b"x"), experimental=True)
    assert transport.connect_count == 0


async def test_half_duplex_talk_wraps_channel_three_in_same_session_livestream():
    transport = FakeTransport(["var firmware_version='test';", "result=0;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.send_talk_audio(
        _payloads(_adpcm_frame(0), _adpcm_frame(1)),
        experimental=True,
        confirm=True,
    )

    assert result["frames_sent"] == 2
    assert result["audio_seconds"] == round(2 * ADPCM_FRAME_DURATION, 3)
    assert result["codec"] == "ima_adpcm"
    assert result["full_duplex"] is False
    assert result["livestream_start_response"] == {"result": 0}
    assert result["livestream_stop_response"] == {"result": 0}
    assert len(transport.channel_writes) == 2
    assert all(channel == 3 for channel, _frame in transport.channel_writes)
    assert len(transport.requests) == 3
    assert "/get_status.cgi?" in transport.requests[0]
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[1]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[2]


async def test_talk_allows_missing_capability_metadata_with_both_safety_flags():
    transport = FakeTransport(["var firmware_version='test';", "result=0;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.send_talk_audio(
        _payloads(_adpcm_frame()),
        experimental=True,
        confirm=True,
    )

    assert result["frames_sent"] == 1


async def test_direct_adpcm_refuses_explicit_full_duplex_capability():
    transport = FakeTransport(["var EchoCancellationVer=2; var support_g711a=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="full-duplex"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []


async def test_half_duplex_talk_ceases_frames_when_audio_source_fails():
    async def failing_payloads():
        yield _adpcm_frame()
        raise RuntimeError("local source failed")

    transport = FakeTransport(["var firmware_version='test';", "result=0;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(RuntimeError, match="local source failed"):
        await camera.send_talk_audio(
            failing_payloads(),
            experimental=True,
            confirm=True,
        )

    assert len(transport.channel_writes) == 1
    assert len(transport.requests) == 3
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[1]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[2]


async def test_talk_attempts_livestream_stop_when_start_ack_is_lost():
    transport = FakeTransport(
        ["var firmware_version='test';", TimeoutError("ack lost"), "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert len(transport.requests) == 3
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[1]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[2]
