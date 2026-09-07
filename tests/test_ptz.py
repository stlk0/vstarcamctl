from __future__ import annotations

import asyncio

import pytest

from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.camera import VStarcamCamera, _await_cancellation_safe_cleanup
from vstarcamctl.errors import (
    CapabilityUnavailableError,
    CatalogError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
    PTZConfigurationError,
    ServiceChangeCancelledError,
    ServiceChangeUncertainError,
    TransportCommandCancelledError,
)
from vstarcamctl.ptz import (
    build_ptz_start_path,
    build_ptz_stop_path,
    parse_ptz_response,
    validate_ptz_duration,
)


class SlowStartTransport(FakeTransport):
    async def request(self, command: str, *, timeout: float) -> str:
        if "/decoder_control.cgi?command=0&onestep=0" in command:
            self.requests.append(command)
            await asyncio.sleep(60)
            raise AssertionError("movement deadline did not cancel the start request")
        return await super().request(command, timeout=timeout)


class CancellationClassifyingSlowStartTransport(FakeTransport):
    async def request(self, command: str, *, timeout: float) -> str:
        if "/decoder_control.cgi?command=0&onestep=0" in command:
            self.requests.append(command)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise TransportCommandCancelledError("start send was interrupted") from exc
        return await super().request(command, timeout=timeout)


class CancellationClassifyingSlowStopTransport(FakeTransport):
    async def request(self, command: str, *, timeout: float) -> str:
        if "/decoder_control.cgi?command=7&onestep=0" in command:
            self.requests.append(command)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise TransportCommandCancelledError("stop send was interrupted") from exc
        return await super().request(command, timeout=timeout)


class GatedStartTransport(FakeTransport):
    def __init__(self, responses):
        super().__init__(responses)
        self.start_seen = asyncio.Event()
        self.release_start = asyncio.Event()

    async def request(self, command: str, *, timeout: float) -> str:
        if "/decoder_control.cgi?command=0&onestep=0" in command:
            self.requests.append(command)
            self.start_seen.set()
            await self.release_start.wait()
            return "var result=0;"
        return await super().request(command, timeout=timeout)


class LockedStopTransport(FakeTransport):
    def __init__(self, responses):
        super().__init__(responses)
        self.stop_lock = asyncio.Lock()
        self.stop_waiting = asyncio.Event()
        self.stop_completed = asyncio.Event()
        self.stop_error: BaseException | None = None

    async def request(self, command: str, *, timeout: float) -> str:
        if "/decoder_control.cgi?command=1&onestep=0" in command:
            self.stop_waiting.set()
            async with self.stop_lock:
                self.requests.append(command)
                self.stop_completed.set()
                if self.stop_error is not None:
                    raise self.stop_error
                return "var result=0;"
        return await super().request(command, timeout=timeout)


@pytest.mark.parametrize(
    ("direction", "start", "stop"),
    [
        ("up", 0, 1),
        ("down", 2, 3),
        ("left", 4, 5),
        ("right", 6, 7),
    ],
)
def test_ptz_builders_use_exact_direction_pairs(direction, start, stop):
    assert build_ptz_start_path(direction) == (f"/decoder_control.cgi?command={start}&onestep=0")
    assert build_ptz_stop_path(direction) == (f"/decoder_control.cgi?command={stop}&onestep=0")


def test_ptz_acknowledgement_requires_exact_zero_result():
    assert parse_ptz_response({"result": "0", "vendor": "ignored"}) == {"result": 0}
    for payload in ({}, {"result": 1}, {"result": "ok"}, {"result": True}):
        with pytest.raises(PTZConfigurationError):
            parse_ptz_response(payload)


def test_ptz_rejects_unknown_direction():
    with pytest.raises(PTZConfigurationError, match="direction"):
        build_ptz_start_path("diagonal")
    with pytest.raises(PTZConfigurationError, match="direction"):
        build_ptz_stop_path(None)


@pytest.mark.parametrize(
    "duration",
    [0, -1, float("nan"), float("inf"), 10**1000, True, "1"],
)
def test_ptz_duration_must_be_finite_and_positive(duration):
    with pytest.raises(PTZConfigurationError, match="finite number greater than zero"):
        validate_ptz_duration(duration)


async def test_ptz_permissions_and_input_are_checked_before_connect():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.move_ptz("up", 1)
    with pytest.raises(ConfirmationRequiredError, match="--confirm"):
        await camera.move_ptz("up", 1, experimental=True)
    with pytest.raises(ExperimentalCommandError, match="experimental/unconfirmed"):
        await camera.stop_ptz("up")
    with pytest.raises(PTZConfigurationError, match="duration"):
        await camera.move_ptz("up", 0, experimental=True, confirm=True)
    with pytest.raises(PTZConfigurationError, match="direction"):
        await camera.move_ptz("diagonal", 1, experimental=True, confirm=True)

    assert transport.connect_count == 0
    assert transport.requests == []


