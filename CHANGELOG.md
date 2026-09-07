# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Complete same-session CGI authentication for local owner zero after external
  password enable, using an empty token when the camera requires dual authentication.
- Redact complete quoted credentials, escaped delimiters, and CGI username aliases.
- Pass absolute local media paths to FFmpeg so filenames cannot become options
  or protocol URLs; reject invalid output directories before starting a stream.
- Reject invalid YAML configuration roots and non-text field names consistently.
- Report a partially completed night-mode transition if reconnecting fails
  before its second write.
- Do not postpone PPPP session readiness when the camera repeats its ready packet.
- Mask the `realdeviceid` status field in CLI output and logs.
- Explain a disabled RTSP service with port zero before resolving a media stream.
- Report the observed CGI authentication refusal (`result=-2`) as
  `TransportAuthenticationError` without retries instead of reporting a generic timeout.
- Read full login status when checking first camera-account enable and owner state.
- Preserve concurrent time-setting updates made through one camera instance.
- Stop bounded RTSP playback when the media process exceeds its duration plus
  startup allowance, including when no media arrives.

- Reject invalid media time bounds and existing output files in the CLI before
  connecting to a camera.
- Bound the total expansion of sparse arrays in camera responses.
- Complete media-process cleanup even when cancellation is repeated.

### Changed

- Exclude local development settings, AI assistant files, and research material
  from Git and distributions, with release checks for accidentally tracked files.
- Remove an unreachable audio-cleanup branch left from an earlier cleanup order.
- Validate normalized device and protected imaging reads in the opt-in hardware test.
- Use the string `"0"` in the Wi-Fi setup QR when no account ID is supplied.
- Treat an omitted account ID as local owner zero and allow guarded first
  external-password enable with local credentials, preserving the CGI password.
- Support Python 3.11–3.13, with unit tests for each version in CI.
- Share acknowledgement-error handling, discovery descriptor construction,
  media output checks, and CLI cleanup without changing command policies.
- Use Hatchling's reproducible builds instead of rewriting distribution
  archives after packaging.
- Document the architecture and maintenance boundaries for contributors.

## [0.2.0] - 2026-08-28

### Added

- Normalized device-information, imaging, time, media, Wi-Fi, detection,
  actuator-state, night-vision, and OSD reads.
- Confirmed one-shot controls for siren, white light, time/NTP, microphone
  volume, humanoid frame and sensitivity, and 12/24-hour OSD clock format.
- Guarded experimental controls for model-dependent service, detection,
  lighting, PTZ, account, Wi-Fi, and media operations.
- Bounded RTSP media helpers, PPPP snapshots, audio playback, talk framing, and
  initial Wi-Fi QR generation.
- Public single-camera discovery and `VStarcamCamera.from_discovery()` for
  YAML-free Python onboarding.
- Public safety, contribution, security-reporting, and compatibility guides.

### Changed

- Hardened PPPP session readiness, response correlation, bounded reassembly,
  cancellation cleanup, and credential masking.
- Made ambiguous state-changing responses explicit uncertain outcomes and
  prevented automatic retries where duplicate delivery could be unsafe.
- Reduced broad camera responses to normalized or allowlisted public results
  where practical; the explicit `get_params()` API remains documented as
  sensitive.
- Moved `aiopppp` into the core installation and set the supported interpreter
  range to Python 3.13 while that pinned dependency remains incompatible with
  Python 3.14.
- Made configuration immutable and redacted its private fields from `repr()`.
- Unified profile and QR output through an atomic private-file writer.
- Made discovery-created private profiles the primary CLI onboarding path.
- Named the LAN identity consistently as `device_id` (the PPPP transport DID),
  distinct from the virtual VUID printed on a label or QR.
- Made an omitted PSK trigger bounded discovery of the three known VStarcam
  transport profiles, with decoded DID-prefix verification; an explicit PSK
  remains an authoritative override for other encrypted profiles.
- Added `vstarcamctl --version` and aligned package, CLI, and changelog release
  identity.

### Security

- Clarified that the PPPP XOR transform provides protocol compatibility, not
  cryptographic confidentiality, integrity, or peer authentication.
- Added tracked-file and distribution gates for private research, capture,
  local-configuration, and credential artifacts.

### Removed

- Removed unsupported placeholders, unused runtime override seams, production
  test fakes, and unsafe raw access to parameterized writes that require typed
  validation or bounded cleanup.
- Removed the static YAML and dotenv-style configuration examples now that
  discovery creates a current camera profile directly and environment fields
  are documented alongside it.

## [0.1.0] - 2026-07-14

### Added

- Async Python API and `vstarcamctl` command-line interface.
- Local discovery and PPPP/CGI camera sessions.
- Status, configuration, media, detection, lighting, audio, time, and Wi-Fi
  command surfaces with centralized safety gates.
- Secret masking, dry-run support, retry controls, and opt-in integration
  tests.

### Changed

- Prepared project documentation, CI, and contributor infrastructure for
  public development.
