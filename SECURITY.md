# Security policy

VStarcamCtl controls physical devices and handles camera credentials. Please
report vulnerabilities privately and avoid including real device data in any
public issue, discussion, commit, or pull request.

## Supported versions

Until the project reaches a stable release, security fixes are applied to the
latest release and the current `main` branch. Older revisions are not
maintained.

## Reporting a vulnerability

Use GitHub's
[private vulnerability reporting](https://github.com/stlk0/vstarcamctl/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

If that form is unavailable, use a public contact method listed on the
[maintainer's GitHub profile](https://github.com/stlk0) to request a private
channel. Do not include vulnerability details in the initial public message.

Include only the information needed to reproduce and assess the problem:

- affected version or commit;
- impact and realistic attack conditions;
- minimal reproduction steps;
- a proposed mitigation, if known.

Replace all device IDs, addresses, account data, credentials, tokens, hashes,
SSIDs, and media with fictional placeholders. Do not attach full camera
parameter output or private device files.

The maintainer aims to acknowledge a report within seven days, provide an
initial assessment within 30 days, and coordinate disclosure after a fix or
mitigation is available. Complex reports may take longer; updates will be
shared through the private advisory.

## Scope

Relevant reports include credential disclosure, authentication bypasses,
unsafe command authorization, secret-masking failures, and vulnerabilities in
the library or CLI. Camera firmware and vendor-service vulnerabilities are out
of scope unless VStarcamCtl directly causes or exposes the issue. Report
third-party vulnerabilities to the responsible vendor or upstream project.

Test only systems you own or are explicitly authorized to assess. Keep tests
local, bounded, and reversible, and stop when an operation has an unknown
outcome.
