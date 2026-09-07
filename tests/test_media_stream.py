from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import vstarcamctl.media_stream as media_stream
from vstarcamctl.errors import MediaConfigurationError, MediaStreamError
from vstarcamctl.media_stream import (
    RTSPStream,
    build_h264_snapshot_command,
    build_play_command,
    build_probe_command,
    build_record_command,
    build_snapshot_command,
    capture_h264_snapshot,
    capture_rtsp_snapshot,
    play_rtsp,
    prepare_media_output,
    probe_rtsp,
    record_rtsp,
)

H264_KEY_ACCESS_UNIT = (
    b"\x00\x00\x00\x01\x67\x64\x00\x1f\x00\x00\x01\x68\xee\x3c\x80\x00\x00\x00\x01\x65\x88\x84"
)


def stream(**overrides) -> RTSPStream:
    values = {
        "host": "192.0.2.20",
        "port": 10554,
        "username": "stream user",
        "password": "p+a:ss/word",
        "quality": "main",
        "transport": "tcp",
    }
    values.update(overrides)
    return RTSPStream(**values)


def test_rtsp_url_uses_camera_paths_and_percent_encodes_credentials():
    main = stream()
    sub = stream(quality="sub", transport="udp")

    assert main.url == ("rtsp://stream%20user:p%2Ba%3Ass%2Fword@192.0.2.20:10554/tcp/av0_0")
    assert sub.url.endswith("/udp/av0_1")
    assert sub.redacted_url == "rtsp://***:***@192.0.2.20:10554/udp/av0_1"
    assert "stream user" not in repr(main)
    assert "p+a:ss/word" not in repr(main)


@pytest.mark.parametrize(
    "overrides",
    [
        {"port": 0},
        {"quality": "third"},
        {"transport": "http"},
        {"host": "user@camera"},
        {"username": "only-one", "password": None},
    ],
)
def test_rtsp_stream_rejects_invalid_connection_details(overrides):
    with pytest.raises(MediaConfigurationError):
        stream(**overrides)


def test_ffmpeg_commands_select_video_and_audio_without_a_shell(tmp_path):
    target = tmp_path / "clip.mkv"
    current = stream()

    probe = build_probe_command(current)
    record = build_record_command(
        current,
        target,
        duration=5,
        media="both",
    )
    snapshot = build_snapshot_command(current, tmp_path / "frame.jpg")
    play = build_play_command(current, media="audio", duration=5, volume=40)

    assert probe[0] == "ffprobe"
    assert probe[-1] == current.url
    assert record[0] == "ffmpeg"
    assert record[record.index("-i") + 1] == current.url
    assert record.count("-map") == 2
    assert "0:v:0?" in record
    assert "0:a:0?" in record
    assert record[-2:] == ["-n", str(target)]
    assert snapshot[-4:-2] == ["-frames:v", "1"]
    assert play[0] == "ffplay"
    assert play[-1] == current.url
    assert "-nodisp" in play and "-vn" in play
    assert play[play.index("-volume") + 1] == "40"


@pytest.mark.parametrize(
    "value",
    [True, "1", float("nan"), float("inf"), 10**1000, 0, -1],
)
def test_media_duration_builders_reject_non_finite_or_non_numeric_values(value, tmp_path):
    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        build_record_command(stream(), tmp_path / "clip.mkv", duration=value)
    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        build_play_command(stream(), duration=value)


@pytest.mark.parametrize(
    "value",
    [True, "1", float("nan"), float("inf"), 10**1000, 0, -1],
)
async def test_media_timeouts_reject_non_finite_or_non_numeric_values(
    value,
    monkeypatch,
    tmp_path,
):
    async def unexpected_start(*_args, **_kwargs):
        raise AssertionError("media process must not start for an invalid timeout")

    monkeypatch.setattr(media_stream, "start_media_process", unexpected_start)

    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        await probe_rtsp(stream(), timeout=value)
    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        await capture_rtsp_snapshot(stream(), tmp_path / "rtsp.jpg", timeout=value)
    with pytest.raises(MediaConfigurationError, match="finite and greater than zero"):
        await capture_h264_snapshot(
            H264_KEY_ACCESS_UNIT,
            tmp_path / "pppp.jpg",
            timeout=value,
        )


def test_h264_snapshot_command_forces_annex_b_stdin_and_one_video_frame(tmp_path):
    output = tmp_path / "frame.jpg"

    assert build_h264_snapshot_command(output) == [
        "ffmpeg",
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
        "-n",
        str(output),
    ]


