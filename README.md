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
the [MIT License](https://github.com/stlk0/vstarcamctl/blob/main/LICENSE).

## Highlights

- PPPP-compatible LAN discovery and sessions using the cameras' legacy XOR
  obfuscation. This is protocol compatibility, not cryptographic protection.
- Async Python API and the `vstarcamctl` command-line interface.
- Camera status and normalized image, time, media, detection, and Wi-Fi settings.
- Allowlisted device software information without identity or network fields.
- Confirmed siren, white-light, and time controls.
- Bounded experimental four-way pan/tilt control for motorized models.
- Confirmed, model-aware 12/24-hour video-overlay clock status and control.
- Camera logo visibility and siren/white-light/alarm-indicator status, with
  experimental logo and alarm-indicator control.
- RTSP probing, bounded recording, and audio playback, plus experimental
  bounded PPPP snapshots.
- A catalog-backed safety policy for unconfirmed and risky commands.
- Secret masking in command output and dry-run previews.
- Unit tests that do not contact a real camera.

## Requirements

- Python 3.11–3.13. Python 3.14 is not currently supported.
- A compatible camera reachable on the same local network.
- Authorized camera credentials.
- FFmpeg tools only for video, recording, snapshots, listening, or talk.

## Installation

Before the first PyPI release, install from the repository root using
Python 3.11–3.13:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

For PowerShell activation, use `.venv\Scripts\Activate.ps1`. Add the optional
QR renderer with `python -m pip install ".[provisioning]"`.

After the first PyPI release, install the published package instead:

```bash
python -m pip install vstarcamctl
```

Use `"vstarcamctl[provisioning]"` for the optional QR renderer.

## Quick start

With the camera already on your LAN, discover it to create a private profile,
provide its current local password, and make a protected read:

```bash
vstarcamctl discover --save-config camera.local.yaml
export VSTARCAM_PASSWORD='current-camera-password'
vstarcamctl --config camera.local.yaml params
```

PowerShell uses
`$env:VSTARCAM_PASSWORD = "current-camera-password"`. The username defaults to
`admin`; set `VSTARCAM_USERNAME` if yours differs. Discovery supplies the
current `host`, PPPP DID (`device_id`), UDP session port, and matching known
transport profile; it never discovers credentials. If several cameras answer,
run `vstarcamctl discover` without `--save-config`, choose the intended host
from the returned list, and repeat with the global `--host` option. The virtual
VUID printed on a label or QR is a different identifier and cannot select a LAN
discovery result. Existing profiles are never replaced.

Global options such as `--config`, `--timeout`, and `--retries` must appear
before the command. Continue with read-only commands:

```bash
vstarcamctl --config camera.local.yaml status
vstarcamctl --config camera.local.yaml device info
vstarcamctl --config camera.local.yaml time status
vstarcamctl --config camera.local.yaml rtsp status
```

On the tested camera, local CGI reads work after account-free Wi-Fi onboarding
and after first `WebPwd` enable and restart. In `basic` mode, omit `account_id`
or use `"0"`; the library handles the required local authentication steps.
The guarded first-enable workflow can activate RTSP without a vendor account.
Keep the local CGI password separate from the new RTSP password. See the
[account and RTSP workflow](https://github.com/stlk0/vstarcamctl/blob/main/docs/experimental-features.md#external-camera-account-webpwd)
for this experimental, model-dependent setup.

For playback and recording, see the
[CLI guide](https://github.com/stlk0/vstarcamctl/blob/main/docs/cli.md#camera-media).
PPPP snapshots start and stop a livestream and remain
[experimental](https://github.com/stlk0/vstarcamctl/blob/main/docs/experimental-features.md#pppp-snapshot).

See the
[getting-started guide](https://github.com/stlk0/vstarcamctl/blob/main/docs/getting-started.md)
for multiple cameras, configuration precedence, authentication modes, and
first-connection checks.

## Safety model

Read-only and confirmed commands run normally. Unconfirmed commands require
`--experimental`; risky commands also require `--confirm`. Wi-Fi and camera
password changes additionally require `--recovery-ready` and a verified
physical recovery procedure.

A timed-out write has an unknown outcome. Check the corresponding status and
physical effect before considering another write. Siren and light writes are
sent once. `--timeout` limits response waiting; it does not turn an actuator
off. Use its separate `off` command to stop it. The CLI `params` command
prints only a field count; the Python API's full `get_params()` result may
contain device and network secrets and must not be published.

The PPPP XOR transform does not provide modern confidentiality, integrity, or
peer authentication. Use the library only on a trusted, preferably isolated,
LAN. Read [Safety](https://github.com/stlk0/vstarcamctl/blob/main/docs/safety.md)
before using actuators, raw commands, experimental features, Wi-Fi changes, or
talk audio.

## Documentation

- [Documentation index](https://github.com/stlk0/vstarcamctl/blob/main/docs/index.md)
- [Getting started](https://github.com/stlk0/vstarcamctl/blob/main/docs/getting-started.md)
- [CLI guide](https://github.com/stlk0/vstarcamctl/blob/main/docs/cli.md)
- [Initial Wi-Fi provisioning](https://github.com/stlk0/vstarcamctl/blob/main/docs/initial-wifi-provisioning.md)
- [Experimental features](https://github.com/stlk0/vstarcamctl/blob/main/docs/experimental-features.md)
- [Python API](https://github.com/stlk0/vstarcamctl/blob/main/docs/python-api.md)
- [Safety](https://github.com/stlk0/vstarcamctl/blob/main/docs/safety.md)
- [Maintainer release guide](https://github.com/stlk0/vstarcamctl/blob/main/docs/releasing.md)

The CLI also provides contextual help:

```bash
vstarcamctl --help
vstarcamctl --version
vstarcamctl audio --help
vstarcamctl stream record --help
```

## Python API

```python
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
```

This path requires no YAML file. The high-level API applies the same command
policy as the CLI. See the
[Python API guide](https://github.com/stlk0/vstarcamctl/blob/main/docs/python-api.md)
for discovery selection, persistent configuration, lifecycle, media, and
guarded-write examples.

## Compatibility

The core command path has been validated with a VStarcam-compatible VE-family
camera. Camera models and firmware vary, so read status before writing and
treat experimental features as model-specific until verified on your device.
When `psk` is omitted, LAN discovery tries only the built-in
`vstarcam2018`, `vstarcam2019`, and `vstarcam2021` transport profiles and
validates each decrypted result against the returned PPPP DID prefix. A
plaintext discovery response needs no seed and selects a known profile from its
DID when possible. `vstarcam2018` is live-confirmed on the tested VE-family;
`vstarcam2019` and `vstarcam2021` are APK-derived compatibility profiles that
are not live-confirmed by this project. An explicit `psk` overrides automatic
profile selection and is required for an unknown encrypted device-ID prefix. A
transport PSK is a packet-obfuscation seed, not a camera, account, or Wi-Fi
password. The
[known transport-seed values](https://github.com/stlk0/vstarcamctl/blob/main/docs/getting-started.md#known-transport-seeds)
are non-exhaustive and have different validation status. WAN access is not
required for local operation.

LAN discovery can occasionally time out on the tested camera. Its advertised
session port can differ from the saved profile, so a fallback connection may
also time out. Rediscover the camera and retry a read; never repeat a write
whose outcome is unknown.

## Development

Development dependencies are separate from the user installation:

```bash
python -m pip install -e ".[provisioning,dev]"
pytest -q
```

Real-camera integration tests are opt-in and must never be run without an
authorized target and a recovery plan. Contributions through issues and pull
requests are welcome.

See [CONTRIBUTING.md](https://github.com/stlk0/vstarcamctl/blob/main/CONTRIBUTING.md)
for the development workflow and
Conventional Commit rules. Please follow the
[Code of Conduct](https://github.com/stlk0/vstarcamctl/blob/main/CODE_OF_CONDUCT.md),
report vulnerabilities through the private process in
[SECURITY.md](https://github.com/stlk0/vstarcamctl/blob/main/SECURITY.md), and
review notable changes in the
[changelog](https://github.com/stlk0/vstarcamctl/blob/main/CHANGELOG.md).
