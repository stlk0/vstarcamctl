"""Check the public Git tree and Python distributions for private artifacts."""

from __future__ import annotations

import argparse
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

_MAX_BLOB_SIZE = 5_000_000
_PRIVATE_DIRECTORIES = {
    ".agents",
    ".claude",
    ".cline",
    ".clinerules",
    ".codex",
    ".continue",
    ".copilot",
    ".cursor",
    ".devcontainer",
    ".direnv",
    ".gemini",
    ".idea",
    ".opencode",
    ".release-smoke",
    ".roo",
    ".roorules",
    ".ruff_cache",
    ".sdist-smoke",
    ".uv-cache",
    ".venv",
    ".vscode",
    ".wheel-smoke",
    ".windsurf",
    "captures",
    "pcaps",
    "research",
}
_PRIVATE_NAMES = {
    ".clinerules",
    ".cursorrules",
    ".devcontainer.json",
    ".editorconfig",
    ".mcp.json",
    ".python-version",
    ".roorules",
    ".tool-versions",
    ".windsurfrules",
    "agents.md",
    "agents.override.md",
    "claude.md",
    "claude.local.md",
    "devcontainer.json",
    "gemini.md",
    "opencode.json",
    "opencode.jsonc",
    "skill.md",
}
_PRIVATE_GITHUB_ENTRIES = {"agents", "copilot-instructions.md", "instructions", "prompts", "skills"}
_PRIVATE_SUFFIXES = (
    ".apk",
    ".key",
    ".local.env",
    ".local.json",
    ".local.yaml",
    ".local.yml",
    ".p12",
    ".pcap",
    ".pcapng",
    ".pem",
    ".pfx",
)
_CREDENTIAL_SIGNATURES = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
}


def _git(*args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(
        ("git", *args),
        check=True,
        input=input_data,
        stdout=subprocess.PIPE,
    ).stdout


def _forbidden_name(name: str) -> bool:
    path = PurePosixPath(name)
    parts = tuple(part.lower() for part in path.parts)
    basename = path.name.lower()
    return (
        bool(set(parts) & _PRIVATE_DIRECTORIES)
        or basename in _PRIVATE_NAMES
        or any(part.startswith(".aider") for part in parts)
        or any(part.startswith(("research.", "research-", "research_")) for part in parts)
        or any(
            parent == ".github" and child in _PRIVATE_GITHUB_ENTRIES
            for parent, child in zip(parts, parts[1:])
        )
        or basename.endswith(_PRIVATE_SUFFIXES)
        or basename.startswith(".env")
        or (basename.startswith("secrets.") and basename != "secrets.py")
    )


def _signature_matches(data: bytes) -> list[str]:
    return [label for label, pattern in _CREDENTIAL_SIGNATURES.items() if pattern.search(data)]


def check_tree() -> None:
    """Reject private filenames and credential signatures in Git and its history."""

    tracked = [name for name in _git("ls-files", "-z").decode().split("\0") if name]
    rejected = [name for name in tracked if _forbidden_name(name)]
    if rejected:
        raise SystemExit(f"current tree contains {len(rejected)} forbidden private path(s)")

    history_names = (
        _git("log", "--all", "--format=", "--name-only", "-z")
        .decode(errors="surrogateescape")
        .split("\0")
    )
    # The former root editor defaults were public and contain no private state.
    # Keep that history while excluding the file from every new tree/artifact.
    history_rejected = {
        name for name in history_names if name and name != ".editorconfig" and _forbidden_name(name)
    }
    if history_rejected:
        raise SystemExit(f"Git history contains {len(history_rejected)} forbidden private path(s)")

    current_matches = []
    for name in tracked:
        path = Path(name)
        if path.is_file() and not path.is_symlink():
            current_matches.extend(_signature_matches(path.read_bytes()))
    if current_matches:
        raise SystemExit(f"current tree contains {len(current_matches)} credential signature(s)")

    object_ids = {
        line.partition(b" ")[0] for line in _git("rev-list", "--objects", "--all").splitlines()
    }
    metadata = _git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_data=b"\n".join(object_ids) + b"\n",
    ).splitlines()
    history_matches = []
    for line in metadata:
        object_id, object_type, raw_size = line.split()
        if object_type != b"blob" or int(raw_size) > _MAX_BLOB_SIZE:
            continue
        for label in _signature_matches(_git("cat-file", "blob", object_id.decode())):
            history_matches.append((label, object_id[:12].decode()))
    if history_matches:
        details = ", ".join(f"{label} in blob {object_id}" for label, object_id in history_matches)
        raise SystemExit(f"Git history contains credential signature(s): {details}")


def _check_archive(name: Path, members: Iterable[tuple[str, bytes]]) -> None:
    private_names = 0
    signature_matches = []
    for member_name, data in members:
        private_names += _forbidden_name(member_name)
        signature_matches.extend(_signature_matches(data))
    if private_names:
        raise SystemExit(f"{name}: contains {private_names} forbidden private path(s)")
    if signature_matches:
        raise SystemExit(f"{name}: contains {len(signature_matches)} credential signature(s)")


def _wheel_members(path: Path) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as package:
        for info in package.infolist():
            data = (
                package.read(info)
                if not info.is_dir() and info.file_size <= _MAX_BLOB_SIZE
                else b""
            )
            yield info.filename, data


def _sdist_members(path: Path) -> Iterable[tuple[str, bytes]]:
    with tarfile.open(path, "r:*") as package:
        for info in package.getmembers():
            data = b""
            if info.isfile() and info.size <= _MAX_BLOB_SIZE:
                stream = package.extractfile(info)
                if stream is not None:
                    data = stream.read()
            yield info.name, data


def check_distributions(directory: Path) -> None:
    """Reject private paths and credential signatures in one wheel and sdist."""

    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected one wheel and one sdist, found {len(wheels)} wheel(s) "
            f"and {len(sdists)} sdist(s)"
        )
    _check_archive(wheels[0], _wheel_members(wheels[0]))
    _check_archive(sdists[0], _sdist_members(sdists[0]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("tree", "distributions"))
    parser.add_argument("path", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.check == "tree":
        check_tree()
    else:
        check_distributions(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
