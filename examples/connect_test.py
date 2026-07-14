import asyncio
import json

from vstarcamctl import VStarcamCamera, load_config


async def main() -> None:
    config = load_config()
    async with VStarcamCamera(config) as camera:
        print(json.dumps(await camera.get_status(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
