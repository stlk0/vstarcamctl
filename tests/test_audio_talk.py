from __future__ import annotations

import asyncio
import struct

import pytest

import vstarcamctl.audio_talk as audio_talk
from tests.helpers import FakeTransport
from tests.helpers import camera_config as config
from vstarcamctl.audio_talk import (
    ADPCM_FRAME_DURATION,
    ADPCM_PAYLOAD_SIZE,
    ADPCM_PCM_SIZE,
    TALK_CODEC_ADPCM,
    TALK_HEADER_MAGIC,
    TALK_HEADER_SIZE,
    TALK_HEADER_VERSION,
    AdpcmTalkFrame,
    ImaAdpcmState,
    build_adpcm_talk_frame,
    build_adpcm_talk_transcode_command,
    iter_adpcm_talk_frames,
    validate_adpcm_talk_frame,
)
from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.errors import (
    CatalogError,
    ConfirmationRequiredError,
    ExperimentalCommandError,
    MediaConfigurationError,
    MediaStreamError,
    ServiceChangeCancelledError,
    ServiceChangeUncertainError,
    TransportCommandCancelledError,
)

TALK_DURATION = 1.0
TALK_VOLUME_LIMIT = 15


def test_adpcm_frame_matches_header_state_and_payload_layout():
    frame, state = build_adpcm_talk_frame(
        b"\x00" * ADPCM_PCM_SIZE,
        ImaAdpcmState(),
    )

    assert len(frame.header) == TALK_HEADER_SIZE
    assert len(frame.payload) == ADPCM_PAYLOAD_SIZE
    assert struct.unpack_from("<I", frame.header, 0)[0] == TALK_HEADER_MAGIC
    assert struct.unpack_from("<H", frame.header, 4)[0] == TALK_HEADER_VERSION
    assert struct.unpack_from("<I", frame.header, 12)[0] == 0
    assert struct.unpack_from("<I", frame.header, 16)[0] == ADPCM_PAYLOAD_SIZE
    assert frame.header[27] == TALK_CODEC_ADPCM
    assert struct.unpack_from("<hH", frame.header, 28) == (0, 0)
    assert frame.payload == b"\x00" * ADPCM_PAYLOAD_SIZE
    assert state == ImaAdpcmState()


