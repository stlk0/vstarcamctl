# Python API

[Documentation index](index.md) · [Safety](safety.md) · [Back to README](../README.md)

The async API uses the same configuration, validation, masking, and command
policy as the CLI. `VStarcamCamera` is the main entry point.

## Configuration and lifecycle

```python
from vstarcamctl import VStarcamCamera, load_config


async def read_status():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        return await camera.get_status()
```

`load_config()` applies CLI-independent environment, local YAML, and default
settings. Pass an explicit path when an application uses a different config
location. Prefer the async context manager so transport cleanup always runs.

Manual lifecycle management is also available:

```python
camera = VStarcamCamera(config)
try:
    await camera.connect()
    status = await camera.get_status()
finally:
    await camera.close()
```

## Read methods

Common normalized reads include:

- `get_status()` and `get_params()`;
- `get_time_settings()`;
- `get_rtsp_settings()` and `get_onvif_settings()`;
- `get_audio_settings()`;
- `get_night_vision_settings()` and `get_infrared_light_settings()`;
- `get_motion_detection_settings()` and `get_human_detection_settings()`;
- `get_wifi_status()`.

`get_params()` returns a broad device response and may contain secrets. Mask or
select fields before logging, displaying, or returning it from an application.

## Confirmed writes

Confirmed controls do not need experimental approval:

```python
async with VStarcamCamera(config) as camera:
    await camera.set_siren(True)
    await camera.set_siren(False)
    await camera.set_light(True)
    await camera.set_light(False)
    await camera.set_time_settings(ntp_enabled=True)
```

Actuator calls still need application-level bounds and state verification. Do
not issue an automatic inverse after an ambiguous timeout.

## Guarded writes

Experimental and risky methods require explicit keyword arguments:

```python
async with VStarcamCamera(config) as camera:
    await camera.set_motion_detection(
        True,
        sensitivity=4,
        experimental=True,
        confirm=True,
    )
```

Other guarded methods include service settings, recording audio, camera volume,
talk, night vision, infrared light, human detection, Wi-Fi, and camera-account
password changes. Recovery-sensitive methods additionally require
`recovery_ready=True`.

The API validates these arguments against the command catalog. Do not bypass a
high-level method with `send_raw_cgi()` merely to avoid a safety gate.

## Raw requests

For supported relative paths:

```python
async with VStarcamCamera(config) as camera:
    response = await camera.send_raw_cgi("/get_status.cgi")
```

Unknown paths require `experimental=True`. Risky catalog entries also require
`confirm=True`; one-shot and recovery-sensitive commands should use their
high-level methods. Treat raw responses as sensitive until selected fields have
been masked.

## RTSP streams and recording

`get_rtsp_stream()` resolves a credential-safe stream object without printing
its URL:

```python
from vstarcamctl import VStarcamCamera, load_config, record_rtsp


async def record_clip():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        stream = await camera.get_rtsp_stream(quality="main", transport="tcp")
    return await record_rtsp(stream, "clip.mkv", duration=30)
```

Media helpers invoke FFmpeg tools as child processes. Credentials are masked in
application output, but they can be visible in the child process argument list
while it runs. Use media helpers only on a trusted local machine.

## Error handling

Catch the package exceptions appropriate to your application and distinguish an
explicit rejection from an uncertain network outcome. For writes, uncertainty
must be resolved by reading state or checking the device physically, not by an
unconditional retry.
