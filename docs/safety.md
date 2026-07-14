# Safety

[Documentation index](index.md) · [Back to README](../README.md)

VStarcamCtl controls physical devices and handles camera, network, and media
credentials. Review these rules before using anything beyond read-only status.

## Authorization and network scope

- Use the project only with cameras you own or are explicitly authorized to
  control.
- Keep camera control and RTSP access on a trusted local network.
- Do not expose camera service ports directly to the internet.
- Do not probe unrelated devices, enumerate credentials, or guess command IDs.
- A vendor cloud connection is not required for local operation.

## Credentials and sensitive output

Store passwords and tokens in an ignored local configuration or environment
variable. Avoid password command-line options because shell history and process
listings may retain them.

Never commit or publish:

- camera passwords, account IDs, hashes, or tokens;
- VUIDs, serial numbers, QR payloads, or MAC addresses;
- SSIDs, Wi-Fi passwords, local/public IP addresses, or complete network logs;
- `.env`, `*.local.yaml`, or other local secret files;
- full `params` output.

The CLI masks known sensitive fields, but masking is a last line of defense, not
permission to publish raw output. Review any diagnostic text manually before
sharing it.

## Writes and uncertain outcomes

Read current state before every write and record the intended inverse. Prefer a
dry run when available. Use a bounded timeout and avoid automatic retries for
actuators and one-shot configuration changes.

If a write times out, its outcome is unknown. Do not assume failure and do not
blindly resend it. Instead:

1. run a confirmed getter when one exists;
2. inspect the camera's physical state;
3. restore the previous state only when the current state is known;
4. use the prepared recovery plan when connectivity was lost.

The flags `--experimental`, `--confirm`, and `--recovery-ready` document intent
and unlock guarded code paths. They do not guarantee that a command is supported
or reversible on a particular camera.

## Actuators

The siren, white light, infrared light, night modes, and talk audio can affect
people nearby. Keep attempts short, avoid unexpected activation, and remain in
physical control of the device. Restore a known quiet state when testing ends.

For talk audio, start with a short duration and conservative speaker volume.
Stop immediately if the session becomes unstable. Do not combine listening and
talk unless the camera explicitly supports the requested mode.

## Wi-Fi and password changes

Wi-Fi and camera-account password writes can make the device unreachable. Before
using either command:

- verify the physical reset procedure;
- keep local access to the camera and router;
- record the current non-secret settings;
- prepare every client that will need updated credentials;
- send one attempt only.

`--recovery-ready` should be passed only after these checks are complete. After
an ambiguous timeout, use recovery instead of trying multiple credential or
network variants.

## RTSP and child processes

The CLI avoids printing credentialed RTSP URLs and suppresses FFmpeg diagnostics
that commonly echo them. Credentials can still appear in a child process
argument list while media tools run. Use stream commands on a trusted machine
and close other users' access to its process list.

## Issues and logs

Before attaching output to an issue, reduce it to the smallest useful example
and remove all identifiers, credentials, network details, and unrelated camera
fields. Describe a timeout as an uncertain outcome rather than retrying solely
to produce a cleaner log.

Real-camera integration tests are opt-in. They require an authorized target,
local network access, a known safe final state, and a recovery procedure for any
configuration write.
