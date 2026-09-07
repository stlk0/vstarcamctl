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

Confirmed siren and white-light writes are also one-shot. If their `result=0`
acknowledgement is missing or invalid, use the corresponding status getter and
do not repeat the write automatically.

Confirmed time/NTP writes likewise require exact `result=ok`. Treat any other
acknowledgement as unknown and verify with `time status`.

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

`ir status` reports the command-2120 logical control flag, not guaranteed
physical illumination. Automatic black-white night control may change the
physical emitters independently. Treat an inverse as logical until both the
image and physical emitters have been checked; do not send another write merely
because the image has not settled.

## RTSP, ONVIF, and recording audio

For RTSP setup after a reset, see the
[external camera account workflow](#external-camera-account-webpwd) and its
fresh RTSP checks. `rtsp set` changes RTSP settings; it does not perform the
first `WebPwd` enable sequence.

Service writes are one-shot and require both `--experimental` and `--confirm`:

```bash
export VSTARCAM_RTSP_PASSWORD='replace-with-dedicated-password'
vstarcamctl --config config.local.yaml rtsp set on --port 10554 --rtsp-username camera --experimental --confirm
vstarcamctl --config config.local.yaml onvif set on --experimental --confirm
vstarcamctl --config config.local.yaml audio recording on --experimental --confirm
```

Omitted RTSP values are preserved when the camera reports them. A disabled
camera may report port `0` as an unset sentinel; enabling then requires an
explicit port rather than a guessed model-wide default. Follow each attempt
with the corresponding `status` command. Exact response command codes and ACK
contracts are not yet proven for these three setters, so even a parseable
response is reported as an unknown one-shot outcome rather than success. This
does not prevent the experimental send; it prevents an unrelated or ambiguous
response from being promoted to confirmation. Do not assume that recording
audio controls live microphone availability.

## PPPP snapshot

The library exposes a bounded PPPP one-frame capture for cameras where RTSP is
not available:

```bash
vstarcamctl --config config.local.yaml stream snapshot --source pppp frame.jpg --experimental
```

The command starts exact substream `1`, accepts one self-contained H.264
keyframe, always attempts the exact same-session stop, and decodes only after
cleanup. It remains experimental because retained-session traffic cessation
after stop is not part of the validated contract. Do not combine it with RTSP
input options or assume H.265 support.

## Camera volume and talk

Camera microphone and speaker levels use a range from 0 to 31:

```bash
vstarcamctl --config config.local.yaml audio volume microphone 12
vstarcamctl --config config.local.yaml audio volume speaker 12 --experimental
vstarcamctl --config config.local.yaml audio status
```

Microphone gain is confirmed. Speaker gain still requires `--experimental`
because its hardware effect is not independently validated.

The setter accepts only exact `result=0`. If it is
missing or nonzero, treat the level as unknown and use `audio status` before
any inverse; never repeat the write automatically.

Talk is an audible actuator and requires both safety flags. Start with a short,
bounded source and a conservative speaker level:

```bash
vstarcamctl --config config.local.yaml audio talk default --input-format pulse --duration 5 --max-speaker-volume 15 --experimental --confirm
```

Use `alsa` instead of `pulse` when appropriate, or pass an audio file as the
source. Both the finite duration and a caller-selected maximum reported speaker
level are mandatory. The library refuses talk when `outvolume` is absent,
malformed, or above that limit; it does not guess or change the level. It caps
even an infinite source, prioritizes livestream stop over a
cancellation-resistant source, bounds time spent waiting for source cleanup,
and reports a channel failure as a possibly played acknowledged prefix. A
session failure can still leave the result uncertain. The CLI exposes only the
guarded, modeled half-duplex workflow; its complete hardware effect remains
experimental and there is no separate full-duplex command. Before it consumes
the source or starts a livestream, it reads the full login-status capability slice and
refuses ADPCM talk when G.711 talk or echo-cancellation/full-duplex support is
explicitly enabled. Missing or exact-zero selectors choose the modeled ADPCM
branch; malformed or negative selectors fail closed as unknown.

## Motion and human detection

Motion configuration requires both safety flags:

```bash
vstarcamctl --config config.local.yaml motion set on --sensitivity 4 --experimental --confirm
vstarcamctl --config config.local.yaml motion status
```

The typed aggregate write is available only when a camera directly reports the
complete exact wire-named adjacent alarm profile, including its inverse state.
No supported mapping exists from `alarm_*` or `motion_plan*` getters to those
adjacent fields. A camera that does not report the required wire-named profile
is refused before a write instead of copying defaults or guessing a mapping.

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

Both confirmed writes require `result=0` and the exact requested field echo.
A missing or mismatched echo is an unknown one-shot outcome; inspect
`human status` before any follow-up write.

The confirmed command-2126 getter reports standalone sensitivity, frame, and
optional zoom fields. It does not provide the command-2106 distance/sensitivity
inverse, so the main detector write remains experimental and every response is
reported as an unknown outcome. Tracking is rejected only when `haveMotor=0`
is explicit; unknown capability remains experimental for another model.
Zoom-tracking additionally requires a reported `humanoid_zoom` pre-state.
Unproven motion/tracking ACK payloads are never presented as success.

Tracking requires compatible camera hardware and should not be enabled merely
because a setting command is accepted.

## Manual pan and tilt

Manual PTZ is available only when the camera has pan/tilt motors. Movement is
experimental, requires both safety flags, and always requires an explicit
caller-selected duration:

```bash
vstarcamctl --config config.local.yaml ptz move left --duration 0.5 --dry-run
vstarcamctl --config config.local.yaml ptz move left --duration 0.5 --experimental --confirm
```

The client starts the duration deadline immediately before the one-shot start
request, so acknowledgement latency consumes the same bound. It attempts the
matching direction-specific stop in cleanup even after a timeout or
cancellation and never exposes an unbounded start. If movement may still be
active, an explicit stop needs `--experimental` but not `--confirm`:

```bash
vstarcamctl --config config.local.yaml ptz stop left --experimental
```

Start and stop acknowledgements must contain exact `result=0`; missing or
nonzero results are uncertain. Keep physical control of the camera. Speed,
presets, patrol, cruise, and multi-motor controls are not exposed.

## Camera logo and alarm indicator

The camera-logo getter uses the confirmed camera-parameter endpoint. Its
one-shot setter remains experimental, is blocked when camera status explicitly
reports `support_custom_logo_show=0`, and also requires a valid current
`logoOsdEnable` pre-state before the write:

```bash
vstarcamctl --config config.local.yaml osd logo status
vstarcamctl --config config.local.yaml osd logo on --dry-run
vstarcamctl --config config.local.yaml osd logo off --experimental
```

One confirmed state endpoint exposes independent siren, white-light, and
red/blue alarm-indicator fields. The siren and white-light setters remain
confirmed; the alarm-indicator setter is experimental:

```bash
vstarcamctl --config config.local.yaml siren status
vstarcamctl --config config.local.yaml light status
vstarcamctl --config config.local.yaml alarm-led status
vstarcamctl --config config.local.yaml alarm-led on --dry-run
vstarcamctl --config config.local.yaml alarm-led off --experimental
```

Neither experimental setter requires `--confirm`, but each is sent only once.
A missing or invalid acknowledgement leaves the outcome unknown; use its
getter before another write.

The alarm-indicator setter requires an exact logical state echo and a
getter-verifiable inverse. It remains experimental until its visible effect is
confirmed on applicable hardware.

## Wi-Fi

Wi-Fi status and scanning are confirmed read-only operations. Both mask network
identifiers in output:

```bash
vstarcamctl --config config.local.yaml wifi status
vstarcamctl --config config.local.yaml wifi scan
```

Changing Wi-Fi can disconnect the camera. Verify physical reset or wired/local
recovery first. The supported request also requires `account_id` in ignored
local configuration. Then provide the password through the environment:

```bash
export VSTARCAM_WIFI_PASSWORD='replace-with-network-password'
vstarcamctl --config config.local.yaml wifi set --ssid "Home network" --experimental --confirm --recovery-ready
```

The command is sent once and closes its session. Its exact ACK payload is not
part of the validated contract, so even a parseable response is reported as an
unknown outcome.
Channel and authentication metadata must exactly match a fresh camera scan;
caller-supplied values only narrow that match and cannot bypass it. The typed
slice carries the password-bearing request form and deliberately does not
guess the security-family meaning of numeric `auth_type` values. Rediscover the
camera or use the recovery plan instead of resending.

## External camera account (`WebPwd`)

Changing an existing `WebPwd` uses one `ExUser`/`ExPwd`/`ExUserSwitch=1`
request. First enable is different: the library checks the camera's
`DualAuthentication` state and, when required, performs the guarded owner,
external-enable, password, and restart sequence. That path requires authorized
`observed` account credentials in the private configuration. The current
first-enable workflow rejects `basic`, including an absent account ID or
`account_id="0"`; successful protected CGI reads with that configuration do not
provide the required owner credentials. These credentials are separate from
the local CGI factory password. Prepare recovery, store the new password in an
environment variable, and run one guarded operation:

```bash
export VSTARCAM_NEW_CAMERA_PASSWORD='replace-with-new-password'
vstarcamctl --config config.local.yaml account password --experimental --confirm --recovery-ready
```

Each first-enable transition requires an exact acknowledgement. Success still
requires the requested `WebPwd` from a fresh post-restart session; an
acknowledgement alone is not enough. After an uncertain outcome, do not resend
any password step. Verify state through the planned recovery procedure.

On the previously validated camera, first `WebPwd` enable also activated RTSP
and RTSP authentication, and the workflow restarted the camera without a
separate `rtsp set`. That successful post-restart CGI verification used the
authorized account ID, login hash, and token. After a successful password
operation, use fresh commands to inspect the resulting configuration and
verify the actual stream:

```bash
vstarcamctl --config config.local.yaml rtsp status
vstarcamctl --config config.local.yaml stream probe
```

Check these results before considering a separate RTSP configuration write.
Verifying `WebPwd` alone does not establish stream availability, and the
observed first-enable behavior is not a guarantee for every camera model.

On the tested reset camera, QR onboarding with account value `"0"` and protected
CGI reads with the known local password worked. A separate private owner-zero
experiment enabled RTSP access with the new `WebPwd`, but protected CGI reads
were rejected after restart: a complete `0x6001` response carried `result=-2`,
while login status reported `DualAuthentication=2` and external access enabled.
The exact firmware cause is unknown. The owner-zero first-enable sequence is
not supported by the public API; a working RTSP login does not establish CGI
access. This result does not establish a general requirement for a vendor
account to use RTSP. Use the planned recovery procedure after an authentication
refusal instead of repeating account writes.
