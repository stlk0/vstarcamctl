import pytest

from vstarcamctl.pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet


def test_vstarcam_seed_effective_key():
    assert derive_effective_key("vstarcam2018") == bytes.fromhex("2cd46006")


def test_encryption_round_trip():
    key = derive_effective_key("vstarcam2018")
    plaintext = bytes.fromhex("f1300000")
    ciphertext = encrypt_packet(plaintext, key)
    assert ciphertext != plaintext
    assert decrypt_packet(ciphertext, key) == plaintext


def test_seed_validation():
    with pytest.raises(ValueError):
        derive_effective_key("")
    with pytest.raises(ValueError):
        derive_effective_key("café")