def test_adpcm_encoder_matches_regression_vector():
    pcm = b"".join(struct.pack("<h", index * 32 - 8192) for index in range(ADPCM_PCM_SIZE // 2))
    frame, state = build_adpcm_talk_frame(pcm)

    assert state == ImaAdpcmState(predictor=8163, index=16)
    assert frame.header[28:32].hex() == "e31f1000"
    assert frame.payload[:32].hex() == (
        "ffffffffb0080808008080080008000800000010010110121122223342342423"
    )


def test_talk_ffmpeg_command_produces_raw_8khz_mono_pcm():
    command = build_adpcm_talk_transcode_command("default", input_format="pulse", duration=3)
    assert command[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f"]
    assert command[command.index("-ar") + 1] == "8000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert command[-2:] == ["s16le", "pipe:1"]


@pytest.mark.parametrize(
    "duration",
    [True, "1", float("nan"), float("inf"), 10**1000, 0, -1],
)
def test_talk_ffmpeg_command_rejects_non_finite_or_non_numeric_duration(duration):
    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        build_adpcm_talk_transcode_command("source.raw", duration=duration)


@pytest.mark.parametrize(
    ("offset", "replacement"),
    [
        (0, 0),
        (4, 0),
        (6, 1),
        (15, 1),
        (17, 0),
        (20, 1),
        (26, 1),
        (27, 1),
        (30, 89),
    ],
)
def test_adpcm_frame_validator_rejects_every_invalid_header_region(offset, replacement):
    frame = _adpcm_frame()
    header = bytearray(frame.header)
    header[offset] = replacement

    with pytest.raises(MediaConfigurationError):
        validate_adpcm_talk_frame(AdpcmTalkFrame(bytes(header), frame.payload))


def test_adpcm_frame_validator_requires_immutable_wire_bytes():
    frame = _adpcm_frame()
    validate_adpcm_talk_frame(frame)

    for invalid in (
        AdpcmTalkFrame(bytearray(frame.header), frame.payload),
        AdpcmTalkFrame(frame.header, bytearray(frame.payload)),
    ):
        with pytest.raises(MediaConfigurationError, match="must be bytes"):
            validate_adpcm_talk_frame(invalid)


class _FakeTranscodeProcess:
    def __init__(self, data: bytes, *, exit_code: int = 0, stdout: bool = True):
        self.returncode = None
        self.exit_code = exit_code
        if stdout:
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()
            self.stdout = reader
        else:
            self.stdout = None

    async def wait(self):
        self.returncode = self.exit_code
        return self.exit_code


async def test_adpcm_iterator_pads_partial_pcm_and_always_stops_process(monkeypatch):
    process = _FakeTranscodeProcess(b"\x00" * ADPCM_PCM_SIZE + b"\x01\x02\x03")
    stopped = []

    async def start(_command):
        return process

    async def stop(current):
        stopped.append(current)

    monkeypatch.setattr(audio_talk, "start_media_process", start)
    monkeypatch.setattr(audio_talk, "stop_process", stop)

    frames = [frame async for frame in iter_adpcm_talk_frames("source.raw")]

    assert len(frames) == 2
    for frame in frames:
        validate_adpcm_talk_frame(frame)
    assert stopped == [process]


@pytest.mark.parametrize(("stdout", "exit_code"), [(True, 1), (False, 0)])
async def test_adpcm_iterator_translates_process_failures_and_stops(
    monkeypatch,
    stdout,
    exit_code,
):
    process = _FakeTranscodeProcess(b"", exit_code=exit_code, stdout=stdout)
    stopped = []

    async def start(_command):
        return process

    async def stop(current):
        stopped.append(current)

    monkeypatch.setattr(audio_talk, "start_media_process", start)
    monkeypatch.setattr(audio_talk, "stop_process", stop)

    with pytest.raises(MediaStreamError, match="ffmpeg"):
        _ = [frame async for frame in iter_adpcm_talk_frames("source.raw")]

    assert stopped == [process]


async def _payloads(*items: bytes):
    for item in items:
        yield item


def _adpcm_frame():
    return build_adpcm_talk_frame(
        b"\x00" * ADPCM_PCM_SIZE,
    )[0]


class LockedLivestreamStopTransport(FakeTransport):
    def __init__(self, responses):
        super().__init__(responses)
        self.stop_lock = asyncio.Lock()
        self.stop_waiting = asyncio.Event()
        self.stop_completed = asyncio.Event()

    async def request(self, command: str, *, timeout: float) -> str:
        if "/livestream.cgi?streamid=16&substream=0" in command:
            self.stop_waiting.set()
            async with self.stop_lock:
                self.requests.append(command)
                self.stop_completed.set()
                return "var result=0;"
        return await super().request(command, timeout=timeout)


async def test_talk_requires_both_safety_flags_before_connecting():
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ExperimentalCommandError):
        await camera.send_talk_audio(
            _payloads(b"x"),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
        )
    with pytest.raises(ConfirmationRequiredError):
        await camera.send_talk_audio(
            _payloads(b"x"),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
        )
    assert transport.connect_count == 0


@pytest.mark.parametrize(
    ("duration", "max_speaker_volume", "message"),
    [
        (None, TALK_VOLUME_LIMIT, "duration"),
        (0, TALK_VOLUME_LIMIT, "duration"),
        (ADPCM_FRAME_DURATION / 2, TALK_VOLUME_LIMIT, "duration"),
        (TALK_DURATION, None, "speaker volume"),
        (TALK_DURATION, 32, "speaker volume"),
    ],
)
async def test_talk_requires_explicit_finite_bounds_before_connecting(
    duration,
    max_speaker_volume,
    message,
):
    transport = FakeTransport()
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match=message):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=duration,
            max_speaker_volume=max_speaker_volume,
            experimental=True,
            confirm=True,
        )

    assert transport.connect_count == 0


@pytest.mark.parametrize(
    ("camera_params", "message"),
    [
        ("var firmware_version='test';", "did not report"),
        ("var outvolume=16;", "exceeds"),
    ],
)
async def test_talk_refuses_unverified_or_excessive_speaker_volume(
    camera_params,
    message,
):
    transport = FakeTransport(["var firmware_version='test';", camera_params])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match=message):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert all("livestream.cgi" not in request for request in transport.requests)


