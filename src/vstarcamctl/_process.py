"""Shared asyncio subprocess helpers for the FFmpeg-based media paths."""

from __future__ import annotations

import asyncio


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child process, escalating to kill if it does not exit."""

    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
