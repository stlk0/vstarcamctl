# VStarcamCtl

[![CI](https://github.com/stlk0/vstarcamctl/actions/workflows/ci.yml/badge.svg)](https://github.com/stlk0/vstarcamctl/actions/workflows/ci.yml)

> **Alpha.** APIs, supported models, and the command surface may change between
> releases.

VStarcamCtl is a local-only Python library and CLI for inspecting and controlling
VStarcam-compatible cameras over their PPPP/CGI command channel. Camera control
stays on the local network and does not require a vendor cloud connection.

## Disclaimer

This is an independent, unofficial project. It is not affiliated with, endorsed
by, or supported by VStarcam, O-KAM, Eye4, or any camera vendor. Product names
are used only to describe interoperability.

Use the software only with cameras you own or are explicitly authorized to
control. Device writes can change camera behavior and some settings can require
a physical reset to recover. The software is provided without warranty; see
the [MIT License](LICENSE).

## Highlights

- Encrypted LAN discovery and local camera sessions.
- Async Python API and the `vstarcamctl` command-line interface.
- Normalized status for camera, time, media, detection, and Wi-Fi settings.
- Confirmed siren, white-light, and time controls.
- RTSP probing, snapshots, bounded recording, and audio playback.
- A catalog-backed safety policy for unconfirmed and risky commands.
- Secret masking in command output and dry-run previews.
- Unit tests that do not contact a real camera.

## Requirements

- Python 3.11 or newer.
- A compatible camera reachable on the same local network.
- Authorized camera credentials.
- FFmpeg tools only for video, recording, snapshots, listening, or talk.

## Install from a checkout

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[transport]"
cp config.example.yaml config.local.yaml
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and copy the
configuration with `Copy-Item config.example.yaml config.local.yaml`.

Store the camera password in `VSTARCAM_PASSWORD` rather than a command-line
argument. `config.local.yaml` and `.env` are ignored by Git. See the
[getting-started guide](docs/getting-started.md) for configuration precedence,
authentication modes, and first-connection checks.

## Quick start

Global options such as `--config`, `--timeout`, and `--retries` must appear
before the command.

```bash
vstarcamctl discover
vstarcamctl --config config.local.yaml status
vstarcamctl --config config.local.yaml time status
vstarcamctl --config config.local.yaml rtsp status
vstarcamctl --config config.local.yaml stream probe
```

Confirmed actuator commands should still be bounded and should not be retried
automatically:

```bash
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 light on
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 light off
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 siren on
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 siren off
```

## Safety model

Read-only and confirmed commands run normally. Unconfirmed commands require
`--experimental`; risky commands also require `--confirm`. Wi-Fi and camera
password changes additionally require `--recovery-ready` and a verified
physical recovery procedure.

A timed-out write has an unknown outcome. Check the corresponding status and
physical effect before considering another write. Never paste full `params`
output into an issue: it may contain device and network secrets.

Read [Safety](docs/safety.md) before using actuators, raw commands, experimental
features, Wi-Fi changes, or talk audio.

## Documentation

- [Documentation index](docs/index.md)
- [Getting started](docs/getting-started.md)
- [CLI guide](docs/cli.md)
- [Experimental features](docs/experimental-features.md)
- [Python API](docs/python-api.md)
- [Safety](docs/safety.md)

The CLI also provides contextual help:

```bash
vstarcamctl --help
vstarcamctl audio --help
vstarcamctl stream record --help
```

## Python API

```python
from vstarcamctl import VStarcamCamera, load_config


async def read_camera():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        return await camera.get_status()
```

The high-level API applies the same command policy as the CLI. See the
[Python API guide](docs/python-api.md) for lifecycle, media, and guarded-write
examples.

## Compatibility

The core command path has been validated with a VStarcam-compatible VE-family
camera. Camera models and firmware vary, so read status before writing and
treat experimental features as model-specific until verified on your device.
WAN access is not required for local operation.

## Development

Development dependencies are separate from the user installation:

```bash
python -m pip install -e ".[transport,dev]"
pytest -q
```

Real-camera integration tests are opt-in and must never be run without an
authorized target and a recovery plan. Contributions through issues and pull
requests are welcome.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
Conventional Commit rules. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md), report vulnerabilities through the
private process in [SECURITY.md](SECURITY.md), and review notable changes in
the [changelog](CHANGELOG.md).
