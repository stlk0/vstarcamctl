import argparse
import asyncio

from vstarcamctl import VStarcamCamera, load_config


async def run(path: str, experimental: bool, confirm: bool) -> None:
    config = load_config()
    async with VStarcamCamera(config) as camera:
        response = await camera.send_raw_cgi(
            path,
            experimental=experimental,
            confirm=confirm,
        )
        print(f"camera returned {len(response.encode('utf-8'))} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--experimental", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.experimental, args.confirm))
