from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.release_checks import (
    _forbidden_name,
    _signature_matches,
    check_distributions,
    check_tree,
)

_LOCAL_TOOL_PATHS = [
    ".editorconfig",
    ".devcontainer/devcontainer.json",
    ".devcontainer.json",
    "devcontainer.json",
    ".python-version",
    ".tool-versions",
    ".direnv/cache",
    ".idea/workspace.xml",
    ".vscode/settings.json",
    ".agents/skills/review/SKILL.md",
    ".codex/config.toml",
    ".claude/settings.json",
    ".gemini/settings.json",
    ".cursor/rules/review.mdc",
    ".cursorrules",
    ".windsurf/rules/review.md",
    ".windsurfrules",
    ".aider.conf.yml",
    ".aider.tags.cache/notes",
    ".cline/settings.json",
    ".clinerules/review.md",
    ".roo/rules/review.md",
    ".roorules",
    ".continue/config.yaml",
    ".opencode/agents/review.md",
    "opencode.json",
    "opencode.jsonc",
    ".mcp.json",
    "nested/agents.md",
    "nested/AgEnTs.OvErRiDe.Md",
    "CLAUDE.md",
    "claude.local.md",
    "GEMINI.md",
    "nested/SkIlL.Md",
    ".github/copilot-instructions.md",
    ".github/instructions/python.instructions.md",
    ".github/prompts/review.prompt.md",
    ".github/agents/reviewer.agent.md",
    ".github/skills/review/SKILL.md",
    "nested/ReSeArCh/notes.md",
    "docs/research-notes.md",
    "research_notes.json",
]
_PUBLIC_PROJECT_PATHS = [
    "README.md",
    ".gitattributes",
    "pyproject.toml",
    "pytest.ini",
    ".github/workflows/ci.yml",
    "docs/architecture.md",
    "docs/researcher-guide.md",
    "tests/fixtures/camera_response.json",
    "src/vstarcamctl/secrets.py",
    "tests/test_secrets.py",
]


@pytest.mark.parametrize(
    "name",
    [
        "AGENTS.md",
        "research/notes.md",
        "captures/session.bin",
        "camera.local.yaml",
        "settings.local.json",
        ".env.production",
        "private.pem",
        "firmware.apk",
        *_LOCAL_TOOL_PATHS,
    ],
)
def test_release_gate_rejects_private_artifact_names(name):
    assert _forbidden_name(name)


@pytest.mark.parametrize("name", _PUBLIC_PROJECT_PATHS)
def test_release_gate_allows_public_project_names(name):
    assert not _forbidden_name(name)


def test_gitignore_excludes_local_tools_without_hiding_public_project_files(tmp_path):
    ignore = Path(__file__).resolve().parents[1] / ".gitignore"
    (tmp_path / ".gitignore").write_bytes(ignore.read_bytes())
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        input="\0".join([*_LOCAL_TOOL_PATHS, *_PUBLIC_PROJECT_PATHS]).encode() + b"\0",
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert set(result.stdout.decode().split("\0")) - {""} == set(_LOCAL_TOOL_PATHS)


def test_release_gate_detects_only_complete_high_confidence_secret_signatures():
    synthetic_header = b"-----BEGIN " + b"PRIVATE KEY-----\n"
    assert _signature_matches(synthetic_header) == ["private key"]
    assert _signature_matches(b"documentation says BEGIN PRIVATE KEY without delimiters") == []


def _write_sdist(path, name="README.md", payload=b"public source\n"):
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"example-1.0/{name}")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


@pytest.mark.parametrize("archive_type", ["wheel", "sdist"])
@pytest.mark.parametrize(
    ("name", "violation"),
    [
        ("captures/private.bin", "private path"),
        (".github/prompts/review.prompt.md", "private path"),
        (".editorconfig", "private path"),
        ("Research/notes.md", "private path"),
        ("public.txt", "credential signature"),
    ],
)
def test_distribution_gate_checks_both_archives(tmp_path, archive_type, name, violation):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    sdist = tmp_path / "example-1.0.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example/__init__.py", "")
    _write_sdist(sdist)

    check_distributions(tmp_path)

    payload = b"private" if violation == "private path" else b"ghp_" + b"A" * 36
    if archive_type == "wheel":
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr(name, payload)
    else:
        _write_sdist(sdist, name, payload)
    with pytest.raises(SystemExit, match=violation):
        check_distributions(tmp_path)


@pytest.mark.parametrize(
    ("name", "violation", "history_forbidden"),
    [
        ("camera.local.yaml", "private path", True),
        ("nested/AgEnTs.OvErRiDe.Md", "private path", True),
        ("Research/notes.md", "private path", True),
        (".editorconfig", "private path", False),
        ("public.txt", "credential signature", True),
    ],
)
def test_tree_gate_checks_forced_adds_and_only_allows_legacy_editor_defaults(
    tmp_path, name, violation, history_forbidden
):
    def git(*args):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init")
    ignore = Path(__file__).resolve().parents[1] / ".gitignore"
    (tmp_path / ".gitignore").write_bytes(ignore.read_bytes())
    (tmp_path / "README.md").write_text("public documentation\n")
    git("add", ".gitignore", "README.md")
    git("commit", "-m", "Public baseline")
    check_tree()

    payload = "private fixture content" if violation == "private path" else "ghp_" + "A" * 36
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(payload)
    git("add", "--force", name)
    git("commit", "-m", "Synthetic private material")
    with pytest.raises(SystemExit, match=f"current tree contains .*{violation}"):
        check_tree()

    git("rm", name)
    git("commit", "-m", "Remove synthetic private material")
    if not history_forbidden:
        check_tree()
        return
    with pytest.raises(SystemExit, match=f"Git history contains .*{violation}") as error:
        check_tree()
    assert payload not in str(error.value)
