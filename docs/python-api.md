# Python API

[Documentation index](index.md) · [Safety](safety.md) · [Back to README](../README.md)

The async API uses the same validation and command policy as the CLI.
`VStarcamCamera` is the main entry point. Python return values are not
redacted for display: select or mask sensitive fields before logging them.

## Discovery and lifecycle

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

`discover_camera()` returns exactly one unambiguous LAN camera or raises
`DiscoveryError`. If several cameras answer, pass a discovered host or exact
PPPP DID with `discover_camera(host="...")` or
`discover_camera(device_id="...")`; the function never silently chooses the
first device. Applications that need to present their own selection UI can use
`discover_cameras()` to receive the complete sorted list. The virtual VUID
printed on the camera label or QR is a different identifier and cannot select a
LAN discovery result.

`VStarcamCamera.from_discovery()` copies the selected camera's current host,
transport device ID, UDP session port, and selected PSK into a validated camera
configuration. The password is always an explicit required argument and has no
default or fallback.

When `psk` is omitted, discovery tries only the built-in `vstarcam2018`,
`vstarcam2019`, and `vstarcam2021` profiles for encrypted responses. It accepts
a decrypted result only when the returned PPPP DID prefix matches the profile.
A plaintext discovery response needs no seed and selects a known profile from
its DID when possible. `vstarcam2018` is live-confirmed on the tested VE-family;
the other two profiles are APK-derived and are not live-confirmed by this
project. In this API a PSK is a seed used for PPPP packet obfuscation, not a
camera, account, or Wi-Fi password.

An explicit PSK overrides automatic profile selection and is required for an
unknown encrypted device-ID prefix:

```python
discovered = await discover_camera(psk="known-transport-seed")
```

