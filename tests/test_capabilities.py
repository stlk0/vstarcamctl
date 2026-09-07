from __future__ import annotations

import pytest

from vstarcamctl._capabilities import capability_state


@pytest.mark.parametrize(
    ("status", "aliases", "expected"),
    [
        ({"a": 1}, ("a",), "supported"),
        ({"a": 0}, ("a",), "unsupported"),
        ({"a": 0, "b": "2"}, ("a", "b"), "supported"),
        ({"a": 0}, ("a", "b"), "unsupported"),
        ({"a": -1}, ("a",), "unknown"),
        ({"a": "invalid"}, ("a",), "unknown"),
        ({"unrelated": 1}, ("a",), "unknown"),
    ],
)
def test_capability_state_is_conservative(status, aliases, expected):
    assert capability_state(status, aliases) == expected
