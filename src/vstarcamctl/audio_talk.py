"""Experimental G.711/ADPCM framing for the PPPP talk channel."""

from __future__ import annotations

import asyncio
import math
import shutil
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ._normalize import has_control_chars
from ._process import stop_process
from .errors import MediaConfigurationError, MediaStreamError, MediaToolUnavailableError

TALK_CHANNEL = 3
TALK_SAMPLE_RATE = 8_000
TALK_HEADER_SIZE = 32
TALK_HEADER_MAGIC = 0xA815AA55
TALK_HEADER_VERSION = 0x0108
TALK_CODEC_G711A = 7
TALK_CODEC_ADPCM = 0
G711A_PAYLOAD_SIZE = 640
ADPCM_PCM_SIZE = 1_024
ADPCM_PAYLOAD_SIZE = ADPCM_PCM_SIZE // 4
ADPCM_FRAME_DURATION = (ADPCM_PCM_SIZE // 2) / TALK_SAMPLE_RATE

_ADPCM_INDEX_ADJUST = (-1, -1, -1, -1, 2, 4, 6, 8)
_ADPCM_STEP_TABLE = (
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
)


@dataclass(frozen=True, slots=True)
class ImaAdpcmState:
    predictor: int = 0
    index: int = 0


def build_g711a_talk_frame(payload: bytes, sequence: int) -> bytes:
    """Wrap up to one 80 ms G.711 A-law block in its 32-byte header."""

    if not isinstance(payload, bytes) or not 1 <= len(payload) <= G711A_PAYLOAD_SIZE:
        raise MediaConfigurationError(
            f"talk payload must contain 1 to {G711A_PAYLOAD_SIZE} G.711 A-law bytes"
        )
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 0 <= sequence <= 0xFFFFFFFF
    ):
        raise MediaConfigurationError("talk sequence must be an unsigned 32-bit integer")

    header = bytearray(TALK_HEADER_SIZE)
    struct.pack_into("<I", header, 0, TALK_HEADER_MAGIC)
    struct.pack_into("<H", header, 4, TALK_HEADER_VERSION)
    struct.pack_into("<I", header, 12, sequence)
    struct.pack_into("<I", header, 16, len(payload))
    header[27] = TALK_CODEC_G711A
    return bytes(header) + payload


