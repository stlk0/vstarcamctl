"""Shared asyncio subprocess helpers for the FFmpeg-based media paths."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .errors import MediaToolUnavailableError


async def start_media_process(
    command: Sequence[str],
    *,
    capture_stderr: bool = False,
    pipe_stdin: bool = False,
) -> asyncio.subprocess.Process:
    """Start a media tool without exposing its command or stderr."""

    executable = command[0]
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if pipe_stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise MediaToolUnavailableError(f"cannot start required media tool {executable}") from exc


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Reap a child before returning, even when cleanup is repeatedly cancelled."""

    if process.returncode is not None:
        return
    cleanup = asyncio.create_task(_stop_process(process))
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    cleanup.result()
    if cancellation is not None:
        raise cancellation


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.terminate()
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