@pytest.mark.parametrize("command", [0, 2, 4, 6])
async def test_raw_ptz_start_is_never_available(command):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError, match="guarded high-level API"):
        await camera.send_raw_cgi(
            f"/decoder_control.cgi?command={command}&onestep=0",
            experimental=True,
            confirm=True,
            recovery_ready=True,
            retry_requests=False,
        )

    assert transport.connect_count == 0


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/decoder_control.cgi?onestep=0&command=0", ExperimentalCommandError),
        ("/decoder_control.cgi?command=0&onestep=0&extra=1", CatalogError),
        ("/decoder_control.cgi?command=0&onestep=1", CatalogError),
        ("/decoder_control.cgi?command=0", CatalogError),
    ],
)
async def test_raw_ptz_start_query_variants_fail_closed(path, error):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(error):
        await camera.send_raw_cgi(
            path,
            experimental=True,
            confirm=True,
            recovery_ready=True,
            retry_requests=False,
        )

    assert transport.connect_count == 0


async def test_raw_ptz_stop_remains_available():
    transport = FakeTransport(["var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert (
        await camera.send_raw_cgi(
            "/decoder_control.cgi?command=1&onestep=0",
            experimental=True,
            retry_requests=False,
        )
        == "var result=0;"
    )
    assert len(transport.requests) == 1


async def test_explicit_missing_motor_capability_blocks_before_start():
    transport = FakeTransport(["var haveMotor=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(CapabilityUnavailableError, match="haveMotor"):
        await camera.move_ptz("left", 0.001, experimental=True, confirm=True)

    assert len(transport.requests) == 1
    assert "/get_status.cgi" in transport.requests[0]


async def test_unknown_motor_capability_allows_bounded_one_shot_move():
    transport = FakeTransport(["var haveMotor='unknown';", "var result=0;", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    assert await camera.move_ptz("right", 0.001, experimental=True, confirm=True) == {
        "start": {"result": 0},
        "stop": {"result": 0},
    }
    assert len(transport.requests) == 3
    assert "/get_status.cgi" in transport.requests[0]
    assert "/decoder_control.cgi?command=6&onestep=0" in transport.requests[1]
    assert "/decoder_control.cgi?command=7&onestep=0" in transport.requests[2]


async def test_invalid_start_acknowledgement_is_uncertain_and_still_stops():
    transport = FakeTransport(["var haveMotor=1;", "var result=1;", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="start.*invalid"):
        await camera.move_ptz("up", 1, experimental=True, confirm=True)

    assert len(transport.requests) == 3
    assert "command=1&onestep=0" in transport.requests[2]


async def test_invalid_explicit_stop_acknowledgement_is_uncertain():
    transport = FakeTransport(["var result=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="stop.*invalid"):
        await camera.stop_ptz("right", experimental=True)

    assert len(transport.requests) == 1


async def test_start_timeout_still_attempts_matching_stop_without_retry():
    start_timeout = TimeoutError("start acknowledgement lost")
    transport = FakeTransport(["var haveMotor=1;", start_timeout, "var result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="PTZ down start") as caught:
        await camera.move_ptz("down", 1, experimental=True, confirm=True)

    assert caught.value.__cause__.__cause__ is start_timeout
    assert len(transport.requests) == 3
    assert sum("command=2&onestep=0" in request for request in transport.requests) == 1
    assert sum("command=3&onestep=0" in request for request in transport.requests) == 1


async def test_movement_deadline_cancels_slow_start_and_attempts_stop():
    transport = SlowStartTransport(["var haveMotor=1;", "var result=0;"])
    camera = VStarcamCamera(config(timeout=8), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="movement deadline"):
        await camera.move_ptz("up", 0.01, experimental=True, confirm=True)

    assert len(transport.requests) == 3
    assert "command=1&onestep=0" in transport.requests[2]


async def test_movement_deadline_is_not_misclassified_as_external_cancellation():
    transport = CancellationClassifyingSlowStartTransport(["var haveMotor=1;", "var result=0;"])
    camera = VStarcamCamera(config(timeout=8), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="movement deadline") as caught:
        await camera.move_ptz("up", 0.01, experimental=True, confirm=True)

    assert not isinstance(caught.value, asyncio.CancelledError)
    assert "command=1&onestep=0" in transport.requests[-1]


async def test_explicit_stop_deadline_is_not_misclassified_as_external_cancellation():
    transport = CancellationClassifyingSlowStopTransport()
    camera = VStarcamCamera(config(timeout=0.01), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="request timeout") as caught:
        await camera.stop_ptz("right", experimental=True)

    assert not isinstance(caught.value, asyncio.CancelledError)
    assert len(transport.requests) == 1


async def test_cancellation_still_attempts_matching_stop():
    transport = GatedStartTransport(["var haveMotor=1;", "var result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    task = asyncio.create_task(camera.move_ptz("up", 60, experimental=True, confirm=True))
    await transport.start_seen.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(transport.requests) == 3
    assert "command=1&onestep=0" in transport.requests[2]


@pytest.mark.parametrize("stop_error", [None, TimeoutError("stop failed")])
async def test_repeated_cancellation_waits_for_one_locked_stop(stop_error, caplog):
    transport = LockedStopTransport(["var haveMotor=1;", "var result=0;"])
    transport.stop_error = stop_error
    await transport.stop_lock.acquire()
    camera = VStarcamCamera(config(), transport=transport)
    task = asyncio.create_task(camera.move_ptz("up", 0.001, experimental=True, confirm=True))
    await transport.stop_waiting.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    transport.stop_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.stop_completed.is_set()
    assert sum("command=1&onestep=0" in request for request in transport.requests) == 1
    if stop_error is not None:
        assert "PTZ cleanup stop failed after cancellation" in caplog.text


async def test_primary_start_error_survives_cleanup_failure():
    start_timeout = TimeoutError("start acknowledgement lost")
    stop_timeout = TimeoutError("stop acknowledgement lost")
    transport = FakeTransport(["var haveMotor=1;", start_timeout, stop_timeout])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="PTZ up start") as caught:
        await camera.move_ptz("up", 1, experimental=True, confirm=True)

    assert caught.value.__cause__.__cause__ is start_timeout
    assert len(transport.requests) == 3


async def test_stop_failure_is_uncertain_and_never_retried():
    stop_timeout = TimeoutError("stop acknowledgement lost")
    transport = FakeTransport([stop_timeout])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="PTZ right stop") as caught:
        await camera.stop_ptz("right", experimental=True)

    assert caught.value.__cause__.__cause__ is stop_timeout
    assert len(transport.requests) == 1
    assert "command=7&onestep=0" in transport.requests[0]


async def test_move_surfaces_cleanup_stop_uncertainty():
    stop_timeout = TimeoutError("stop acknowledgement lost")
    transport = FakeTransport(["var haveMotor=1;", "var result=0;", stop_timeout])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="PTZ left stop") as caught:
        await camera.move_ptz("left", 0.001, experimental=True, confirm=True)

    assert caught.value.__cause__.__cause__ is stop_timeout
    assert len(transport.requests) == 3


async def test_cleanup_preserves_domain_cancellation_while_caller_is_also_cancelled():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await release_cleanup.wait()
        raise ServiceChangeCancelledError("cleanup outcome is unknown")

    task = asyncio.create_task(
        _await_cancellation_safe_cleanup(cleanup(), operation="synthetic cleanup")
    )
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_cleanup.set()

    with pytest.raises(ServiceChangeCancelledError, match="outcome is unknown"):
        await task


async def test_ptz_moves_are_serialized_but_explicit_stop_bypasses_move_lock():
    class ConcurrentMoveTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.up_start_seen = asyncio.Event()
            self.release_up_start = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if not self.connected:
                raise RuntimeError("fake transport is not connected")
            self.requests.append(command)
            if "/get_status.cgi" in command:
                return "var haveMotor=1;"
            if "/decoder_control.cgi?command=0&onestep=0" in command:
                self.up_start_seen.set()
                await self.release_up_start.wait()
            return "var result=0;"

    transport = ConcurrentMoveTransport()
    camera = VStarcamCamera(config(), transport=transport)
    up = asyncio.create_task(camera.move_ptz("up", 0.1, experimental=True, confirm=True))
    await transport.up_start_seen.wait()
    right = asyncio.create_task(camera.move_ptz("right", 0.1, experimental=True, confirm=True))
    await asyncio.sleep(0)

    assert not any("command=6&onestep=0" in request for request in transport.requests)
    assert await camera.stop_ptz("right", experimental=True) == {"result": 0}
    transport.release_up_start.set()
    await asyncio.gather(up, right)

    up_stop = next(
        index
        for index, request in enumerate(transport.requests)
        if "command=1&onestep=0" in request
    )
    right_start = next(
        index
        for index, request in enumerate(transport.requests)
        if "command=6&onestep=0" in request
    )
    assert up_stop < right_start
