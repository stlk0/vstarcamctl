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
| `device` | Read an allowlisted software-version summary. |
| `imaging` | Read reported brightness and contrast. |
| `siren`, `light`, `time` | Use confirmed controls and inspect time settings. |
| `rtsp`, `onvif`, `audio` | Inspect services, recording audio, volume, and live audio. |
| `night`, `ir` | Inspect or configure night vision and the infrared light. |
| `alarm-led` | Inspect or configure the red/blue alarm indicator. |
| `motion`, `human` | Inspect or configure detection features. |
| `ptz` | Run bounded manual pan/tilt movement or send an explicit stop. |
| `osd` | Inspect or configure the video-overlay clock format and camera logo. |
| `stream` | Probe or record RTSP media, or take an RTSP/PPPP snapshot. |
| `wifi` | Inspect, scan, or configure Wi-Fi. |
| `provision` | Create an experimental initial Wi-Fi setup QR locally. |
| `account` | Guarded camera-account maintenance. |
| `raw` | Send a catalog-checked relative CGI path. |

## Discovery and camera profiles

`discover` broadcasts by default and does not inherit the target from
`config.local.yaml`. Save one unambiguous result as a new private profile:

```bash
vstarcamctl discover --save-config entrance.local.yaml
```

When several cameras answer, first inspect the discovered endpoints and then
narrow the second discovery to the intended host:

```bash
vstarcamctl discover
vstarcamctl --host '<discovered-host>' discover --save-config entrance.local.yaml
```

If the PPPP DID (`device_id`) is already known, global
`--device-id '<transport-device-id>'` can select it instead. The virtual VUID
printed on the camera label or QR is a different identifier and is not accepted
as a LAN discovery selector.

Existing files are never replaced. Use a different `*.local.yaml` for each
camera. Discovery never copies or invents camera/account credentials; configure
those separately before running control commands.

