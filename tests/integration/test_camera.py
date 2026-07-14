from __future__ import annotations

import os

import pytest

from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import load_config

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("VSTARCAM_INTEGRATION") != "1", reason="real camera test disabled"
)
async def test_real_camera_status_and_params_are_non_empty():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        assert await camera.get_status()
        assert await camera.get_params()
