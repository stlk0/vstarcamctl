# Experimental features

[Documentation index](index.md) · [Safety](safety.md) · [Back to README](../README.md)

Experimental support is model- and firmware-dependent. A successful response
does not by itself prove that a physical or persistent setting changed.

## Required workflow

Before an experimental write:

1. Read the current normalized status.
2. Use `--dry-run` when the command provides it.
3. Decide how the previous state will be restored.
4. For lockout-sensitive settings, verify physical reset or recovery.
5. Send one bounded attempt.
6. Check both the getter and the physical effect.

Never resend a timed-out write automatically. Its outcome is unknown.

## Safety flags

| Flag | Meaning |
| --- | --- |
| `--experimental` | Allow an unconfirmed or model-dependent command. |
| `--confirm` | Acknowledge a risky or physically observable operation. |
| `--recovery-ready` | Confirm that physical recovery has been verified. |
| `--dry-run` | Validate and show a masked request without connecting. |

Flags do not make an operation safe by themselves; they ensure that risky
actions are explicit.

## Night vision and infrared light

Status reads are available without experimental flags:

```bash
vstarcamctl --config config.local.yaml night status
vstarcamctl --config config.local.yaml ir status
```

Mode and infrared writes require `--experimental`:

```bash
vstarcamctl --config config.local.yaml night set black-white --experimental
vstarcamctl --config config.local.yaml night set starlight --experimental
vstarcamctl --config config.local.yaml night set full-color --experimental
vstarcamctl --config config.local.yaml night set smart --experimental
vstarcamctl --config config.local.yaml ir set on --experimental
vstarcamctl --config config.local.yaml ir set off --experimental
```

Some modes may involve the visible white light. Observe the camera after every
change and verify the final `night`, `ir`, and `light` state.

## RTSP, ONVIF, and recording audio

Service writes are one-shot and require both `--experimental` and `--confirm`:

```bash
export VSTARCAM_RTSP_PASSWORD='replace-with-dedicated-password'
vstarcamctl --config config.local.yaml rtsp set on --port 10554 --rtsp-username camera --experimental --confirm
vstarcamctl --config config.local.yaml onvif set on --experimental --confirm
vstarcamctl --config config.local.yaml audio recording on --experimental --confirm
```

Omitted RTSP values are preserved when the camera reports them. Follow each
attempt with the corresponding `status` command. Do not assume that recording
audio controls live microphone availability.

## Camera volume and talk

Camera microphone and speaker levels use a range from 0 to 31:

```bash
vstarcamctl --config config.local.yaml audio volume microphone 12 --dry-run
vstarcamctl --config config.local.yaml audio volume speaker 12 --experimental
vstarcamctl --config config.local.yaml audio status
```

Talk is an audible actuator and requires both safety flags. Start with a short,
bounded source and a conservative speaker level:

```bash
vstarcamctl --config config.local.yaml audio talk default --input-format pulse --duration 5 --experimental --confirm
```

Use `alsa` instead of `pulse` when appropriate, or pass an audio file as the
source. The implementation always attempts cleanup, but a session failure can
leave the result uncertain. Full-duplex mode is refused unless explicitly
supported by the camera.

## Motion and human detection

Motion configuration requires both safety flags:

```bash
vstarcamctl --config config.local.yaml motion set on --sensitivity 4 --experimental --confirm
vstarcamctl --config config.local.yaml motion status
```

Human detection includes separate features. The main detector and hardware-
dependent tracking controls remain guarded:

```bash
vstarcamctl --config config.local.yaml human detection on --sensitivity 2 --distance 2 --experimental --confirm
vstarcamctl --config config.local.yaml human tracking on --experimental --confirm
vstarcamctl --config config.local.yaml human zoom-tracking on --experimental --confirm
```

The frame display and standalone sensitivity control are available without
experimental flags on supported cameras:

```bash
vstarcamctl --config config.local.yaml human sensitivity 1
vstarcamctl --config config.local.yaml human frame on
```

Tracking requires compatible camera hardware and should not be enabled merely
because a setting command is accepted.

## Wi-Fi

Wi-Fi status is read-only. Scanning remains experimental and masks network
identifiers in output:

```bash
vstarcamctl --config config.local.yaml wifi status
vstarcamctl --config config.local.yaml wifi scan --experimental
```

Changing Wi-Fi can disconnect the camera. Verify physical reset or wired/local
recovery first, then provide the password through the environment:

```bash
export VSTARCAM_WIFI_PASSWORD='replace-with-network-password'
vstarcamctl --config config.local.yaml wifi set --ssid "Home network" --experimental --confirm --recovery-ready
```

The command is sent once and closes its session. If no acknowledgement arrives,
use the recovery plan instead of resending. Open and WEP networks are not
supported.

## Camera-account password

A password change can lock out both control and RTSP access. Prepare recovery,
store the new password in an environment variable, and send only one attempt:

```bash
export VSTARCAM_NEW_CAMERA_PASSWORD='replace-with-new-password'
vstarcamctl --config config.local.yaml account password --experimental --confirm --recovery-ready
```

After a confirmed change, update every local client. After a timeout, do not
guess whether the old or new password is active; verify through the planned
recovery procedure.
