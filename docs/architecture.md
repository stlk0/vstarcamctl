# Architecture and maintenance

VStarcamCtl is an async Python library with a thin argparse CLI. Camera requests
use CGI-shaped paths inside a PPPP UDP session; they are not HTTP requests.
RTSP playback and capture use the installed FFmpeg tools.

## Follow a command

1. `cli.py` loads configuration, validates command-line input, and calls the
   same public API used by Python applications.
2. `camera.py` coordinates feature reads, writes, retries, and cleanup.
   Feature modules such as `wifi.py`, `media.py`, and `time_settings.py` build
   paths and normalize responses without doing network I/O.
3. `command_catalog.py` applies the policy in `data/catalog.yaml`. The catalog
   decides whether a command is available, experimental, one-shot, or requires
   confirmation. Feature modules enforce field-specific acknowledgement checks.
4. `cgi.py` validates relative paths and appends authentication from trusted
   configuration. `transport_aiopppp.py` owns the PPPP session, authentication
   preflight, framing, acknowledgements, and response reassembly.
5. `parser.py` reads a bounded assignment grammar. It never evaluates camera
   responses as executable JavaScript.

Discovery is separate from connection. `discovery_udp.py` exchanges bounded
plaintext or known-profile packets; `discovery.py` selects an unambiguous
camera. Use its transport device ID and returned session port, not a label
identifier or an assumed port.

## Keep the boundaries small

The CLI owns presentation, the camera API owns operation sequencing, the
transport owns sessions, and feature modules own wire-field meanings. Shared
helpers belong in the existing private modules only when several real callers
need them. Keep short protocol-specific branches explicit; do not turn them
into a command framework or generate the public methods dynamically.

`CameraTransport` is the narrow contract shared by the real implementation and
the deterministic test fake. There is no transport plug-in registry. Tests
should normally substitute this boundary; dependency-contract tests separately
cover the pinned aiopppp integration.

Runtime dependencies are aiopppp and PyYAML; QR generation optionally uses
qrcode. Use the standard library for argument parsing, subprocesses, URL
encoding, and async coordination. Add a package when it removes a maintained
implementation and fits the protocol, rather than merely wrapping existing
code. The PPPP adapter depends on private aiopppp details, so dependency upgrades
need the contract tests and a separate compatibility review.

## Preserve these behaviours

- A transport acknowledgement is not proof that a setting took effect. A
  malformed or missing write response must remain an uncertain outcome.
- Reads may retry. One-shot writes never resend automatically after a timeout
  or lost response. Unknown commands require explicit opt-in.
- Adjacent fields in aggregate setters are preserved. Concurrent time-setting
  updates are serialized across the full read/write operation on one camera
  instance. Separate instances or processes still need caller coordination.
- Multi-step account, Wi-Fi, night, PTZ, and livestream operations have their
  own sequencing rules. A single global lock would obstruct cleanup and stop
  commands; keep those paths explicit.
- PTZ and livestream operations attempt cleanup on failure or cancellation.
  Code that preserves cancellation or quarantines an ambiguous session should
  not be replaced with a retry loop.
- Media playback with a duration has an external process timeout, including
  startup allowance. Playback without a duration remains interactive.
- Logs and CLI output redact credentials; test values are fictional. Private
  configuration and device captures never belong in a source distribution.

Experimental status describes evidence, not an invitation to relax validation.
When adding support for a model, distinguish a parsed configuration echo from
an observed effect. A getter or recovery procedure should guide the next step
after an uncertain write.

## Check a change

Use the checks in [Contributing](../CONTRIBUTING.md). Prefer a small behavioural
regression for the failure being fixed; avoid tests that merely repeat the
implementation. Unit tests must work without hardware or private files.
Live tests require a separate, explicit request and are not a prerequisite
for contributing a refactor.
