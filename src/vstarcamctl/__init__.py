"""Local control library for VStarcam-compatible PPPP/CGI cameras."""

from .camera import VStarcamCamera
from .config import VStarcamConfig, load_config
from .media_stream import (
    RTSPStream,
    capture_rtsp_snapshot,
    play_rtsp,
    probe_rtsp,
    record_rtsp,
)
from .parser import parse_vstarcam_response

__all__ = [
    "RTSPStream",
    "VStarcamCamera",
    "VStarcamConfig",
    "capture_rtsp_snapshot",
    "load_config",
    "parse_vstarcam_response",
    "play_rtsp",
    "probe_rtsp",
    "record_rtsp",
]
__version__ = "0.1.0"
