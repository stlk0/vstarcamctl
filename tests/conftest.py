from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolate_unit_tests_from_local_config(request, monkeypatch, tmp_path):
    """Prevent unit tests from implicitly loading ignored developer credentials."""

    if request.node.get_closest_marker("integration") is None:
        monkeypatch.chdir(tmp_path)
