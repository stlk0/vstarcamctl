"""PPPP packet obfuscation used by compatible CS2/VStarcam devices.

The algorithm reduces an arbitrary ASCII seed to a four-byte effective key and
uses that key with a fixed lookup table. It provides protocol compatibility
rather than cryptographic protection.
"""

from __future__ import annotations

from functools import reduce
from operator import xor

from aiopppp.encrypt import XOR1_KEY_TABLE


def derive_effective_key(seed: str | bytes) -> bytes:
    """Map a PPPP init-string seed to its four-byte wire key."""

    data = seed.encode("ascii") if isinstance(seed, str) else bytes(seed)
    if not data:
        raise ValueError("PPPP seed cannot be empty")
    if any(byte > 0x7F for byte in data):
        raise ValueError("PPPP seed must contain ASCII bytes only")
    total = sum(data)
    return bytes(
        (
            total & 0xFF,
            (-total) & 0xFF,
            sum(byte // 3 for byte in data) & 0xFF,
            reduce(xor, data, 0),
        )
    )


def encrypt_packet(plaintext: bytes, key: bytes) -> bytes:
    if len(key) != 4:
        raise ValueError("effective PPPP key must be four bytes")
    ciphertext = bytearray()
    previous_byte = 0
    for byte in plaintext:
        index = (key[previous_byte & 3] + previous_byte) & 0xFF
        ciphertext.append(byte ^ XOR1_KEY_TABLE[index])
        previous_byte = ciphertext[-1]
    return bytes(ciphertext)


def decrypt_packet(ciphertext: bytes, key: bytes) -> bytes:
    if len(key) != 4:
        raise ValueError("effective PPPP key must be four bytes")
    plaintext = bytearray()
    previous_byte = 0
    for byte in ciphertext:
        index = (key[previous_byte & 3] + previous_byte) & 0xFF
        plaintext.append(byte ^ XOR1_KEY_TABLE[index])
        previous_byte = byte
    return bytes(plaintext)
