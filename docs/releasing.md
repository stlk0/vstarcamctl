# Releasing

[Documentation index](index.md) · [Back to README](../README.md)

Releases are built by GitHub Actions and published through PyPI Trusted
Publishing. Maintainers must not upload an existing local `dist/` directory.

Hatchling builds both distributions with reproducible timestamps from
`SOURCE_DATE_EPOCH`. The source archive includes the library, tests, examples,
documentation, and contributor configuration selected in `pyproject.toml`;
the wheel includes the library and its command catalog. Both artifacts pass
the private-file and credential checks before publication.

Before the first release, create the GitHub repository and confirm that its
CI passes, including Windows. Configure an environment named `pypi` with a
required reviewer. If the PyPI project does not exist yet, register a
[pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
from the PyPI account Publishing page:

- PyPI project: `vstarcamctl`
- GitHub owner/repository: `stlk0` / `vstarcamctl`
- Workflow filename: `release.yml`
- Environment: `pypi`

For an existing PyPI project, add the same mapping from its Publishing settings.
The first successful publish creates the PyPI project. Maintainer accounts
must use two-factor authentication. Enable GitHub private vulnerability
reporting so the process linked from `SECURITY.md` is available.

For each release:

1. Update the single project version in `pyproject.toml`.
2. Add a dated matching section to `CHANGELOG.md`.
3. Merge the fully verified change into `main`.
4. Create and push an annotated `v<version>` tag on that commit.
5. Review the protected `pypi` deployment after the build and clean-install
   checks pass.
6. Verify the published metadata, documentation links, wheel installation,
   `vstarcamctl --version`, and `vstarcamctl --help` without contacting a
   camera.

The workflow rejects lightweight tags, tags outside `main`, version mismatches,
and changelog entries without a release date. The publish job downloads the
exact artifacts produced by the preceding verification job and authenticates
with a short-lived OpenID Connect token.