When no PSK is supplied, discovery tries only the three built-in VStarcam
profiles for encrypted responses and validates each decrypted result against
the returned PPPP DID prefix. Plaintext discovery needs no seed. An explicit
global `--psk` overrides profile selection and is required for an unknown
encrypted prefix. A discovery-created profile retains a custom explicit seed;
built-in seeds are resolved from the saved `device_id`. The PSK is a
packet-obfuscation seed, not a camera password. See the
[known transport-seed values](getting-started.md#known-transport-seeds) and
their validation status.

On a multi-homed host, global `--source-address <local-ipv4>` binds the
PSK-obfuscated discovery socket and the resulting PPPP session to one
interface. A profile created by that discovery stores the selected source
address.

## Read-only status

These commands normalize common fields and avoid printing complete camera
responses:

```bash
vstarcamctl --config config.local.yaml status
vstarcamctl --config config.local.yaml device info
vstarcamctl --config config.local.yaml imaging status
vstarcamctl --config config.local.yaml siren status
vstarcamctl --config config.local.yaml light status
vstarcamctl --config config.local.yaml time status
vstarcamctl --config config.local.yaml rtsp status
vstarcamctl --config config.local.yaml onvif status
vstarcamctl --config config.local.yaml audio status
vstarcamctl --config config.local.yaml night status
vstarcamctl --config config.local.yaml ir status
vstarcamctl --config config.local.yaml alarm-led status
vstarcamctl --config config.local.yaml motion status
vstarcamctl --config config.local.yaml human status
vstarcamctl --config config.local.yaml wifi status
vstarcamctl --config config.local.yaml wifi scan
vstarcamctl --config config.local.yaml osd clock status
vstarcamctl --config config.local.yaml osd timestamp status
vstarcamctl --config config.local.yaml osd logo status
```

The CLI `params` command safely prints only the number of returned fields. The
Python API's `get_params()` method returns the complete response, which may
contain credentials, identifiers, network names, and addresses; do not publish
that mapping.

`device info` selects only the reported system, application, and kernel version
strings from the status response. It does not return camera identity, model, or
network fields.

## Siren and white light

Their shared state getter and existing on/off writes are confirmed:

```bash
vstarcamctl --config config.local.yaml siren status
vstarcamctl --config config.local.yaml light status
```

Confirmed on/off writes do not require experimental flags. Bound each attempt
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

## Video-overlay clock

Inspect or select the 12/24-hour overlay format:

```bash
vstarcamctl --config config.local.yaml osd clock status
vstarcamctl --config config.local.yaml osd clock 12h --dry-run
vstarcamctl --config config.local.yaml osd clock 12h
vstarcamctl --config config.local.yaml osd clock 24h
```

Both operations read the exact `12h_mode_support` field first. Explicit zero
blocks them; missing, malformed, or negative data remains unknown and does not
block the request. Both operations are confirmed. A set is sent once without
automatic request retry; run the status command after an uncertain outcome.

Read timestamp visibility independently of its 12/24-hour format:

```bash
vstarcamctl --config config.local.yaml osd timestamp status
```

The getter uses the confirmed login-status response. It does not treat
`support_osd_adjustment` as a capability flag because that relationship is not
established. The validated camera returned an exact enabled state.

## Camera logo and alarm indicator

Read camera-logo visibility through the confirmed camera-parameter endpoint.
An explicit `support_custom_logo_show=0` status flag blocks both operations;
missing or malformed capability data does not. The setter still requires the
exact current `logoOsdEnable` field so an inverse state exists before it sends:

```bash
vstarcamctl --config config.local.yaml osd logo status
vstarcamctl --config config.local.yaml osd logo on --dry-run
vstarcamctl --config config.local.yaml osd logo off --experimental
```

The same confirmed state endpoint supplies an independent red/blue alarm
indicator field; its setter is experimental:

```bash
vstarcamctl --config config.local.yaml alarm-led status
vstarcamctl --config config.local.yaml alarm-led on --dry-run
vstarcamctl --config config.local.yaml alarm-led off --experimental
```

Both setters are one-shot safe writes and do not require `--confirm`. Verify
with the corresponding status command before another write after a missing or
invalid acknowledgement.

## Camera media

Install the required FFmpeg executables on `PATH`: `ffprobe` for probing,
`ffmpeg` for recording and snapshots, and `ffplay` for listening.

After a reset, successful protected CGI reads do not establish RTSP readiness.
Follow the [external camera account and RTSP workflow](experimental-features.md#external-camera-account-webpwd)
for first `WebPwd` enable when needed and authorized observed credentials are
available, then use fresh `rtsp status` and
`stream probe` commands. Media commands do not perform that password workflow.

```bash
vstarcamctl --config config.local.yaml stream probe
vstarcamctl --config config.local.yaml stream snapshot frame.jpg
vstarcamctl --config config.local.yaml stream snapshot --source pppp frame.jpg --experimental
vstarcamctl --config config.local.yaml stream record clip.mkv --duration 30
vstarcamctl --config config.local.yaml stream record audio.mka --duration 30 --media audio
vstarcamctl --config config.local.yaml audio listen --duration 30 --local-volume 70
```

The default is the main stream over TCP. Use `--quality sub` for lower bandwidth
or `--rtsp-transport udp` when required. Recording remuxes available streams and
does not overwrite an existing file unless `--overwrite` is supplied.

`--probe-timeout` and `--snapshot-timeout` override the global `--timeout` for
those media operations. Durations and timeouts must be finite positive numbers.
Recording and bounded listening allow extra startup time beyond `--duration`;
listening without `--duration` stays interactive until the player exits or you
interrupt it. Interrupted media commands stop their local child process, but
a failed recording can leave a partial output file.

For stream commands, omitting the RTSP port makes the CLI read the reported
camera configuration and select a validated credential source. The configuration
flag does not prove that a listener is reachable; `stream probe` performs that
separate check. An unknown authentication flag requires explicit credentials.
An explicit stream port also requires an explicit username and password, preferably through
`VSTARCAM_RTSP_USERNAME` and `VSTARCAM_RTSP_PASSWORD`.

A disabled camera may report port `0` as an unset sentinel. `rtsp set on` then
requires an explicit port because the library does not guess a model-wide
default. `stream snapshot` never enables RTSP automatically.

`stream snapshot --source pppp` uses the bounded camera channel-1 livestream
path instead of RTSP. It receives one
self-contained H.264 key access unit, always attempts the matching same-session
stop, and then decodes the image through FFmpeg. This path is experimental and
does not accept RTSP port, credential, quality, or transport options. There is
no automatic fallback between RTSP and PPPP.

## Raw commands

`raw` accepts only a relative CGI path. Known paths inherit their catalog safety
policy. Every unknown path requires both `--experimental` and `--confirm` and
is sent once without request retries because its write/idempotency semantics are
unknown.

```bash
vstarcamctl --config config.local.yaml raw --dry-run "/get_status.cgi"
vstarcamctl --config config.local.yaml raw "/get_status.cgi"
```

Authentication fields in a supplied path are replaced by central configuration.
Dry runs do not connect and mask sensitive values. Prefer high-level commands
because they validate inputs, preserve adjacent settings, and apply one-shot
behavior where needed. Starts requiring paired cleanup, Wi-Fi changes, and
the complete camera-account first-enable/restart flow are blocked from `raw`.
So are all
known writes with parameter placeholders: only their high-level builders
validate value domains. Fixed known writes and cataloged reads remain available.

For guarded writes, continue with [Experimental features](experimental-features.md).

## Exit status

Successful commands exit with `0`. Invalid arguments and expected command errors
exit with `2`; Ctrl+C interruption exits with `130`. Results go to standard output and
errors to standard error. An uncertain write also exits with `2`: it may have
changed the camera, so verify its state before attempting another write.
