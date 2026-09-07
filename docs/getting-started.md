# Getting started

[Documentation index](index.md) · [Back to README](../README.md)

## Requirements

- Python 3.11–3.13. Python 3.14 is not currently supported.
- A VStarcam-compatible camera reachable from the same local network; see the
  [tested compatibility limits](../README.md#compatibility).
- Credentials for a camera you own or are authorized to control.
- FFmpeg, ffprobe, and ffplay only when using media commands.

The camera does not need WAN access for local control. Local firewalls must
allow the Python process to send and receive UDP traffic on the LAN.

This guide assumes the camera has already joined your LAN. For a reset camera,
see [experimental initial Wi-Fi provisioning](initial-wifi-provisioning.md).
QR generation accepts an optional account ID and uses the string `"0"` when
none is configured. It does not create a vendor account or retrieve control
credentials.

## Install

Before the first PyPI release, install from a source checkout. From the
repository root, create an environment with Python 3.11–3.13:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

PowerShell equivalents:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
```

After the first PyPI release, use `python -m pip install vstarcamctl` in place
of `python -m pip install .`. Check the installation with
`vstarcamctl --version` and `vstarcamctl --help`; neither contacts a camera.

For an editable development installation, use
`python -m pip install -e ".[provisioning,dev]"` from the repository root.

The `dev` extra is not needed for normal use. Install FFmpeg separately through
your operating system when you need stream probing, recording, snapshots,
listening, or talk. Initial Wi-Fi QR generation requires the optional
`provisioning` extra: `python -m pip install ".[provisioning]"` from the source
checkout, or `python -m pip install "vstarcamctl[provisioning]"` after release.

## Discover and create a profile

Run discovery while connected to the camera's LAN. An unambiguous result can be
saved directly as a new private profile:

```bash
vstarcamctl discover --save-config camera.local.yaml
```

The profile contains the discovered `host`, PPPP DID (`device_id`), current UDP
session port, and transport settings. Bare discovery always broadcasts and does
not inherit a possibly stale `host` or `device_id` from an existing default
profile. Only an explicit global `--host` narrows the probe to one address.

When `psk` is omitted, discovery tries only the three built-in VStarcam
transport profiles for encrypted responses. It accepts a decrypted result only
when the prefix of the returned PPPP DID matches the profile used to decode it.
A plaintext discovery response needs no seed and selects a known profile from
its DID when possible. Here `psk` means the seed for PPPP packet obfuscation; it
is not a camera, account, or Wi-Fi password. The virtual VUID printed on the
camera label or QR and the model name do not select a transport profile.

An explicitly supplied PSK overrides automatic profile selection. Use this for
an independently known custom encrypted profile or for an encrypted device-ID
prefix not covered by the built-in table:

```bash
vstarcamctl --psk '<known-transport-seed>' discover --save-config camera.local.yaml
```

`VSTARCAM_PSK` provides the same explicit override. A custom explicit seed is
saved in the new profile and reused for later connections. Built-in seeds are
resolved again from the saved `device_id`, so they need not be duplicated in
YAML.

### Known transport seeds

This compatibility list is intentionally non-exhaustive:

| PPPP DID prefixes | PSK | Validation status |
| --- | --- | --- |
| `VSTG`, `VSTH` | `vstarcam2018` | Live-confirmed on the tested VE-family target. |
| `VSTJ`, `VSTK`, `VSTL`, `VSTM`, `VSTN`, `VSTP` | `vstarcam2019` | APK-derived; not live-confirmed by this project. |
| `VSGG`, `VSGM`, `VSGS` | `vstarcam2021` | APK-derived; not live-confirmed by this project. |

Discovery does not try other seeds or contact a vendor service. An unknown
encrypted prefix requires an explicit PSK; a plaintext/legacy camera does not.

If two interfaces can route the camera subnet, bind discovery and the following
PPPP session to the intended local address. The saved profile keeps this value:

```bash
vstarcamctl --source-address '<local-ipv4>' discover --save-config camera.local.yaml
```

If several cameras answer, first inspect the discovered endpoints, then narrow
the second discovery to the intended host. Global options must precede the
command:

```bash
vstarcamctl discover
vstarcamctl --host '<discovered-host>' discover --save-config camera.local.yaml
```

Applications that already know the exact PPPP transport ID can instead use
`--device-id '<transport-device-id>'`. Do not pass the virtual VUID printed on
the camera label or QR; it is a different identifier.

Existing files are never replaced. For several cameras, create separately
named profiles such as `entrance.local.yaml` and `garage.local.yaml`, and pass
the intended one with `--config`.

## Add credentials

Discovery does not provide `password`, `account_id`, `login_hash`, or
`login_token`. Supply only credentials you are authorized to use. Prefer the
environment for the current local camera password:

```bash
export VSTARCAM_PASSWORD='current-camera-password'
```

PowerShell:

```powershell
$env:VSTARCAM_PASSWORD = "current-camera-password"
```

The username defaults to `admin`. Set `VSTARCAM_USERNAME` if the camera uses
another username. Use the local camera password, not a vendor-account or
Wi-Fi password.

Avoid `--password` for routine use because shell history and process listings
may expose it. Passwords after registration and factory-reset credentials vary
by camera and firmware; the library never guesses or tries a fallback.

## Make the first request

Use the protected `params` read to verify local control. It prints only the
number of returned fields:

```bash
vstarcamctl --config camera.local.yaml params
```

Then continue with normalized, read-only commands:

```bash
vstarcamctl --config camera.local.yaml device info
vstarcamctl --config camera.local.yaml time status
vstarcamctl --config camera.local.yaml rtsp status
vstarcamctl --config camera.local.yaml onvif status
vstarcamctl --config camera.local.yaml audio status
```

The Python API's `get_params()` method exposes the complete camera response,
which can contain network and device secrets; never paste that mapping into
logs, issues, or chat. The broader `status` command is also available, but its
success alone does not prove access to protected commands such as `params`.

## Advanced configuration

The discovery-created YAML profile is optional for Python applications, but is
convenient for persistent CLI settings. Supported fields include:

- `host`: current local camera address;
- `source_address`: optional local IPv4 address for a host with several LAN
  interfaces;
- `device_id`: PPPP transport device ID returned by LAN discovery;
- `username` and `password`: authorized camera credentials;
- `psk`: optional explicit PPPP transport-obfuscation seed override;
- `udp_port` and `discovery_port`: transport ports;
- `auth_mode`, `account_id`, `login_hash`, and `login_token`: authentication
  settings;
- `timeout` and `retries`: request behavior.

Every configuration field also has a `VSTARCAM_*` environment-variable form,
for example `device_id` maps to `VSTARCAM_DEVICE_ID`, `host` maps to
`VSTARCAM_HOST`, and `timeout` maps to `VSTARCAM_TIMEOUT`. Settings are resolved
in this order:

1. CLI options;
2. `VSTARCAM_*` environment variables;
3. local YAML;
4. built-in defaults.

Without `--config` or `VSTARCAM_CONFIG`, the CLI reads `config.local.yaml` in
the current directory when present. Environment overrides apply to every
selected profile, including when switching cameras.

## Authentication modes

`basic` uses the configured camera username and password and is the simplest
starting point.

`observed` additionally requires `account_id`, `login_hash`, and `login_token`
from an authorized local configuration; it still uses the local camera
username and password. Do not guess these values or copy them from
another account or device. Re-registering a camera can refresh the stored
device credential and account token, so replace stale private values from an
authorized source before diagnosing the local camera password.

`auto` is intentionally unavailable because cloud account authorization is
outside the local-only product scope.

## Common connection checks

A supported `result=-2` authentication refusal is reported as
`TransportAuthenticationError` in Python instead of a generic timeout and is
not retried. Check the authentication mode and current authorized credentials.
After an account change, follow the
[account recovery guidance](experimental-features.md#external-camera-account-webpwd).

If a read times out:

1. Confirm the computer and camera are on the same LAN.
2. Run `discover` again instead of assuming an old address or port.
3. Check the local firewall and VPN routing.
4. Verify the configured authentication mode and credentials.
5. Increase `--timeout` only after checking the network path.

Do not use a write as a connectivity test. Continue with the [CLI guide](cli.md)
after the read-only checks succeed.
