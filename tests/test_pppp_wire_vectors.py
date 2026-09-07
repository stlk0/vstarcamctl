"""Fixed PPPP vectors that do not derive expectations with production code."""

from __future__ import annotations

import pytest

from vstarcamctl.pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet


@pytest.mark.parametrize(
    ("plaintext_hex", "ciphertext_hex"),
    [
        ("f1300000", "49b573d5"),
        ("f1320000", "49b7ed6c"),
    ],
)
def test_vstarcam_discovery_wire_vectors(plaintext_hex: str, ciphertext_hex: str):
    key = derive_effective_key("vstarcam2018")
    plaintext = bytes.fromhex(plaintext_hex)
    ciphertext = bytes.fromhex(ciphertext_hex)

    assert encrypt_packet(plaintext, key) == ciphertext
    assert decrypt_packet(ciphertext, key) == plaintext
