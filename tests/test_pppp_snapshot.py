from __future__ import annotations

import asyncio

import pytest

import vstarcamctl.camera as camera_module
from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.audio_talk import ADPCM_PCM_SIZE, build_adpcm_talk_frame
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    ExperimentalCommandError,
    MediaConfigurationError,
    ServiceChangeUncertainError,
    TransportError,
    TransportTimeoutError,
)

KEY_ACCESS_UNIT = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"


class SnapshotTransport(FakeTransport):
    def __init__(self, responses=None):
        super().__init__(responses)
        self.events: list[str] = []

    async def request(self, command: str, *, timeout: float) -> str:
        if "streamid=10" in command:
            self.events.append("start")
        elif "streamid=16" in command:
            self.events.append("stop")
        return await super().request(command, timeout=timeout)

    async def start_video_capture(self) -> None:
        self.events.append("arm")
        await super().start_video_capture()

    async def receive_video_keyframe(self, *, timeout: float) -> bytes:
        self.events.append("receive")
        assert self._video_keyframe_future is not None
        self._video_keyframe_future.set_result(KEY_ACCESS_UNIT)
        return await super().receive_video_keyframe(timeout=timeout)

    async def stop_video_capture(self) -> None:
        self.events.append("disarm")
        await super().stop_video_capture()


async def _one_talk_frame():
    yield build_adpcm_talk_frame(b"\x00" * ADPCM_PCM_SIZE)[0]


async def test_pppp_snapshot_requires_experimental_before_connecting(tmp_path):
    transport = SnapshotTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.capture_pppp_snapshot(tmp_path / "frame.jpg")

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_pppp_snapshot_preflights_output_before_starting_camera(tmp_path):
    output = tmp_path / "frame.jpg"
    output.write_bytes(b"existing")
    transport = SnapshotTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="already exists"):
        await camera.capture_pppp_snapshot(output, experimental=True)

    assert transport.connect_count == 0
    assert transport.requests == []


