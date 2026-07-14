# CLI guide

[Documentation index](index.md) · [Back to README](../README.md)

## Command layout

The general form is:

```text
vstarcamctl [global options] command [command options]
```

Global options must precede the command:

```bash
vstarcamctl --config config.local.yaml --timeout 8 status
```

Use `vstarcamctl --help` for all command groups and add `--help` after any group
or subcommand for its exact options.

## Command overview

| Command group | Purpose |
| --- | --- |
| `discover` | Find compatible cameras on the local network. |
| `status`, `params` | Read general device state. |
| `siren`, `light`, `time` | Use confirmed controls and inspect time settings. |
| `rtsp`, `onvif`, `audio` | Inspect services, recording audio, volume, and live audio. |
| `night`, `ir` | Inspect or configure night vision and the infrared light. |
| `motion`, `human` | Inspect or configure detection features. |
| `stream` | Probe, record, or snapshot RTSP media. |
| `wifi` | Inspect, scan, or configure Wi-Fi. |
| `account` | Guarded camera-account maintenance. |
| `raw` | Send a catalog-checked relative CGI path. |

## Read-only status

These commands normalize common fields and avoid printing complete camera
responses:

```bash
vstarcamctl --config config.local.yaml status
vstarcamctl --config config.local.yaml time status
vstarcamctl --config config.local.yaml rtsp status
vstarcamctl --config config.local.yaml onvif status
vstarcamctl --config config.local.yaml audio status
vstarcamctl --config config.local.yaml night status
vstarcamctl --config config.local.yaml ir status
vstarcamctl --config config.local.yaml motion status
vstarcamctl --config config.local.yaml human status
vstarcamctl --config config.local.yaml wifi status
```

`params` is intentionally broad and may contain credentials, identifiers,
network names, and addresses. Do not publish its complete output.

## Siren and white light

These confirmed actuators do not require experimental flags. Bound each attempt
and disable automatic retry:

```bash
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 siren on
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 siren off
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 light on
vstarcamctl --config config.local.yaml --timeout 8 --retries 0 light off
```

If acknowledgement is lost, inspect the camera physically before issuing an
inverse command.

## Time and NTP

Read the normalized clock, fixed UTC offset, NTP state, and server:

```bash
vstarcamctl --config config.local.yaml time status
```

Synchronize the clock while preserving omitted settings:

```bash
vstarcamctl --config config.local.yaml time set
vstarcamctl --config config.local.yaml time set --timezone UTC+05:30 --ntp on --ntp-server time.example.net
```

The camera uses a fixed UTC offset rather than an automatic daylight-saving
timezone. Run `time status` after a setting attempt.

## RTSP media

Install FFmpeg tools before using this section.

```bash
vstarcamctl --config config.local.yaml stream probe
vstarcamctl --config config.local.yaml stream snapshot frame.jpg
vstarcamctl --config config.local.yaml stream record clip.mkv --duration 30
vstarcamctl --config config.local.yaml stream record audio.mka --duration 30 --media audio
vstarcamctl --config config.local.yaml audio listen --duration 30 --local-volume 70
```

The default is the main stream over TCP. Use `--quality sub` for lower bandwidth
or `--rtsp-transport udp` when required. Recording remuxes available streams and
does not overwrite an existing file unless `--overwrite` is supplied.

When no RTSP port is supplied, the CLI reads current camera settings and selects
the configured credential source. An explicit port also requires an explicit
username and password, preferably through `VSTARCAM_RTSP_USERNAME` and
`VSTARCAM_RTSP_PASSWORD`.

## Raw commands

`raw` accepts only a relative CGI path. Known paths inherit their catalog safety
policy; unknown paths require `--experimental`, and risky paths require
`--confirm`.

```bash
vstarcamctl --config config.local.yaml raw --dry-run "/get_status.cgi"
vstarcamctl --config config.local.yaml raw "/get_status.cgi"
```

Authentication fields in a supplied path are replaced by central configuration.
Dry runs do not connect and mask sensitive values. Prefer high-level commands
because they validate inputs, preserve adjacent settings, and apply one-shot
behavior where needed.

For guarded writes, continue with [Experimental features](experimental-features.md).
