import argparse
import asyncio
import json
from pathlib import Path

from vstarcamctl import VStarcamCamera, load_config, record_rtsp


async def run(output: Path, duration: float, quality: str) -> None:
    config = load_config()
    camera = VStarcamCamera(config)
    try:
        stream = await camera.get_rtsp_stream(quality=quality)
    finally:
        await camera.close()
    result = await record_rtsp(stream, output, duration=duration)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--quality", choices=("main", "sub"), default="main")
    args = parser.parse_args()
    asyncio.run(run(args.output, args.duration, args.quality))
