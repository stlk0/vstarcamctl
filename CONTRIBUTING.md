# Contributing to VStarcamCtl

Thank you for helping improve VStarcamCtl. Contributions should keep the
project local-only, safe by default, and usable without access to the
maintainer's hardware.

## Before opening an issue

- Search existing issues first.
- Use the bug or feature template.
- Remove device IDs, serial numbers, public IPs, MAC addresses, SSIDs,
  usernames, passwords, tokens, hashes, and full `params` output.
- Report security vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md).

Questions that can be answered by the user documentation belong in
[docs/](docs/index.md). A focused issue is welcome when the documentation is
unclear or incomplete.

## Development setup

VStarcamCtl requires Python 3.11 or newer.

```bash
git clone https://github.com/stlk0/vstarcamctl.git
cd vstarcamctl
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[transport,dev]"
```

Run the same checks used by CI:

```bash
ruff check src tests examples
ruff format --check src tests examples
pytest -q
python -m build
python -m twine check dist/*
```

Unit tests use deterministic fakes and must not contact a camera. Real-device
integration tests are opt-in, are never run by CI, and are not required from
contributors.

## Public repository scope

The published project is a camera-control library and CLI. Product code,
tests, examples, user documentation, and safety metadata belong here.

Do not add vendor application binaries, packet-analysis tools, raw device
dumps, credentials, private network data, or device-specific investigation
logs to a contribution. Test fixtures must use clearly fictional values from
reserved address ranges.

## Making a change

1. Create a branch from the latest `main`.
2. Keep the change focused and add or update tests.
3. Update user documentation when behavior changes.
4. Run the checks above.
5. Open a pull request and complete its safety checklist.

For substantial features or behavior changes, open an issue first so the
scope and safety model can be agreed before implementation.

### Command safety

Changes to device commands require extra care:

- keep command policy in `src/vstarcamctl/data/catalog.yaml`;
- classify reads and writes explicitly;
- keep unvalidated writes experimental;
- require confirmation for risky or destructive writes;
- use one-shot delivery when a retry could duplicate a state change;
- document a getter, inverse action, or recovery path where applicable;
- add unit tests for validation and safety gates.

Never add password, token, key, or command-ID brute forcing.

## Commit and pull request titles

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>(optional-scope): short imperative summary
```

Allowed types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`,
`ci`, `chore`, and `revert`. Common scopes include `cli`, `transport`, `media`,
`detection`, `docs`, and `ci`.

Examples:

```text
feat(cli): add structured status output
fix(transport): close sessions after timeouts
docs: clarify one-shot command safety
feat(api)!: rename the camera connection method
```

Use `!` and a `BREAKING CHANGE:` footer for incompatible changes. Pull request
titles are checked automatically because squash merges use the title as the
resulting commit message.

## Pull request expectations

A pull request should:

- explain the user-visible outcome and safety implications;
- link a related issue when one exists;
- include tests appropriate to the risk;
- avoid unrelated formatting or refactoring;
- contain no secrets or private device data;
- pass all required checks and resolve review conversations.

By contributing, you agree that your contribution is licensed under the
project's [MIT License](LICENSE).
