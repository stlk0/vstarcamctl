from __future__ import annotations

import asyncio

import pytest

import vstarcamctl._process as process_helpers
from vstarcamctl.errors import MediaToolUnavailableError


class _Process:
    def __init__(self, *, terminate_missing: bool = False, kill_missing: bool = False):
        self.returncode = None
        self.terminate_missing = terminate_missing
        self.kill_missing = kill_missing
        self.terminate_count = 0
        self.kill_count = 0
        self.wait_count = 0

    def terminate(self):
        self.terminate_count += 1
        if self.terminate_missing:
            raise ProcessLookupError

    def kill(self):
        self.kill_count += 1
        if self.kill_missing:
            raise ProcessLookupError

    async def wait(self):
        self.wait_count += 1
        self.returncode = 0
        return 0


async def test_start_media_process_translates_os_error_without_echoing_arguments(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise OSError("secret argument")

    monkeypatch.setattr(process_helpers.asyncio, "create_subprocess_exec", fail)

    with pytest.raises(MediaToolUnavailableError) as caught:
        await process_helpers.start_media_process(["ffmpeg", "private-url"])

    assert "ffmpeg" in str(caught.value)
    assert "private-url" not in str(caught.value)
    assert "secret argument" not in str(caught.value)


async def test_stop_process_reaps_terminate_race():
    process = _Process(terminate_missing=True)

    await process_helpers.stop_process(process)

    assert process.terminate_count == 1
    assert process.wait_count == 1
    assert process.kill_count == 0


async def test_stop_process_reaps_kill_race_after_timeout(monkeypatch):
    process = _Process(kill_missing=True)

    async def timeout(awaitable, *, timeout):
        assert timeout == 2.0
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(process_helpers.asyncio, "wait_for", timeout)

    await process_helpers.stop_process(process)

    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.wait_count == 1


async def test_stop_process_leaves_an_exited_process_untouched():
    process = _Process()
    process.returncode = 0

    await process_helpers.stop_process(process)

    assert process.terminate_count == 0
    assert process.kill_count == 0
    assert process.wait_count == 0


async def test_stop_process_reaps_child_before_propagating_repeated_cancellation():
    waiting = asyncio.Event()
    release = asyncio.Event()

    class DelayedProcess(_Process):
        async def wait(self):
            self.wait_count += 1
            waiting.set()
            await release.wait()
            self.returncode = 0
            return 0

    process = DelayedProcess()
    operation = asyncio.create_task(process_helpers.stop_process(process))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        for _ in range(2):
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
            assert process.returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=1)
        assert process.returncode == 0
        assert process.terminate_count == 1
        assert process.wait_count == 1
        assert process.kill_count == 0
    finally:
        release.set()
        operation.cancel()
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), timeout=1)
