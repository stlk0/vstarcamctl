import pytest

from vstarcamctl.pppp_crypto import derive_effective_key


def test_vstarcam_seed_effective_key():
    assert derive_effective_key("vstarcam2018") == bytes.fromhex("2cd46006")


def test_seed_validation():
    with pytest.raises(ValueError):
        derive_effective_key("")
    with pytest.raises(ValueError):
        derive_effective_key("café")
