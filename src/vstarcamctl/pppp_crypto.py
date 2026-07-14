"""PPPP packet obfuscation used by compatible CS2/VStarcam devices.

The algorithm reduces an arbitrary ASCII seed to a four-byte effective key and
uses that key with a fixed lookup table. It provides protocol compatibility
rather than cryptographic protection.
"""

from __future__ import annotations

from functools import reduce
from operator import xor

KEY_TABLE = bytes.fromhex(
    "7c9ce84a13dedcb22f2123e4307b3d8c"
    "bc0b270c3cf79ae7087196009785efc1"
    "1fc4dba1c2ebd901faba3b05b8158783"
    "2872d18b5ad6da9358feaacc6e1bf0a3"
    "88ab43c00db545384f502266207f075b"
    "14981d9ba72ab9a8cbf1fc4947063eb1"
    "0e043a945eee541134dd4df9ecc7c9e3"
    "781a6f706ba4bda95dd5f8e5bb26af42"
    "37d8e1020aae5f1cc573094e6924906d"
    "12b319ad748a2940f52dbea559e0f479"
    "d24bce8982488425c6912ba2fb8fe9a6"
    "b09e3f65f603312eac0f952c5ced39b7"
    "336c567eb4a0fd7a815351868d9f77ff"
    "6a80dfe2bf10d775645776f355cdd0c8"
    "18e6364162cf99f2324c67606192cad3"
    "ea637d16b68ed46835c3529d46441e17"
)


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
        ciphertext.append(byte ^ KEY_TABLE[index])
        previous_byte = ciphertext[-1]
    return bytes(ciphertext)


def decrypt_packet(ciphertext: bytes, key: bytes) -> bytes:
    if len(key) != 4:
        raise ValueError("effective PPPP key must be four bytes")
    plaintext = bytearray()
    previous_byte = 0
    for byte in ciphertext:
        index = (key[previous_byte & 3] + previous_byte) & 0xFF
        plaintext.append(byte ^ KEY_TABLE[index])
        previous_byte = byte
    return bytes(plaintext)