def test_media_output_can_be_preflighted_before_starting_a_stream(tmp_path):
    output = tmp_path / "frame.jpg"
    output.write_bytes(b"existing")

    with pytest.raises(MediaConfigurationError, match="already exists"):
        prepare_media_output(output)
    assert prepare_media_output(output, overwrite=True) == output
    with pytest.raises(MediaConfigurationError, match="directory does not exist"):
        prepare_media_output(tmp_path / "missing" / "frame.jpg")
    with pytest.raises(MediaConfigurationError, match="not a directory"):
        prepare_media_output(output / "frame.jpg")
    for overwrite in (False, True):
        with pytest.raises(MediaConfigurationError, match="output must be a file"):
            prepare_media_output(tmp_path, overwrite=overwrite)


@pytest.mark.parametrize("output", ["-report", "file:clip.mkv", "tcp:127.0.0.1:1"])
async def test_media_outputs_are_local_paths_not_ffmpeg_options_or_protocols(
    output, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    async def check_destination(command, **_kwargs):
        assert Path(command[-1]).is_absolute()
        assert command[-1] == str(tmp_path / output)
        raise MediaStreamError("local destination checked")

    monkeypatch.setattr(media_stream, "_run_process", check_destination)
    with pytest.raises(MediaStreamError, match="local destination checked"):
        await record_rtsp(stream(), output, duration=1)
    with pytest.raises(MediaStreamError, match="local destination checked"):
        await capture_rtsp_snapshot(stream(), output)
    with pytest.raises(MediaStreamError, match="local destination checked"):
        await capture_h264_snapshot(H264_KEY_ACCESS_UNIT, output)


@pytest.mark.parametrize("duration", [2, None])
async def test_live_playback_bounds_duration_and_returns_only_sanitized_metadata(
    monkeypatch, duration
):
    async def fake_run(_command, *, timeout):
        if duration is None:
            assert timeout is None
        else:
            assert duration < timeout <= duration + 20
        return media_stream._ProcessResult(0, b"")

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    result = await play_rtsp(stream(), media="audio", duration=duration, volume=35)
    assert result == {
        "media": "audio",
        "quality": "main",
        "duration_limit": duration,
        "local_volume": 35,
    }


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancelled"])
async def test_interrupted_media_operation_reaps_its_child_process(monkeypatch, cancel):
    process = None
    started = asyncio.Event()

    async def start(_command, **_kwargs):
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        assert line.rstrip(b"\r\n") == b"ready"
        started.set()
        return process

    monkeypatch.setattr(media_stream, "start_media_process", start)
    operation = asyncio.create_task(probe_rtsp(stream(), timeout=10 if cancel else 0.01))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if cancel:
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, timeout=5)
        else:
            with pytest.raises(MediaStreamError, match="ffprobe timed out"):
                await asyncio.wait_for(operation, timeout=5)
        assert process.returncode is not None
    finally:
        operation.cancel()
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), timeout=5)


async def test_probe_returns_only_sanitized_codec_metadata(monkeypatch):
    async def fake_run(_command, *, timeout):
        assert timeout == 4
        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "15/1",
                    "tags": {"secret": "must-not-pass-through"},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "pcm_alaw",
                    "sample_rate": "8000",
                    "channels": 1,
                    "channel_layout": "mono",
                },
            ],
            "format": {"format_name": "rtsp", "filename": "credentialed-url"},
        }
        return media_stream._ProcessResult(0, json.dumps(payload).encode())

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    result = await probe_rtsp(stream(), timeout=4)

    assert result == {
        "streams": [
            {
                "index": 0,
                "type": "video",
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 15.0,
            },
            {
                "index": 1,
                "type": "audio",
                "codec": "pcm_alaw",
                "sample_rate": "8000",
                "channels": 1,
                "channel_layout": "mono",
            },
        ],
        "format": "rtsp",
    }
    assert "secret" not in json.dumps(result)
    assert "credentialed-url" not in json.dumps(result)


async def test_probe_failure_does_not_echo_ffmpeg_stderr(monkeypatch):
    async def fake_run(_command, *, timeout):
        return media_stream._ProcessResult(1, b"", "authentication rejected")

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    with pytest.raises(MediaStreamError) as caught:
        await probe_rtsp(stream())
    assert "authentication rejected" in str(caught.value)
    assert "stream user" not in str(caught.value)
    assert "p+a:ss/word" not in str(caught.value)


async def test_snapshot_failure_reports_safe_connection_category(monkeypatch, tmp_path):
    async def fake_run(_command, *, timeout):
        return media_stream._ProcessResult(1, b"", "connection refused")

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    with pytest.raises(MediaStreamError, match="connection refused") as caught:
        await capture_rtsp_snapshot(stream(), tmp_path / "frame.jpg")
    assert "stream user" not in str(caught.value)
    assert "p+a:ss/word" not in str(caught.value)


