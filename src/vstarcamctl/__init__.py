"""Local control library for VStarcam-compatible PPPP/CGI cameras."""

from importlib.metadata import PackageNotFoundError, version

from .camera import VStarcamCamera
from .config import VStarcamConfig, load_config
from .discovery import discover_camera, discover_cameras
from .discovery_udp import LanDiscoveryResult, wait_for_camera_on_lan
from .media_stream import (
    RTSPStream,
    capture_h264_snapshot,
    capture_rtsp_snapshot,
    play_rtsp,
    probe_rtsp,
    record_rtsp,
)
from .parser import parse_vstarcam_response
from .provisioning import build_static_wifi_qr_payload, write_static_wifi_qr_svg
from .ptz import PTZDirection
from .transport import DiscoveredCamera

__all__ = [
    "DiscoveredCamera",
    "LanDiscoveryResult",
    "PTZDirection",
    "RTSPStream",
    "VStarcamCamera",
    "VStarcamConfig",
    "build_static_wifi_qr_payload",
    "capture_h264_snapshot",
    "capture_rtsp_snapshot",
    "discover_camera",
    "discover_cameras",
    "load_config",
    "parse_vstarcam_response",
    "play_rtsp",
    "probe_rtsp",
    "record_rtsp",
    "wait_for_camera_on_lan",
    "write_static_wifi_qr_svg",
]
try:
    __version__ = version("vstarcamctl")
except PackageNotFoundError:  # pragma: no cover - source tree without installation metadata
    __version__ = "0+unknown"
