"""Discover one LAN camera and read software versions using VSTARCAM_PASSWORD."""

import asyncio
import os

from vstarcamctl import VStarcamCamera, discover_camera


async def main() -> None:
    password = os.environ["VSTARCAM_PASSWORD"]
    discovered = await discover_camera()
    async with VStarcamCamera.from_discovery(
        discovered,
        password=password,
    ) as camera:
        info = await camera.get_device_software_info()
    print(info)


if __name__ == "__main__":
    asyncio.run(main())
