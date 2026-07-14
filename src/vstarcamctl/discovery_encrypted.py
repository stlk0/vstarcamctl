"""LAN discovery for compatible PPPP devices using a configured seed."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Iterable

from .pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet

MAGIC = 0xF1
LAN_SEARCH = 0x30
LAN_SEARCH_EXT = 0x32
PUNCH_PACKET = 0x41


@dataclass(frozen=True, slots=True)
class EncryptedDiscoveryResult:
    host: str
    port: int
    vuid: str
    encrypted: bool


def build_search_packets(seed: str) -> tuple[bytes, bytes]:
    key = derive_effective_key(seed)
    return tuple(
        encrypt_packet(struct.pack("!BBH", MAGIC, message_type, 0), key)
        for message_type in (LAN_SEARCH, LAN_SEARCH_EXT)
    )


def parse_punch_packet(data: bytes) -> str | None:
    if len(data) < 24:
        return None
    magic, message_type, payload_size = struct.unpack("!BBH", data[:4])
    if magic != MAGIC or message_type != PUNCH_PACKET or payload_size != len(data) - 4:
        return None
    prefix, serial, check_code = struct.unpack("!8sI8s", data[4:24])
    try:
        prefix_text = prefix.rstrip(b"\0").decode("ascii", errors="strict")
        check_text = check_code.rstrip(b"\0").decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None
    if not prefix_text.isalpha() or not prefix_text.isupper():
        return None
    if not check_text.isalpha() or not check_text.isupper():
        return None
    return f"{prefix_text}-{serial:06d}-{check_text}"


def discover_with_seed(
    host: str,
    *,
    ports: Iterable[int] = (32108, 12833),
    seed: str = "vstarcam2018",
    timeout: float = 2.0,
    expected_host: str | None = None,
    expected_vuid: str | None = None,
    require_encrypted: bool = False,
    stop_after_first: bool = False,
) -> list[EncryptedDiscoveryResult]:
    """Send only the two expected search messages for one explicitly known seed."""

    key = derive_effective_key(seed)
    encrypted_requests = build_search_packets(seed)
    plaintext_requests = tuple(
        struct.pack("!BBH", MAGIC, message_type, 0) for message_type in (LAN_SEARCH, LAN_SEARCH_EXT)
    )
    destination_ports = tuple(dict.fromkeys(int(port) for port in ports))
    expected_address = socket.gethostbyname(expected_host) if expected_host else None
    results: dict[tuple[str, int, str], EncryptedDiscoveryResult] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        for port in destination_ports:
            for request in (*plaintext_requests, *encrypted_requests):
                sock.sendto(request, (host, port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                response, address = sock.recvfrom(2048)
            except socket.timeout:
                break
            vuid = parse_punch_packet(response)
            encrypted = False
            if vuid is None:
                try:
                    decrypted = decrypt_packet(response, key)
                    vuid = parse_punch_packet(decrypted)
                    encrypted = vuid is not None
                except (ValueError, UnicodeDecodeError):
                    vuid = None
            if vuid is not None:
                if expected_address is not None and address[0] != expected_address:
                    continue
                if expected_vuid is not None and vuid != expected_vuid:
                    continue
                if require_encrypted and not encrypted:
                    continue
                item = EncryptedDiscoveryResult(address[0], address[1], vuid, encrypted)
                results[(item.host, item.port, item.vuid)] = item
                if stop_after_first:
                    break
    return sorted(results.values(), key=lambda item: (item.host, item.port, item.vuid))