def encode_ima_adpcm(
    pcm: bytes, state: ImaAdpcmState = ImaAdpcmState()
) -> tuple[bytes, ImaAdpcmState]:
    """Encode little-endian PCM with the camera-compatible IMA variant."""

    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 4:
        raise MediaConfigurationError(
            "ADPCM input must contain a positive even number of 16-bit sample pairs"
        )
    if not -32768 <= state.predictor <= 32767 or not 0 <= state.index <= 88:
        raise MediaConfigurationError("ADPCM state is outside the codec range")

    predictor = state.predictor
    index = state.index
    encoded = bytearray()
    for position, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
        step = _ADPCM_STEP_TABLE[index]
        difference = sample - predictor
        sign = 8 if difference < 0 else 0
        magnitude = min(7, (abs(difference) * 4) // step)
        delta = (magnitude * step) // 4 + step // 8
        predictor += -delta if sign else delta
        predictor = ((predictor + 0x8000) & 0xFFFF) - 0x8000
        index = max(0, min(88, index + _ADPCM_INDEX_ADJUST[magnitude]))
        nibble = sign | magnitude
        if position % 2 == 0:
            encoded.append(nibble << 4)
        else:
            encoded[-1] |= nibble
    return bytes(encoded), ImaAdpcmState(predictor=predictor, index=index)


def build_adpcm_talk_frame(
    pcm: bytes,
    sequence: int,
    state: ImaAdpcmState = ImaAdpcmState(),
) -> tuple[bytes, ImaAdpcmState]:
    if len(pcm) != ADPCM_PCM_SIZE:
        raise MediaConfigurationError(
            f"ADPCM talk PCM frame must contain exactly {ADPCM_PCM_SIZE} bytes"
        )
    payload, next_state = encode_ima_adpcm(pcm, state)
    header = bytearray(TALK_HEADER_SIZE)
    struct.pack_into("<I", header, 0, TALK_HEADER_MAGIC)
    struct.pack_into("<H", header, 4, TALK_HEADER_VERSION)
    struct.pack_into("<I", header, 12, sequence & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, len(payload))
    header[27] = TALK_CODEC_ADPCM
    # The camera frame stores the post-encode state here.
    struct.pack_into("<hH", header, 28, next_state.predictor, next_state.index)
    return bytes(header) + payload, next_state


def validate_adpcm_talk_frame(frame: bytes) -> None:
    if not isinstance(frame, bytes) or len(frame) != TALK_HEADER_SIZE + ADPCM_PAYLOAD_SIZE:
        raise MediaConfigurationError("invalid ADPCM talk frame size")
    if struct.unpack_from("<I", frame, 0)[0] != TALK_HEADER_MAGIC:
        raise MediaConfigurationError("invalid ADPCM talk frame magic")
    if struct.unpack_from("<H", frame, 4)[0] != TALK_HEADER_VERSION:
        raise MediaConfigurationError("invalid ADPCM talk frame version")
    if struct.unpack_from("<I", frame, 16)[0] != ADPCM_PAYLOAD_SIZE:
        raise MediaConfigurationError("invalid ADPCM talk payload length")
    if frame[27] != TALK_CODEC_ADPCM:
        raise MediaConfigurationError("talk frame is not ADPCM")


def build_talk_transcode_command(
    source: str | Path,
    *,
    input_format: str | None = None,
    duration: float | None = None,
    executable: str = "ffmpeg",
) -> list[str]:
    """Build a shell-free FFmpeg command producing raw 8 kHz mono A-law."""

    source_text = str(source)
    if not source_text or has_control_chars(source_text):
        raise MediaConfigurationError(
            "talk input must be non-empty and contain no control characters"
        )
    if input_format is not None and (
        not input_format or not all(char.isalnum() or char in "_-" for char in input_format)
    ):
        raise MediaConfigurationError("talk input format contains unsupported characters")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise MediaConfigurationError("talk duration must be finite and greater than zero")

    command = [executable, "-hide_banner", "-loglevel", "error"]
    if input_format is not None:
        command.extend(("-f", input_format))
    command.extend(("-i", source_text))
    if duration is not None:
        command.extend(("-t", str(duration)))
    command.extend(
        (
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(TALK_SAMPLE_RATE),
            "-c:a",
            "pcm_alaw",
            "-f",
            "alaw",
            "pipe:1",
        )
    )
    return command


def build_adpcm_talk_transcode_command(
    source: str | Path,
    *,
    input_format: str | None = None,
    duration: float | None = None,
    executable: str = "ffmpeg",
) -> list[str]:
    command = build_talk_transcode_command(
        source,
        input_format=input_format,
        duration=duration,
        executable=executable,
    )
    codec_index = command.index("pcm_alaw")
    command[codec_index] = "pcm_s16le"
    format_index = command.index("alaw", codec_index)
    command[format_index] = "s16le"
    return command


async def iter_g711a_talk_payloads(
    source: str | Path,
    *,
    input_format: str | None = None,
    duration: float | None = None,
    executable: str = "ffmpeg",
) -> AsyncIterator[bytes]:
    """Transcode input and yield camera-sized 80 ms G.711 A-law chunks.

    FFmpeg stderr is intentionally never included in errors because it may echo
    a local device or file name supplied by the user.
    """

    command = build_talk_transcode_command(
        source,
        input_format=input_format,
        duration=duration,
        executable=executable,
    )
    if shutil.which(executable) is None:
        raise MediaToolUnavailableError(
            f"{executable} is required for talk audio but was not found"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MediaToolUnavailableError(f"cannot start required media tool {executable}") from exc

    assert process.stdout is not None
    try:
        while True:
            reached_eof = False
            try:
                payload = await process.stdout.readexactly(G711A_PAYLOAD_SIZE)
            except asyncio.IncompleteReadError as exc:
                payload = exc.partial
                reached_eof = True
                if not payload:
                    break
            if len(payload) < G711A_PAYLOAD_SIZE:
                # A-law silence is 0xd5. Padding keeps the camera cadence stable.
                payload += bytes((0xD5,)) * (G711A_PAYLOAD_SIZE - len(payload))
            yield payload
            if reached_eof:
                break

        returncode = await process.wait()
        if returncode != 0:
            raise MediaStreamError(f"ffmpeg could not encode talk audio (exit {returncode})")
    finally:
        await stop_process(process)


async def iter_adpcm_talk_frames(
    source: str | Path,
    *,
    input_format: str | None = None,
    duration: float | None = None,
    executable: str = "ffmpeg",
) -> AsyncIterator[bytes]:
    """Yield camera-compatible half-duplex ADPCM frames without CGI start."""

    command = build_adpcm_talk_transcode_command(
        source,
        input_format=input_format,
        duration=duration,
        executable=executable,
    )
    if shutil.which(executable) is None:
        raise MediaToolUnavailableError(
            f"{executable} is required for talk audio but was not found"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MediaToolUnavailableError(f"cannot start required media tool {executable}") from exc

    assert process.stdout is not None
    state = ImaAdpcmState()
    sequence = 0
    try:
        while True:
            reached_eof = False
            try:
                pcm = await process.stdout.readexactly(ADPCM_PCM_SIZE)
            except asyncio.IncompleteReadError as exc:
                pcm = exc.partial
                reached_eof = True
                if not pcm:
                    break
            if len(pcm) % 2:
                pcm += b"\x00"
            pcm += b"\x00" * (ADPCM_PCM_SIZE - len(pcm))
            frame, state = build_adpcm_talk_frame(pcm, sequence, state)
            yield frame
            sequence = (sequence + 1) & 0xFFFFFFFF
            if reached_eof:
                break

        returncode = await process.wait()
        if returncode != 0:
            raise MediaStreamError(f"ffmpeg could not prepare ADPCM talk audio (exit {returncode})")
    finally:
        await stop_process(process)
