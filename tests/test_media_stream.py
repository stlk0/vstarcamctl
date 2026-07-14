from __future__ import annotations

import json

import pytest

import vstarcamctl.media_stream as media_stream
from vstarcamctl.errors import MediaConfigurationError, MediaStreamError
from vstarcamctl.media_stream import (
    RTSPStream,
    build_play_command,
    build_probe_command,
    build_record_command,
    build_snapshot_command,
    capture_rtsp_snapshot,
    play_rtsp,
    probe_rtsp,
    record_rtsp,
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


async def test_live_playback_returns_only_sanitized_metadata(monkeypatch):
    async def fake_run(_command):
        return media_stream._ProcessResult(
            0,
            b"",
            b"rtsp://private-user:private-password@camera",
        )

    monkeypatch.setattr(media_stream, "_run_process_until_exit", fake_run)
    result = await play_rtsp(stream(), media="audio", duration=2, volume=35)
    assert result == {
        "media": "audio",
        "quality": "main",
        "duration_limit": 2,
        "local_volume": 35,
    }


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
        return media_stream._ProcessResult(0, json.dumps(payload).encode(), b"")

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
        return media_stream._ProcessResult(
            1,
            b"",
            b"rtsp://private-user:private-password@camera/tcp/av0_0: denied",
        )

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    with pytest.raises(MediaStreamError) as caught:
        await probe_rtsp(stream())
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)


async def test_record_and_snapshot_report_created_files(monkeypatch, tmp_path):
    async def fake_run(command, *, timeout):
        output = tmp_path / command[-1]
        output.write_bytes(b"media")
        return media_stream._ProcessResult(0, b"", b"")

    monkeypatch.setattr(media_stream, "_run_process", fake_run)
    clip = await record_rtsp(stream(), tmp_path / "clip.mkv", duration=2)
    frame = await capture_rtsp_snapshot(stream(), tmp_path / "frame.jpg")

    assert clip["bytes_written"] == 5
    assert clip["media"] == "both"
    assert frame["bytes_written"] == 5


async def test_record_refuses_implicit_overwrite(monkeypatch, tmp_path):
    target = tmp_path / "clip.mkv"
    target.write_bytes(b"existing")

    with pytest.raises(MediaConfigurationError, match="already exists"):
        await record_rtsp(stream(), target, duration=1)
