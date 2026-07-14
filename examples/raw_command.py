import argparse
import asyncio

from vstarcamctl import VStarcamCamera, load_config
from vstarcamctl.secrets import mask_secrets


async def run(path: str, experimental: bool) -> None:
    config = load_config()
    async with VStarcamCamera(config) as camera:
        response = await camera.send_raw_cgi(path, experimental=experimental)
        print(mask_secrets(response))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--experimental", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.experimental))