async def test_talk_rejects_invalid_external_frame_before_livestream_or_channel_write():
    valid = _adpcm_frame()
    header = bytearray(valid.header)
    header[6] = 1
    invalid = AdpcmTalkFrame(bytes(header), valid.payload)
    transport = FakeTransport(
        ["var EchoCancellationVer=0; var support_g711a=0;", "var outvolume=10;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="reserved"):
        await camera.send_talk_audio(
            _payloads(invalid),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert all("livestream.cgi" not in request for request in transport.requests)


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/audiostream.cgi?streamid=7", ExperimentalCommandError),
        ("/audiostream.cgi?streamid=7&extra=1", CatalogError),
        ("/audiostream.cgi?streamid=8", CatalogError),
        ("/livestream.cgi?streamid=10&substream=0", ExperimentalCommandError),
        ("/livestream.cgi?streamid=10&substream=2", ExperimentalCommandError),
        ("/livestream.cgi?streamid=11&substream=0", CatalogError),
    ],
)
async def test_raw_talk_and_livestream_starts_never_connect(path, error):
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


async def test_half_duplex_talk_wraps_channel_three_in_same_session_livestream():
    transport = FakeTransport(
        [
            "var EchoCancellationVer=0; var support_g711a=0;",
            "var outvolume=10;",
            "result=0;",
            "result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.send_talk_audio(
        _payloads(_adpcm_frame(), _adpcm_frame()),
        duration=TALK_DURATION,
        max_speaker_volume=TALK_VOLUME_LIMIT,
        experimental=True,
        confirm=True,
    )

    assert result["frames_sent"] == 2
    assert result["audio_seconds"] == round(2 * ADPCM_FRAME_DURATION, 3)
    assert result["codec"] == "ima_adpcm"
    assert result["full_duplex"] is False
    assert result["livestream_start_response"] == {"result": 0}
    assert result["livestream_stop_response"] == {"result": 0}
    assert result["speaker_volume"] == 10
    assert result["speaker_volume_limit"] == TALK_VOLUME_LIMIT
    assert len(transport.channel_writes) == 4
    assert len(transport.channel_batches) == 2
    assert all(len(parts) == 2 for _channel, parts in transport.channel_batches)
    assert [len(payload) for _channel, payload in transport.channel_writes] == [
        TALK_HEADER_SIZE,
        ADPCM_PAYLOAD_SIZE,
        TALK_HEADER_SIZE,
        ADPCM_PAYLOAD_SIZE,
    ]
    assert all(channel == 3 for channel, _payload in transport.channel_writes)
    assert len(transport.requests) == 4
    assert transport.requests[0] == (
        "GET /get_status.cgi?name=admin&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    )
    assert "/get_camera_params.cgi?" in transport.requests[1]
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[2]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[3]


async def test_talk_caps_an_infinite_source_at_the_explicit_duration():
    source_closed = asyncio.Event()

    async def infinite_frames():
        try:
            while True:
                yield _adpcm_frame()
        finally:
            source_closed.set()

    transport = FakeTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    result = await camera.send_talk_audio(
        infinite_frames(),
        duration=ADPCM_FRAME_DURATION * 2,
        max_speaker_volume=TALK_VOLUME_LIMIT,
        experimental=True,
        confirm=True,
    )

    assert result["frames_sent"] == 2
    assert source_closed.is_set()
    assert len(transport.channel_batches) == 2
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[-1]


async def test_talk_stops_livestream_before_waiting_for_source_cleanup():
    class BlockingCloseSource:
        def __init__(self):
            self.yielded = False
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.yielded:
                raise StopAsyncIteration
            self.yielded = True
            return _adpcm_frame()

        async def aclose(self):
            self.close_started.set()
            await self.release_close.wait()

    source = BlockingCloseSource()
    transport = FakeTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)
    task = asyncio.create_task(
        camera.send_talk_audio(
            source,
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )
    )

    await source.close_started.wait()
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[-1]
    assert not task.done()
    source.release_close.set()
    await task


async def test_talk_channel_timeout_is_uncertain_and_still_stops_livestream():
    class FailingTalkTransport(FakeTransport):
        async def send_channel_parts(self, channel, parts, *, timeout):
            raise TimeoutError("synthetic channel timeout")

    transport = FailingTalkTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="unknown acknowledged prefix"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[-1]


async def test_talk_channel_cancellation_remains_cancelled_and_uncertain():
    class CancelledTalkTransport(FakeTransport):
        async def send_channel_parts(self, channel, parts, *, timeout):
            raise TransportCommandCancelledError("synthetic post-send cancellation")

    transport = CancelledTalkTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeCancelledError, match="unknown acknowledged prefix") as caught:
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert isinstance(caught.value, asyncio.CancelledError)
    assert isinstance(caught.value, ServiceChangeUncertainError)
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[-1]


async def test_talk_stop_preempts_cancellation_resistant_source():
    class StopObservedTransport(FakeTransport):
        def __init__(self, responses):
            super().__init__(responses)
            self.stop_seen = asyncio.Event()

        async def request(self, command: str, *, timeout: float) -> str:
            if "/livestream.cgi?streamid=16&substream=0" in command:
                self.stop_seen.set()
            return await super().request(command, timeout=timeout)

    class ResistantSource:
        def __init__(self):
            self.count = 0
            self.next_started = asyncio.Event()
            self.release = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.count == 0:
                self.count += 1
                return _adpcm_frame()
            self.next_started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            raise StopAsyncIteration

    source = ResistantSource()
    transport = StopObservedTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(timeout=1), transport=transport)
    task = asyncio.create_task(
        camera.send_talk_audio(
            source,
            duration=ADPCM_FRAME_DURATION * 2,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )
    )

    await source.next_started.wait()
    await asyncio.wait_for(transport.stop_seen.wait(), timeout=1)
    assert not task.done()
    source.release.set()

    with pytest.raises(ServiceChangeUncertainError, match="input stalled"):
        await task


