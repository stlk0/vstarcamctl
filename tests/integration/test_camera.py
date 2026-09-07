from __future__ import annotations

import os

import pytest

from vstarcamctl.camera import VStarcamCamera
from vstarcamctl.config import load_config

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("VSTARCAM_INTEGRATION") != "1", reason="real camera test disabled"
)
async def test_real_camera_returns_validated_status_and_protected_image_settings():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        software = await camera.get_device_software_info()
        image = await camera.get_image_adjustments()
        assert software["available"] is True
        assert image["available"] is True
