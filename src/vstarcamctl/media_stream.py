"""Safe RTSP video/audio access through local FFmpeg tools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import shutil
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import quote

from ._normalize import has_control_chars
from ._process import stop_process
from .errors import (
    MediaConfigurationError,
    MediaStreamError,
    MediaToolUnavailableError,
)

StreamQuality = Literal["main", "sub"]
RTSPTransport = Literal["tcp", "udp"]
MediaSelection = Literal["both", "video", "audio"]
PlaybackSelection = Literal["both", "video", "audio"]

_STREAM_INDEX = {"main": 0, "sub": 1}


def _validate_credential(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise MediaConfigurationError(f"{field} must be text")
    if has_control_chars(value):
        raise MediaConfigurationError(f"{field} contains control characters")


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
            _validate_credential(self.username, "RTSP username")
            assert self.password is not None
            _validate_credential(self.password, "RTSP password")

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
    stderr: bytes


async def _run_process(command: Sequence[str], *, timeout: float) -> _ProcessResult:
    executable = command[0]
    if shutil.which(executable) is None:
        raise MediaToolUnavailableError(
            f"{executable} is required for RTSP media access but was not found"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MediaToolUnavailableError(f"cannot start required media tool {executable}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    except asyncio.TimeoutError as exc:
        await stop_process(process)
        raise MediaStreamError(f"{executable} timed out while reading the RTSP stream") from exc
    return _ProcessResult(process.returncode or 0, stdout, stderr)


async def _run_process_until_exit(command: Sequence[str]) -> _ProcessResult:
    executable = command[0]
    if shutil.which(executable) is None:
        raise MediaToolUnavailableError(
            f"{executable} is required for RTSP media access but was not found"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MediaToolUnavailableError(f"cannot start required media tool {executable}") from exc
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    return _ProcessResult(process.returncode or 0, stdout, stderr)


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

    if not math.isfinite(timeout) or timeout <= 0:
        raise MediaConfigurationError("media probe timeout must be finite and greater than zero")
    result = await _run_process(build_probe_command(stream, executable=executable), timeout=timeout)
    if result.returncode != 0:
        # FFmpeg commonly echoes its input URL to stderr, so stderr is
        # intentionally not included in this exception or logs.
        raise MediaStreamError(f"ffprobe could not read the RTSP stream (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaStreamError("ffprobe returned invalid JSON metadata") from exc
    return _normalize_probe(payload)


def _prepare_output(output: str | Path, *, overwrite: bool) -> Path:
    path = Path(output).expanduser()
    if not path.parent.exists():
        raise MediaConfigurationError(f"output directory does not exist: {path.parent}")
    if path.exists() and not overwrite:
        raise MediaConfigurationError(
            f"output already exists: {path}; pass overwrite=True or --overwrite"
        )
    return path


def build_record_command(
    stream: RTSPStream,
    output: str | Path,
    *,
    duration: float,
    media: MediaSelection = "both",
    overwrite: bool = False,
    executable: str = "ffmpeg",
) -> list[str]:
    if not math.isfinite(duration) or duration <= 0:
        raise MediaConfigurationError("recording duration must be finite and greater than zero")
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

    path = _prepare_output(output, overwrite=overwrite)
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
        raise MediaStreamError(
            f"ffmpeg could not record the RTSP stream (exit {result.returncode})"
        )
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaStreamError("ffmpeg completed without producing a non-empty recording")
    return {
        "path": str(path.resolve()),
        "bytes_written": path.stat().st_size,
        "duration": duration,
        "media": media,
        "quality": stream.quality,
    }


def build_play_command(
    stream: RTSPStream,
    *,
    media: PlaybackSelection = "audio",
    duration: float | None = None,
    volume: int = 100,
    executable: str = "ffplay",
) -> list[str]:
    """Build an interactive low-latency ffplay command without a shell."""

    if media not in ("both", "video", "audio"):
        raise MediaConfigurationError("playback media must be both, video, or audio")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise MediaConfigurationError("playback duration must be finite and greater than zero")
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
    media: PlaybackSelection = "audio",
    duration: float | None = None,
    volume: int = 100,
    executable: str = "ffplay",
) -> dict[str, Any]:
    """Play a live RTSP stream until it ends, reaches duration, or is cancelled."""

    result = await _run_process_until_exit(
        build_play_command(
            stream,
            media=media,
            duration=duration,
            volume=volume,
            executable=executable,
        )
    )
    if result.returncode != 0:
        raise MediaStreamError(f"ffplay could not play the RTSP stream (exit {result.returncode})")
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

    if not math.isfinite(timeout) or timeout <= 0:
        raise MediaConfigurationError("snapshot timeout must be finite and greater than zero")
    path = _prepare_output(output, overwrite=overwrite)
    result = await _run_process(
        build_snapshot_command(stream, path, overwrite=overwrite, executable=executable),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MediaStreamError(f"ffmpeg could not capture a video frame (exit {result.returncode})")
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaStreamError("ffmpeg completed without producing a non-empty snapshot")
    return {
        "path": str(path.resolve()),
        "bytes_written": path.stat().st_size,
        "quality": stream.quality,
    }