async def test_h264_snapshot_passes_only_validated_bytes_to_opt_in_stdin(monkeypatch, tmp_path):
    output = tmp_path / "frame.jpg"
    seen = {}

    class Process:
        returncode = 0

        async def communicate(self, *, input):
            seen["input"] = input
            output.write_bytes(b"image")
            return b"", b""

    async def start(command, *, capture_stderr, pipe_stdin):
        seen.update(command=command, capture_stderr=capture_stderr, pipe_stdin=pipe_stdin)
        return Process()

    monkeypatch.setattr(media_stream, "start_media_process", start)

    result = await capture_h264_snapshot(H264_KEY_ACCESS_UNIT, output, timeout=2)

    assert seen["input"] == H264_KEY_ACCESS_UNIT
    assert seen["pipe_stdin"] is True
    assert seen["capture_stderr"] is True
    assert seen["command"] == build_h264_snapshot_command(output)
    assert result == {
        "path": str(output.resolve()),
        "bytes_written": 5,
        "codec": "h264",
        "source": "pppp",
    }


@pytest.mark.parametrize(
    "access_unit",
    [
        b"not-annex-b",
        b"\x00\x00\x01\x68\x01\x00\x00\x01\x65\x01",
        b"\x00\x00\x01\x67\x01\x00\x00\x01\x65\x01",
        b"\x00\x00\x01\x67\x01\x00\x00\x01\x68\x01",
    ],
)
async def test_h264_snapshot_rejects_non_key_access_units_without_starting_ffmpeg(
    access_unit,
    monkeypatch,
    tmp_path,
):
    async def unexpected_start(*_args, **_kwargs):
        raise AssertionError("FFmpeg must not start for malformed input")

    monkeypatch.setattr(media_stream, "start_media_process", unexpected_start)

    with pytest.raises(MediaStreamError):
        await capture_h264_snapshot(access_unit, tmp_path / "frame.jpg")


async def test_h264_snapshot_timeout_stops_process_without_exposing_input(monkeypatch, tmp_path):
    stopped = False

    class Process:
        returncode = None

        async def communicate(self, *, input):
            assert input == H264_KEY_ACCESS_UNIT
            await media_stream.asyncio.Future()

    async def start(_command, *, capture_stderr, pipe_stdin):
        assert capture_stderr and pipe_stdin
        return Process()

    async def stop(_process):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(media_stream, "start_media_process", start)
    monkeypatch.setattr(media_stream, "stop_process", stop)

    with pytest.raises(MediaStreamError, match="decoding the H.264 access unit") as caught:
        await capture_h264_snapshot(
            H264_KEY_ACCESS_UNIT,
            tmp_path / "frame.jpg",
            timeout=0.001,
        )
    assert stopped
    assert H264_KEY_ACCESS_UNIT.hex() not in str(caught.value)


async def test_h264_snapshot_failure_exposes_only_classified_stderr(monkeypatch, tmp_path):
    secret = "private-camera-credential"

    class Process:
        returncode = 1

        async def communicate(self, *, input):
            assert input == H264_KEY_ACCESS_UNIT
            return b"", f"{secret}: Invalid data found when processing input".encode()

    async def start(_command, *, capture_stderr, pipe_stdin):
        assert capture_stderr and pipe_stdin
        return Process()

    monkeypatch.setattr(media_stream, "start_media_process", start)

    with pytest.raises(MediaStreamError, match="invalid stream data") as caught:
        await capture_h264_snapshot(H264_KEY_ACCESS_UNIT, tmp_path / "frame.jpg")
    assert secret not in str(caught.value)


async def test_h264_snapshot_requires_a_non_empty_output(monkeypatch, tmp_path):
    class Process:
        returncode = 0

        async def communicate(self, *, input):
            assert input == H264_KEY_ACCESS_UNIT
            return b"", b""

    async def start(_command, *, capture_stderr, pipe_stdin):
        return Process()

    monkeypatch.setattr(media_stream, "start_media_process", start)

    with pytest.raises(MediaStreamError, match="non-empty snapshot"):
        await capture_h264_snapshot(H264_KEY_ACCESS_UNIT, tmp_path / "frame.jpg")


async def test_record_and_snapshot_report_created_files(monkeypatch, tmp_path):
    async def fake_run(command, *, timeout):
        output = tmp_path / command[-1]
        output.write_bytes(b"media")
        return media_stream._ProcessResult(0, b"")

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    clip = await record_rtsp(stream(), tmp_path / "clip.mkv", duration=2)
    frame = await capture_rtsp_snapshot(stream(), tmp_path / "frame.jpg")

    assert clip["bytes_written"] == 5
    assert clip["media"] == "both"
    assert frame["bytes_written"] == 5


async def test_record_refuses_implicit_overwrite(tmp_path):
    target = tmp_path / "clip.mkv"
    target.write_bytes(b"existing")

    with pytest.raises(MediaConfigurationError, match="already exists"):
        await record_rtsp(stream(), target, duration=1)
