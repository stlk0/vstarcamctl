# Initial Wi-Fi provisioning

`wifi set` only works after the camera has already joined a network and the
local PPPP session can be established. A reset camera instead receives Wi-Fi
settings through a QR code shown to its lens.

The experimental `provision qr` command creates the static setup QR locally.
It does not contact the camera or a cloud service. The SVG contains the Wi-Fi
password, so choose an explicit private output path and do not share it.

From the source checkout and activated virtual environment described in
[Getting started](getting-started.md), install the optional QR renderer:

```bash
python -m pip install ".[provisioning]"
```

```bash
export VSTARCAM_WIFI_PASSWORD='...'
vstarcamctl provision qr \
  --ssid 'your 2.4 GHz network' \
  --output /tmp/camera-wifi.svg \
  --experimental --confirm --recovery-ready
```

`account_id` is optional for QR generation. When no value is configured, the
QR uses the string `"0"` in its `U` field. An explicit account ID is preserved;
the CLI reads it from `--account-id`, `VSTARCAM_ACCOUNT_ID`, or local
configuration. Use `vstarcamctl --account-id 0 provision qr ...` to select
`"0"` even when an account ID is saved. This QR default does not change local
control authentication or create a vendor account.
`--bssid` is optional; omitting it uses the protocol's uppercase `NULL`
fallback.

Use a visible 2.4 GHz personal Wi-Fi network. Open the SVG at full screen and
show it to the reset camera from about 5–10 inches away. Camera sounds and LEDs
are signals for the operator, not a programmatic acknowledgement. The library
can separately wait for one caller-supplied PPPP transport device ID to appear
on the LAN:

```python
from vstarcamctl import wait_for_camera_on_lan

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

All timing values are explicit and independent of QR UI timers. The helper
derives a known PSK profile from `expected_device_id`; pass `psk=` explicitly
only for an independently known encrypted profile that is not in the built-in
table. A non-`None` result means only
`lan_identity_seen`: the exact transport device ID answered discovery. The
virtual VUID printed on the label or QR is a different identifier and is not
accepted by this helper. A result does not prove that the camera acknowledged
the QR, completed vendor binding, authenticated, or is ready for control.
Verify control separately with an authenticated read.

Treat presenting the QR as a one-shot configuration attempt; reset/recovery
should be available before showing the code.

After the camera joins the LAN, create a separate control profile without
reusing another camera's authentication values:

```bash
vstarcamctl discover --save-config new-camera.local.yaml
```

If multiple cameras answer, first run `vstarcamctl discover`, identify the new
camera's LAN host, and repeat the command with global `--host` before
`discover`. If its exact transport device ID is already known, global
`--device-id` can be used instead. Do not use the virtual VUID from the label or
QR. Saving a profile records only LAN identity/endpoint data; authorized
control credentials remain a separate configuration step.

For a freshly reset camera, keep the discovered profile and supply the current,
authorized local credentials privately. Do not assume a model-wide factory
password or copy account values from another device:

```bash
export VSTARCAM_PASSWORD='<authorized-local-password>'
vstarcamctl --config new-camera.local.yaml --auth-mode basic --account-id 0 status
vstarcamctl --config new-camera.local.yaml --auth-mode basic --account-id 0 params
```

These commands use the local password and the zero account value, without a
vendor account ID, login hash, or token. Omitting `account_id` in `basic` mode
selects the same flow. Protected parameter reads passed on the tested camera
after QR onboarding and after first `WebPwd` enable and restart. If another
camera's current state needs additional credentials, follow the
[authentication configuration](getting-started.md#authentication-modes).

Run these as separate commands so each check gets a fresh PPPP session. A
successful `status` proves transport readiness; a successful protected
`params` confirms access to protected CGI reads. It does not establish RTSP
availability. For media setup after a reset, follow the
[external camera account and RTSP workflow](experimental-features.md#external-camera-account-webpwd).
Its guarded first-enable path supports `basic` mode with an absent or zero
account ID. Keep using the original local CGI password for control and the
new `WebPwd` for RTSP. The library handles the additional local authentication
required after the transition.

The QR exchange and Wi-Fi join are local camera functions. Any separate
internet-backed vendor account binding is outside provisioning and local
control in this library.

This mechanism is experimental until it has been validated on the specific
camera model.