The discovery result retains that seed, and `from_discovery()` uses it
automatically. Its explicit `psk=` argument remains available as an override.
The CLI saves custom explicit seeds in discovery-created profiles and resolves
built-in seeds again from the saved `device_id`. The
[known transport-seed values](getting-started.md#known-transport-seeds) have
different validation status.

Prefer the async context manager so transport cleanup always runs. Pass any
non-default `source_address` or `discovery_port` consistently to both
`discover_camera()` and `from_discovery()`.

### Factory-reset credential

One validated freshly reset VE-family target accepted the explicit local
credential `password="888888"`. This is model- and state-specific, is not tried
automatically, and must not be used after the camera password has changed. Some
factory onboarding states additionally require an explicit `account_id`. The
tested reset state accepted `account_id="0"` with the known local password for
protected reads; use an explicit `VStarcamConfig`. This does not authorize or
validate the later owner-account enable sequence.

## Persistent configuration

Applications that want a persistent profile can use the same optional YAML and
environment configuration as the CLI:

```python
from vstarcamctl import VStarcamCamera, load_config

config = load_config(yaml_path="camera.local.yaml")
```

`load_config()` applies process environment, local YAML, and built-in defaults,
in that precedence order. Without an explicit path it uses `VSTARCAM_CONFIG`,
or `config.local.yaml` in the current directory if present. It is not required
when using discovery directly. The shorter snippets below assume an async
function and a loaded `config`.

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

The broad parsed reads are `get_status()` and `get_params()`. `get_status()` is
the minimal device-ID-scoped presence response; it is not the private
capability-rich login preflight. `get_params()` may contain secrets.

Normalized reads include:

- `get_device_software_info()`;
- `get_image_adjustments()`;
- `get_time_settings()`;
- `get_rtsp_settings()` and `get_onvif_settings()`;
- `get_audio_settings()`;
- `get_night_vision_settings()` and `get_infrared_light_settings()`;
- `get_logo_osd()`;
- `get_timestamp_osd()`;
- `get_siren_state()` and `get_light_state()`;
- `get_alarm_led()`;
- `get_motion_detection_settings()`, `get_motion_detection_regions()`, and
  `get_human_detection_settings()`;
- `get_osd_12h_mode()`;
- `get_wifi_status()` and `scan_wifi()`.

`get_params()` returns a broad device response and may contain secrets. Mask or
select fields before logging, displaying, or returning it from an application.
Use `get_device_software_info()` when only allowlisted system, application, and
kernel version strings are needed.

RTSP and ONVIF getters report configuration, not listener availability:
`configured_enabled` is the CGI flag and `reported_port` is the reported value.
RTSP authentication is tri-state; `authentication_enabled=None` requires
explicit stream credentials. Probe the resulting RTSP descriptor separately
before treating it as a live stream.

`get_audio_settings()` reads audio capability flags from the exact full
login-status response. The distinct minimal device-presence status does not
carry those flags. Experimental half-duplex talk is refused when the camera
explicitly reports G.711 talk or echo-cancellation/full-duplex support.
Malformed or negative selector values remain unknown and are also refused.

`get_motion_detection_regions()` returns an 18-by-22
`detection_enabled` boolean grid. Rows follow `md_reign0` through
`md_reign17`; columns are left-to-right, and `True` means motion detection is
enabled for that cell. An explicit false `support_motionArea` report blocks the
read, while a missing or malformed capability remains unknown and does not.

The `logical_control_enabled` field returned by
`await camera.get_infrared_light_settings()` is the camera's reported
command-2120 control flag. It is deliberately not named `enabled`:
automatic night logic can make the physical IR emitters differ from that value.
`set_infrared_light()` returns only the four validated acknowledgement fields;
a missing or mismatched echo is reported as an uncertain one-shot outcome.

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

Concurrent time-setting updates through one `VStarcamCamera` instance are
serialized. Separate instances, processes, and camera apps are not coordinated;
use one writer for configuration changes.

Actuator calls still need application-level bounds and state verification. Do
not issue an automatic inverse after an ambiguous timeout.

## Guarded writes

Experimental and risky methods require explicit keyword arguments:

```python
import asyncio

from vstarcamctl.errors import ServiceChangeUncertainError


async with VStarcamCamera(config) as camera:
    try:
        await camera.set_motion_detection(
            True,
            sensitivity=4,
            experimental=True,
            confirm=True,
        )
    except asyncio.CancelledError:
        raise
    except ServiceChangeUncertainError:
        observed = await camera.get_motion_detection_settings()
```

Typed RTSP, ONVIF, record-audio, aggregate-motion, unconfirmed humanoid, and
Wi-Fi setters deliberately have no post-send success return. Their exact
endpoint response code or ACK contract is not proven, so after their single
guarded send they raise an uncertain-outcome error even when response text
arrives. A confirmed getter may be used for observation, but do not infer an
inverse write or retry from that exception.

Other guarded methods include service settings, recording audio, speaker
volume, talk, night vision, infrared light, the alarm indicator, OSD writes,
human detection, Wi-Fi, and camera-account password changes. Microphone volume
is confirmed but remains one-shot; speaker volume still requires
`experimental=True`. Recovery-sensitive methods additionally require
`recovery_ready=True`.

`set_camera_account_password()` changes an existing `WebPwd` with one write.
When `WebPwd` is absent, it uses the camera-reported authentication state to run
the guarded first-enable sequence and restart, then verifies the password on a
fresh session. First enable requires `auth_mode="observed"` with authorized
account values supplied privately. `basic` with an absent or zero account ID
is refused before any first-enable write; see the
[account and RTSP limitations](experimental-features.md#external-camera-account-webpwd).
Both the existing-account and first-enable password steps require exact
`result=0` and `DualAuthentication=2` acknowledgement before fresh-session
effect verification can report success. The returned mapping is normalized and
does not expose arbitrary camera response fields. CLI dry-run lists both
conditional branches because the exact branch is selected only after the
read-only preflight.

Experimental talk requires two independent finite bounds in addition to its
safety flags:

```python
result = await camera.send_talk_audio(
    frames,
    duration=5,
    max_speaker_volume=15,
    experimental=True,
    confirm=True,
)
```

The method verifies the reported speaker level without changing it and caps
the number and delivery window of frames. Livestream stop takes priority over
waiting for a cancellation-resistant source, and time spent waiting for source
cleanup is bounded. It reports delivery failures as an uncertain possibly
played prefix.

Manual PTZ accepts only four directions and a finite positive caller-selected
duration measured from the start request attempt. It always attempts the
matching stop before returning:

```python
async with VStarcamCamera(config) as camera:
    result = await camera.move_ptz(
        "left",
        duration=0.5,
        experimental=True,
        confirm=True,
    )
```

`stop_ptz("left", experimental=True)` sends the direction-specific stop once
without a new capability preflight. Use it when an earlier movement may still
be active. Decoder-control start and stop responses must contain exact
`result=0`; missing or nonzero acknowledgements are unknown outcomes. A failed
start still triggers the matching bounded cleanup stop. An explicit
`haveMotor=0` blocks a start only for that camera model.

The overlay clock API uses a boolean domain: `True` selects 12-hour format and
`False` selects 24-hour format. Both operations are confirmed and read the
exact `12h_mode_support` field first. Explicit zero
blocks the operation; missing, malformed, or negative data remains unknown and
does not block it:

```python
async with VStarcamCamera(config) as camera:
    current_is_12h = await camera.get_osd_12h_mode()
    echoed_is_12h = await camera.set_osd_12h_mode(True)
```

The setter is sent once and requires an acknowledgement echo matching the
request. After a timeout or invalid acknowledgement, call the getter before
another write.

Camera-logo visibility is a separate boolean contract. Its getter is not
experimental; both operations reject only an explicit false capability report:

```python
async with VStarcamCamera(config) as camera:
    logo_visible = await camera.get_logo_osd()
    await camera.set_logo_osd(not logo_visible, experimental=True)
```

One confirmed camera-state request exposes three independent boolean fields
through narrow getters. Siren and white-light writes remain confirmed; the
red/blue alarm-indicator write remains experimental:

```python
async with VStarcamCamera(config) as camera:
    siren_enabled = await camera.get_siren_state()
    light_enabled = await camera.get_light_state()
    indicator_enabled = await camera.get_alarm_led()
    await camera.set_alarm_led(not indicator_enabled, experimental=True)
```

Both setters are one-shot safe writes. After a timeout or invalid
acknowledgement, verify through the matching getter before another write.

The API validates these arguments against the command catalog. Do not bypass a
high-level method with `send_raw_cgi()` merely to avoid a safety gate.

## Raw requests

For supported relative paths:

```python
async with VStarcamCamera(config) as camera:
    response = await camera.send_raw_cgi("/get_status.cgi")
```

Unknown paths require both `experimental=True` and `confirm=True` and are sent
without request retries. Catalog commands that require bounded cleanup or a
recovery-validated workflow are unavailable through this raw method. Known
parameterized writes are also unavailable because only their high-level
builders validate value domains. Treat raw responses as sensitive until
selected fields have been masked.

## Initial Wi-Fi provisioning

The synchronous helpers build and privately write the supported static QR used
by a reset camera before a PPPP session exists. Install the optional renderer
with `python -m pip install "vstarcamctl[provisioning]"` first:

```python
from vstarcamctl import (
    build_static_wifi_qr_payload,
    wait_for_camera_on_lan,
    write_static_wifi_qr_svg,
)

payload = build_static_wifi_qr_payload(
    "2.4 GHz network",
    wifi_password,
)
write_static_wifi_qr_svg(payload, "/private/path/camera-wifi.svg")

lan_result = wait_for_camera_on_lan(
    broadcast_host,
    expected_device_id=expected_device_id,
    ports=discovery_ports,
    total_timeout=total_timeout,
    probe_timeout=probe_timeout,
    probe_interval=probe_interval,
    require_encrypted=require_encrypted,
)
```

Omitting `account_id` or passing `None` uses the string `"0"` in the QR's `U`
field. Pass `account_id="..."` to preserve an explicit account ID. This
default applies only to QR generation, not to local control authentication.

Both the payload and SVG contain the Wi-Fi password. Do not log, display in
diagnostics, or retain them longer than needed. The caller remains responsible
for explicit confirmation, physical reset/recovery readiness, and showing the
QR. The synchronous wait blocks until a match or timeout and returns `None` on
timeout; use `await asyncio.to_thread(wait_for_camera_on_lan, ...)` in an
async application. It requires an expected PPPP transport device ID and
explicit timing policy; it automatically selects a built-in PSK from that ID.
Pass `psk=` only
for an independently known unknown-prefix profile. The virtual VUID printed on
a label or QR is not accepted. A result means only `lan_identity_seen`, not QR
acknowledgement, authentication, vendor binding, or control readiness. Verify
control separately with an authenticated read.

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

Media helpers need the corresponding executable on `PATH`: `ffmpeg` for
recording and snapshots, `ffprobe` for `probe_rtsp()`, and `ffplay` for
`play_rtsp()`. Recording and snapshots refuse existing files unless
`overwrite=True` is supplied. Their results include `path` and `bytes_written`;
recording also reports the requested duration, media selection, and quality.
Timeouts and cancellation terminate and reap the child process. A failed
recording can leave a partial file; inspect or remove it before retrying.

Stream representations and helper results omit credentials, but `stream.url`
and the child process argument list contain them. Use media helpers only on a
trusted local machine.

For a camera whose RTSP service is unavailable, capture one frame through the
bounded experimental PPPP media channel:

```python
async def capture_frame():
    config = load_config()
    async with VStarcamCamera(config) as camera:
        return await camera.capture_pppp_snapshot(
            "frame.jpg",
            timeout=8,
            experimental=True,
        )
```

The method keeps only one bounded access unit in memory, requires a
self-contained H.264 SPS/PPS/IDR frame, stops the livestream before invoking
FFmpeg, and never falls back to guessed codecs or endpoints.

## Error handling

Catch the package exceptions appropriate to your application and distinguish an
explicit rejection from an uncertain network outcome. For writes, uncertainty
must be resolved by reading state or checking the device physically, not by an
unconditional retry.

Cancellation-specific write errors also inherit their uncertain-outcome error
classes. Catch and re-raise `asyncio.CancelledError` before a broader package
exception handler so cancellation does not become a retry or a fresh request.

Runnable scripts are in [`examples/connect_test.py`](../examples/connect_test.py),
[`examples/raw_command.py`](../examples/raw_command.py), and
[`examples/record_stream.py`](../examples/record_stream.py). The latter two use
`load_config()`, including `VSTARCAM_CONFIG` when selecting a saved profile.
