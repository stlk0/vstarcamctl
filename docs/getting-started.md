# Getting started

[Documentation index](index.md) · [Back to README](../README.md)

## Requirements

- Python 3.11 or newer.
- A supported camera reachable from the same local network.
- Credentials for a camera you own or are authorized to control.
- FFmpeg, ffprobe, and ffplay only when using media commands.

The camera does not need WAN access for local control. Local firewalls must
allow the Python process to send and receive UDP traffic on the LAN.

## Install

From a repository checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[transport]"
cp config.example.yaml config.local.yaml
```

PowerShell equivalents:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[transport]"
Copy-Item config.example.yaml config.local.yaml
```

The `dev` extra is not needed for normal use. Install FFmpeg separately through
your operating system when you need stream probing, recording, snapshots,
listening, or talk.

## Configure

Edit the ignored `config.local.yaml` copy. The main fields are:

- `host`: current local camera address;
- `vuid`: camera identifier;
- `username` and `password`: authorized camera credentials;
- `auth_mode`: `basic` or `observed`;
- `timeout` and `retries`: request behavior.

Keep the transport-related defaults from
[`config.example.yaml`](../config.example.yaml) unless your camera requires a
different documented value.

Prefer environment variables for secrets:

```bash
export VSTARCAM_PASSWORD='replace-with-camera-password'
```

PowerShell:

```powershell
$env:VSTARCAM_PASSWORD = "replace-with-camera-password"
```

Available examples are listed in [`.env.example`](../.env.example). Settings
are resolved in this order:

1. CLI options;
2. `VSTARCAM_*` environment variables;
3. local YAML;
4. built-in defaults.

Avoid `--password` for routine use because shell history and process listings
may expose it.

## Authentication modes

`basic` uses the configured camera username and password and is the simplest
starting point.

`observed` requires `account_id`, `login_hash`, and `login_token` from an
authorized local configuration. Do not guess these values or copy them from
another account or device.

`auto` is intentionally unavailable because cloud account authorization is
outside the local-only product scope.

## Discover the camera

Run discovery while connected to the camera's LAN:

```bash
vstarcamctl discover
```

Discovery output is masked. Update `host` or other non-secret connection fields
in `config.local.yaml` if needed.

## Make the first request

Begin with normalized, read-only commands:

```bash
vstarcamctl --config config.local.yaml status
vstarcamctl --config config.local.yaml time status
vstarcamctl --config config.local.yaml rtsp status
vstarcamctl --config config.local.yaml onvif status
vstarcamctl --config config.local.yaml audio status
```

The `params` command exposes a broad camera response that can contain network
and device secrets. Use it only locally and never paste the complete output
into logs, issues, or chat.

## Common connection checks

If a read times out:

1. Confirm the computer and camera are on the same LAN.
2. Run `discover` again instead of assuming an old address or port.
3. Check the local firewall and VPN routing.
4. Verify the configured authentication mode and credentials.
5. Increase `--timeout` only after checking the network path.

Do not use a write as a connectivity test. Continue with the [CLI guide](cli.md)
after the read-only checks succeed.