async def test_talk_source_cleanup_timeout_is_a_domain_error_after_stop():
    class SlowCloseSource:
        def __init__(self):
            self.yielded = False
            self.release = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.yielded:
                raise StopAsyncIteration
            self.yielded = True
            return _adpcm_frame()

        async def aclose(self):
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()

    source = SlowCloseSource()
    transport = FakeTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(timeout=0.01), transport=transport)

    with pytest.raises(MediaStreamError, match="cleanup did not finish"):
        await camera.send_talk_audio(
            source,
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[-1]
    source.release.set()
    await asyncio.sleep(0)


async def test_direct_adpcm_refuses_explicit_full_duplex_capability():
    transport = FakeTransport(["var EchoCancellationVer=2; var support_g711a=0;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="full-duplex"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert transport.requests == [
        "GET /get_status.cgi?name=admin&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]


@pytest.mark.parametrize(
    "response",
    [
        "var EchoCancellationVer=-1;",
        "var EchoCancellationVer='invalid';",
        "var EchoCancellationVer=null;",
        "var support_g711a=-1;",
        "var support_audio_g711a='invalid';",
    ],
)
async def test_direct_adpcm_refuses_unknown_malformed_capability(response):
    transport = FakeTransport([response])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="capability is malformed"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert transport.requests == [
        "GET /get_status.cgi?name=admin&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]


@pytest.mark.parametrize("field", ["support_g711a", "support_audio_g711a"])
async def test_direct_adpcm_refuses_explicit_g711_capability(field):
    transport = FakeTransport([f"var EchoCancellationVer=0; var {field}=1;"])
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(MediaConfigurationError, match="G.711"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert transport.requests == [
        "GET /get_status.cgi?name=admin&loginuse=admin"
        "&userId=0&loginpas=camera-secret&user=admin&pwd=camera-secret&"
    ]


async def test_half_duplex_talk_ceases_frames_when_audio_source_fails():
    async def failing_payloads():
        yield _adpcm_frame()
        raise RuntimeError("local source failed")

    transport = FakeTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="talk input failed"):
        await camera.send_talk_audio(
            failing_payloads(),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert len(transport.channel_writes) == 2
    assert len(transport.requests) == 4
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[2]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[3]


async def test_talk_attempts_livestream_stop_when_start_ack_is_lost():
    transport = FakeTransport(
        [
            "var firmware_version='test';",
            "var outvolume=10;",
            TimeoutError("ack lost"),
            "result=0;",
        ]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="outcome is unknown"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert len(transport.requests) == 4
    assert "/livestream.cgi?streamid=10&substream=0&" in transport.requests[2]
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[3]


async def test_talk_rejects_invalid_livestream_ack_before_sending_audio():
    transport = FakeTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=1;", "result=0;"]
    )
    camera = VStarcamCamera(config(), transport=transport)

    with pytest.raises(ServiceChangeUncertainError, match="acknowledgement was invalid"):
        await camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )

    assert transport.channel_writes == []
    assert len(transport.requests) == 4
    assert "/livestream.cgi?streamid=16&substream=0&" in transport.requests[3]


async def test_cancellation_during_livestream_stop_waits_for_one_cleanup_attempt():
    transport = LockedLivestreamStopTransport(
        ["var firmware_version='test';", "var outvolume=10;", "result=0;"]
    )
    await transport.stop_lock.acquire()
    camera = VStarcamCamera(config(), transport=transport)
    task = asyncio.create_task(
        camera.send_talk_audio(
            _payloads(_adpcm_frame()),
            duration=TALK_DURATION,
            max_speaker_volume=TALK_VOLUME_LIMIT,
            experimental=True,
            confirm=True,
        )
    )
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
    assert sum("streamid=16" in request for request in transport.requests) == 1