async def test_pppp_snapshot_uses_exact_same_session_start_receive_stop_then_decode(
    monkeypatch,
    tmp_path,
):
    transport = SnapshotTransport(["result=0;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)
    output = tmp_path / "frame.jpg"

    async def decode(access_unit, path, **kwargs):
        assert access_unit == KEY_ACCESS_UNIT
        assert transport._video_keyframe_future is None
        assert path == output
        assert kwargs == {"timeout": 2, "overwrite": False, "executable": "ffmpeg"}
        transport.events.append("decode")
        return {"path": str(output), "bytes_written": 5, "codec": "h264", "source": "pppp"}

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", decode)

    result = await camera.capture_pppp_snapshot(output, timeout=2, experimental=True)

    assert result["source"] == "pppp"
    assert transport.events == ["arm", "start", "receive", "stop", "disarm", "decode"]
    assert len(transport.requests) == 2
    assert "/livestream.cgi?streamid=10&substream=1&" in transport.requests[0]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[1]
    assert transport.connect_count == 1


async def test_pppp_snapshot_timeout_still_stops_and_disarms(monkeypatch, tmp_path):
    class TimeoutTransport(SnapshotTransport):
        async def receive_video_keyframe(self, *, timeout: float) -> bytes:
            self.events.append("receive")
            raise TransportTimeoutError("no keyframe")

    transport = TimeoutTransport(["result=0;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    async def unexpected_decode(*_args, **_kwargs):
        raise AssertionError("decoder must not run without a keyframe")

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", unexpected_decode)

    with pytest.raises(TransportTimeoutError, match="no keyframe"):
        await camera.capture_pppp_snapshot(
            tmp_path / "frame.jpg",
            timeout=0.01,
            experimental=True,
        )

    assert transport.events == ["arm", "start", "receive", "stop", "disarm"]
    assert sum("streamid=16" in request for request in transport.requests) == 1


async def test_pppp_snapshot_rejects_bad_start_ack_and_still_sends_stop(monkeypatch, tmp_path):
    transport = SnapshotTransport(["result=1;", "result=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    async def unexpected_decode(*_args, **_kwargs):
        raise AssertionError("decoder must not run after a rejected start")

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", unexpected_decode)

    with pytest.raises(ServiceChangeUncertainError, match="acknowledgement was invalid"):
        await camera.capture_pppp_snapshot(
            tmp_path / "frame.jpg",
            experimental=True,
        )

    assert transport.events == ["arm", "start", "stop", "disarm"]


async def test_pppp_snapshot_never_reconnects_to_send_stop_on_a_new_session(tmp_path):
    class DisconnectingTransport(SnapshotTransport):
        async def receive_video_keyframe(self, *, timeout: float) -> bytes:
            self.events.append("receive")
            self._connected = False
            raise TransportError("session lost")

    transport = DisconnectingTransport(["result=0;"])
    camera = VStarcamCamera(config(retries=5), transport=transport)

    with pytest.raises(TransportError, match="session lost"):
        await camera.capture_pppp_snapshot(
            tmp_path / "frame.jpg",
            experimental=True,
        )

    assert transport.connect_count == 1
    assert len(transport.requests) == 1
    assert transport.events == ["arm", "start", "receive", "disarm"]


async def test_pppp_snapshot_disarm_finishes_when_caller_is_cancelled(monkeypatch, tmp_path):
    class LockedDisarmTransport(SnapshotTransport):
        def __init__(self):
            super().__init__(["result=0;", "result=0;"])
            self.disarm_waiting = asyncio.Event()
            self.release_disarm = asyncio.Event()

        async def stop_video_capture(self) -> None:
            self.events.append("disarm")
            self.disarm_waiting.set()
            await self.release_disarm.wait()
            await FakeTransport.stop_video_capture(self)

    transport = LockedDisarmTransport()
    camera = VStarcamCamera(config(), transport=transport)

    async def unexpected_decode(*_args, **_kwargs):
        raise AssertionError("cancelled capture must not decode")

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", unexpected_decode)
    task = asyncio.create_task(
        camera.capture_pppp_snapshot(
            tmp_path / "frame.jpg",
            experimental=True,
        )
    )
    await transport.disarm_waiting.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    transport.release_disarm.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport._video_keyframe_future is None


async def test_pppp_snapshot_session_lease_delays_concurrent_close(monkeypatch, tmp_path):
    class WaitingFrameTransport(SnapshotTransport):
        def __init__(self):
            super().__init__(["result=0;", "result=0;"])
            self.receive_waiting = asyncio.Event()
            self.release_frame = asyncio.Event()

        async def receive_video_keyframe(self, *, timeout: float) -> bytes:
            self.events.append("receive")
            self.receive_waiting.set()
            await self.release_frame.wait()
            assert self._video_keyframe_future is not None
            self._video_keyframe_future.set_result(KEY_ACCESS_UNIT)
            return await FakeTransport.receive_video_keyframe(self, timeout=timeout)

    transport = WaitingFrameTransport()
    camera = VStarcamCamera(config(), transport=transport)
    output = tmp_path / "frame.jpg"

    async def decode(_access_unit, _path, **_kwargs):
        return {"path": str(output), "bytes_written": 5, "codec": "h264", "source": "pppp"}

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", decode)
    snapshot = asyncio.create_task(camera.capture_pppp_snapshot(output, experimental=True))
    await transport.receive_waiting.wait()
    close = asyncio.create_task(camera.close())
    await asyncio.sleep(0)
    assert not close.done()

    transport.release_frame.set()
    await snapshot
    await close

    assert transport.events == ["arm", "start", "receive", "stop", "disarm"]
    assert transport.connect_count == 1
    assert transport.close_count == 1


async def test_talk_and_snapshot_livestreams_are_serialized(monkeypatch, tmp_path):
    class BlockingTalkTransport(SnapshotTransport):
        def __init__(self):
            super().__init__(
                [
                    "firmware_version='test';",
                    "outvolume=10;",
                    "result=0;",
                    "result=0;",
                    "result=0;",
                    "result=0;",
                ]
            )
            self.talk_waiting = asyncio.Event()
            self.release_talk = asyncio.Event()

        async def send_channel_parts(self, channel, parts, *, timeout):
            self.talk_waiting.set()
            await self.release_talk.wait()
            await super().send_channel_parts(channel, parts, timeout=timeout)

    transport = BlockingTalkTransport()
    camera = VStarcamCamera(config(), transport=transport)
    output = tmp_path / "frame.jpg"

    async def decode(_access_unit, _path, **_kwargs):
        return {"path": str(output), "bytes_written": 5, "codec": "h264", "source": "pppp"}

    monkeypatch.setattr(camera_module, "capture_h264_snapshot", decode)
    talk = asyncio.create_task(
        camera.send_talk_audio(
            _one_talk_frame(),
            duration=1,
            max_speaker_volume=15,
            experimental=True,
            confirm=True,
        )
    )
    await transport.talk_waiting.wait()
    snapshot = asyncio.create_task(camera.capture_pppp_snapshot(output, experimental=True))
    await asyncio.sleep(0)

    assert not any("substream=1" in request for request in transport.requests)
    transport.release_talk.set()
    await talk
    await snapshot

    starts = [request for request in transport.requests if "streamid=10" in request]
    stops = [request for request in transport.requests if "streamid=16" in request]
    assert "substream=0" in starts[0]
    assert "substream=1" in starts[1]
    assert len(stops) == 2
