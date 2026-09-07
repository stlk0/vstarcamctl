"""Safe RTSP video/audio access through local FFmpeg tools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import quote

from ._normalize import has_control_chars, require_positive_finite, require_safe_text
from ._process import start_media_process, stop_process
from .errors import MediaConfigurationError, MediaStreamError

StreamQuality = Literal["main", "sub"]
RTSPTransport = Literal["tcp", "udp"]
MediaSelection = Literal["both", "video", "audio"]

_STREAM_INDEX = {"main": 0, "sub": 1}


def _format_host(host: str) -> str:
    if not isinstance(host, str) or not host or host != host.strip():
        raise MediaConfigurationError(
            "RTSP host must be non-empty text without surrounding whitespace"
        )
    if has_control_chars(host):
        raise MediaConfigurationError("RTSP host contains control characters")
    if any(char in host for char in "/@?#[]"):
        raise MediaConfigurationError("RTSP host contains URI delimiters")
    if ":" in host:
        # A colon is accepted only as part of an IPv6 literal. Brackets are
        # added here so callers cannot smuggle URI user-info through the host.
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise MediaConfigurationError("RTSP host is not a valid IPv6 literal") from exc
        return f"[{host}]"
    return host


@dataclass(frozen=True, slots=True)
class RTSPStream:
    """Connection details for one VStarcam RTSP main or sub stream.

    Credential fields are excluded from ``repr``. The full ``url`` property is
    intended only for passing directly to a media library or process.
    """

    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    quality: StreamQuality = "main"
    transport: RTSPTransport = "tcp"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise MediaConfigurationError("RTSP port must be an integer from 1 to 65535")
        _format_host(self.host)
        if self.quality not in _STREAM_INDEX:
            raise MediaConfigurationError("RTSP stream quality must be main or sub")
        if self.transport not in ("tcp", "udp"):
            raise MediaConfigurationError("RTSP transport must be tcp or udp")
        if (self.username is None) != (self.password is None):
            raise MediaConfigurationError("RTSP username and password must be provided together")
        if self.username is not None:
            require_safe_text(self.username, "RTSP username", MediaConfigurationError)
            assert self.password is not None
            require_safe_text(self.password, "RTSP password", MediaConfigurationError)

    @property
    def url(self) -> str:
        host = _format_host(self.host)
        user_info = ""
        if self.username is not None and self.password is not None:
            user_info = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        stream_index = _STREAM_INDEX[self.quality]
        return f"rtsp://{user_info}{host}:{self.port}/{self.transport}/av0_{stream_index}"

    @property
    def redacted_url(self) -> str:
        host = _format_host(self.host)
        user_info = "***:***@" if self.username is not None else ""
        stream_index = _STREAM_INDEX[self.quality]
        return f"rtsp://{user_info}{host}:{self.port}/{self.transport}/av0_{stream_index}"


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    error_category: str | None = None


def _classify_media_error(stderr: bytes) -> str | None:
    message = stderr.lower()
    if b"401 unauthorized" in message:
        return "authentication rejected"
    if b"connection refused" in message:
        return "connection refused"
    if b"404 not found" in message:
        return "stream path not found"
    if b"invalid data found" in message:
        return "invalid stream data"
    return None


def _media_failure(action: str, result: _ProcessResult) -> MediaStreamError:
    category = f": {result.error_category}" if result.error_category else ""
    return MediaStreamError(f"{action}{category} (exit {result.returncode})")


async def _run_process(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    input_data: bytes | None = None,
    timeout_context: str = "reading the RTSP stream",
) -> _ProcessResult:
    executable = command[0]
    if input_data is None:
        process = await start_media_process(command, capture_stderr=True)
    else:
        process = await start_media_process(command, capture_stderr=True, pipe_stdin=True)

    try:
        communicate = (
            process.communicate() if input_data is None else process.communicate(input=input_data)
        )
        stdout, _stderr = await asyncio.wait_for(communicate, timeout=timeout)
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    except asyncio.TimeoutError as exc:
        await stop_process(process)
        raise MediaStreamError(f"{executable} timed out while {timeout_context}") from exc
    return _ProcessResult(
        process.returncode or 0,
        stdout,
        _classify_media_error(_stderr or b""),
    )


def build_probe_command(stream: RTSPStream, *, executable: str = "ffprobe") -> list[str]:
    return [
        executable,
        "-v",
        "error",
        "-rtsp_transport",
        stream.transport,
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
            "sample_rate,channels,channel_layout:format=format_name,duration"
        ),
        "-of",
        "json",
        stream.url,
    ]


def _parse_rate(value: Any) -> float | None:
    if not isinstance(value, str) or value in ("0/0", "N/A"):
        return None
    try:
        return round(float(Fraction(value)), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _normalize_probe(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise MediaStreamError("ffprobe returned malformed stream metadata")
    streams: list[dict[str, Any]] = []
    for source in payload["streams"]:
        if not isinstance(source, dict):
            continue
        kind = source.get("codec_type")
        if kind not in ("video", "audio"):
            continue
        item: dict[str, Any] = {
            "index": source.get("index"),
            "type": kind,
            "codec": source.get("codec_name"),
        }
        if kind == "video":
            item.update(
                {
                    "width": source.get("width"),
                    "height": source.get("height"),
                    "fps": _parse_rate(source.get("avg_frame_rate")),
                }
            )
        else:
            item.update(
                {
                    "sample_rate": source.get("sample_rate"),
                    "channels": source.get("channels"),
                    "channel_layout": source.get("channel_layout"),
                }
            )
        streams.append(item)

    format_payload = payload.get("format")
    result: dict[str, Any] = {"streams": streams}
    if isinstance(format_payload, dict):
        result["format"] = format_payload.get("format_name")
        duration = format_payload.get("duration")
        if isinstance(duration, str):
            try:
                result["duration"] = float(duration)
            except ValueError:
                pass
    return result


async def probe_rtsp(
    stream: RTSPStream,
    *,
    timeout: float = 10.0,
    executable: str = "ffprobe",
) -> dict[str, Any]:
    """Return sanitized codec metadata for the selected RTSP stream."""

    require_positive_finite(timeout, "media probe timeout", MediaConfigurationError)
    result = await _run_process(build_probe_command(stream, executable=executable), timeout=timeout)
    if result.returncode != 0:
        raise _media_failure("ffprobe could not read the RTSP stream", result)
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaStreamError("ffprobe returned invalid JSON metadata") from exc
    return _normalize_probe(payload)


def prepare_media_output(output: str | Path, *, overwrite: bool = False) -> Path:
    """Validate a media destination before any camera stream is started."""

    path = Path(output).expanduser().absolute()
    if not path.parent.is_dir():
        raise MediaConfigurationError(
            f"output directory does not exist or is not a directory: {path.parent}"
        )
    if path.is_dir():
        raise MediaConfigurationError(f"output must be a file, not a directory: {path}")
    if path.exists() and not overwrite:
        raise MediaConfigurationError(
            f"output already exists: {path}; pass overwrite=True or --overwrite"
        )
    return path


def _output_metadata(path: Path, kind: str) -> dict[str, Any]:
    size = path.stat().st_size if path.is_file() else 0
    if not size:
        raise MediaStreamError(f"ffmpeg completed without producing a non-empty {kind}")
    return {"path": str(path.resolve()), "bytes_written": size}


def build_record_command(
    stream: RTSPStream,
    output: str | Path,
    *,
    duration: float,
    media: MediaSelection = "both",
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> list[str]:
    require_positive_finite(duration, "recording duration", MediaConfigurationError)
    if media not in ("both", "video", "audio"):
        raise MediaConfigurationError("media selection must be both, video, or audio")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        stream.transport,
        "-i",
        stream.url,
        "-t",
        str(duration),
    ]
    if media in ("both", "video"):
        command.extend(("-map", "0:v:0?"))
    if media in ("both", "audio"):
        command.extend(("-map", "0:a:0?"))
    command.extend(("-c", "copy", "-y" if overwrite else "-n", str(output)))
    return command


async def record_rtsp(
    stream: RTSPStream,
    output: str | Path,
    *,
    duration: float,
    media: MediaSelection = "both",
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> dict[str, Any]:
    """Remux video/audio from RTSP into a local file without transcoding."""

    path = prepare_media_output(output, overwrite=overwrite)
    command = build_record_command(
        stream,
        path,
        duration=duration,
        media=media,
        overwrite=overwrite,
        executable=executable,
    )
    result = await _run_process(command, timeout=duration + max(15.0, duration * 0.1))
    if result.returncode != 0:
        raise _media_failure("ffmpeg could not record the RTSP stream", result)
    return {
        **_output_metadata(path, "recording"),
        "duration": duration,
        "media": media,
        "quality": stream.quality,
    }


def build_play_command(
    stream: RTSPStream,
    *,
    media: MediaSelection = "audio",
    duration: float | None = None,
    volume: int = 100,
    executable: str = "ffplay",
) -> list[str]:
    """Build an interactive low-latency ffplay command without a shell."""

    if media not in ("both", "video", "audio"):
        raise MediaConfigurationError("playback media must be both, video, or audio")
    if duration is not None:
        require_positive_finite(duration, "playback duration", MediaConfigurationError)
    if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
        raise MediaConfigurationError("local playback volume must be an integer from 0 to 100")

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        stream.transport,
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-volume",
        str(volume),
        "-autoexit",
    ]
    if duration is not None:
        command.extend(("-t", str(duration)))
    if media == "audio":
        command.extend(("-nodisp", "-vn"))
    elif media == "video":
        command.append("-an")
    command.append(stream.url)
    return command


async def play_rtsp(
    stream: RTSPStream,
    *,
    media: MediaSelection = "audio",
    duration: float | None = None,
    volume: int = 100,
    executable: str = "ffplay",
) -> dict[str, Any]:
    """Play RTSP, allowing bounded sessions extra time to connect and start."""

    result = await _run_process(
        build_play_command(
            stream,
            media=media,
            duration=duration,
            volume=volume,
            executable=executable,
        ),
        timeout=None if duration is None else duration + max(15.0, duration * 0.1),
    )
    if result.returncode != 0:
        raise _media_failure("ffplay could not play the RTSP stream", result)
    return {
        "media": media,
        "quality": stream.quality,
        "duration_limit": duration,
        "local_volume": volume,
    }


def build_snapshot_command(
    stream: RTSPStream,
    output: str | Path,
    *,
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> list[str]:
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        stream.transport,
        "-i",
        stream.url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-y" if overwrite else "-n",
        str(output),
    ]


async def capture_rtsp_snapshot(
    stream: RTSPStream,
    output: str | Path,
    *,
    timeout: float = 15.0,
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> dict[str, Any]:
    """Save one decoded video frame from RTSP as an image file."""

    require_positive_finite(timeout, "snapshot timeout", MediaConfigurationError)
    path = prepare_media_output(output, overwrite=overwrite)
    result = await _run_process(
        build_snapshot_command(stream, path, overwrite=overwrite, executable=executable),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise _media_failure("ffmpeg could not capture a video frame", result)
    return {
        **_output_metadata(path, "snapshot"),
        "quality": stream.quality,
    }


def _validate_h264_key_access_unit(access_unit: bytes) -> None:
    if not isinstance(access_unit, bytes) or not access_unit:
        raise MediaConfigurationError("H.264 access unit must be non-empty bytes")

    nal_types: set[int] = set()
    first_start: int | None = None
    offset = 0
    while offset < len(access_unit) - 2:
        start_size = 0
        if access_unit[offset : offset + 3] == b"\x00\x00\x01":
            start_size = 3
        elif access_unit[offset : offset + 4] == b"\x00\x00\x00\x01":
            start_size = 4
        if not start_size:
            offset += 1
            continue

        if first_start is None:
            first_start = offset
        header_offset = offset + start_size
        if header_offset < len(access_unit):
            header = access_unit[header_offset]
            if not header & 0x80:
                nal_types.add(header & 0x1F)
        offset = header_offset + 1

    if first_start is None or any(access_unit[:first_start]):
        raise MediaStreamError("H.264 access unit is not Annex-B framed")
    missing = {5, 7, 8} - nal_types
    if missing:
        raise MediaStreamError("H.264 key access unit must contain SPS, PPS, and IDR NAL units")


def build_h264_snapshot_command(
    output: str | Path,
    *,
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> list[str]:
    """Build the fixed one-frame decoder command for a key Annex-B access unit."""

    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-y" if overwrite else "-n",
        str(output),
    ]


async def capture_h264_snapshot(
    access_unit: bytes,
    output: str | Path,
    *,
    timeout: float = 15.0,
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> dict[str, Any]:
    """Decode one already-received H.264 Annex-B key access unit into an image."""

    require_positive_finite(timeout, "snapshot timeout", MediaConfigurationError)
    path = prepare_media_output(output, overwrite=overwrite)
    _validate_h264_key_access_unit(access_unit)
    result = await _run_process(
        build_h264_snapshot_command(path, overwrite=overwrite, executable=executable),
        timeout=timeout,
        input_data=access_unit,
        timeout_context="decoding the H.264 access unit",
    )
    if result.returncode != 0:
        raise _media_failure("ffmpeg could not decode the H.264 access unit", result)
    return {
        **_output_metadata(path, "snapshot"),
        "codec": "h264",
        "source": "pppp",
    }
