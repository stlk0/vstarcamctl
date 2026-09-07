"""UDP LAN discovery for compatible PPPP devices using bounded transport profiles."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from math import isfinite
from typing import Iterable

from .pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet
from .transport import KNOWN_PSKS, psk_for_device_id

MAGIC = 0xF1
LAN_SEARCH = 0x30
LAN_SEARCH_EXT = 0x32
PUNCH_PACKET = 0x41


@dataclass(frozen=True, slots=True)
class LanDiscoveryResult:
    host: str
    port: int
    device_id: str
    encrypted: bool
    psk: str | None = field(default=None, repr=False)


def _validate_source_address(source_address: str | None) -> str | None:
    if source_address is None:
        return None
    if not isinstance(source_address, str):
        raise ValueError("source address must be an IPv4 address")
    try:
        return str(IPv4Address(source_address))
    except ValueError as exc:
        raise ValueError("source address must be an IPv4 address") from exc


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


def discover_with_seeds(
    host: str,
    *,
    ports: Iterable[int] = (32108, 12833),
    seeds: Iterable[str],
    timeout: float = 2.0,
    expected_host: str | None = None,
    expected_device_id: str | None = None,
    require_encrypted: bool = False,
    require_matching_profile: bool = False,
    stop_after_first: bool = False,
    source_address: str | None = None,
) -> list[LanDiscoveryResult]:
    """Discover with a bounded set of transport seeds on one UDP socket."""

    if not isinstance(host, str) or not host.strip():
        raise ValueError("discovery host cannot be empty")
    if isinstance(timeout, bool):
        raise ValueError("discovery timeout must be a finite positive number")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("discovery timeout must be a finite positive number") from exc
    if not isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("discovery timeout must be a finite positive number")
    if expected_device_id is not None and (
        not isinstance(expected_device_id, str)
        or not expected_device_id.strip()
        or expected_device_id != expected_device_id.strip()
    ):
        raise ValueError("expected device ID must be non-empty text without surrounding whitespace")
    if not isinstance(require_encrypted, bool) or not isinstance(require_matching_profile, bool):
        raise ValueError("discovery profile requirements must be boolean")
    if not isinstance(stop_after_first, bool):
        raise ValueError("stop_after_first must be a boolean")
    if isinstance(seeds, (str, bytes)):
        raise ValueError("PPPP seeds must be an iterable of strings")
    try:
        supplied_seeds = tuple(seeds)
    except TypeError as exc:
        raise ValueError("PPPP seeds must be an iterable of strings") from exc
    if any(not isinstance(seed, str) or not seed for seed in supplied_seeds):
        raise ValueError("PPPP seeds must be non-empty strings")
    unique_seeds = tuple(dict.fromkeys(supplied_seeds))
    if not unique_seeds and (require_encrypted or require_matching_profile):
        raise ValueError("encrypted discovery requires at least one PPPP seed")
    try:
        destination_ports = tuple(dict.fromkeys(ports))
    except TypeError as exc:
        raise ValueError("discovery ports must be an iterable of integers") from exc
    if not destination_ports:
        raise ValueError("at least one discovery port is required")
    if any(
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        for port in destination_ports
    ):
        raise ValueError("discovery ports must be integers from 1 to 65535")

    deadline = time.monotonic() + timeout_value
    candidates = tuple((seed, derive_effective_key(seed)) for seed in unique_seeds)
    encrypted_requests = tuple(
        request for seed in unique_seeds for request in build_search_packets(seed)
    )
    plaintext_requests = (
        ()
        if require_encrypted
        else tuple(
            struct.pack("!BBH", MAGIC, message_type, 0)
            for message_type in (LAN_SEARCH, LAN_SEARCH_EXT)
        )
    )
    expected_address = socket.gethostbyname(expected_host) if expected_host else None
    source_address = _validate_source_address(source_address)
    results: dict[tuple[str, int, str, str | None], LanDiscoveryResult] = {}
    if deadline - time.monotonic() <= 0:
        return []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((source_address or "0.0.0.0", 0))
        for port in destination_ports:
            for request in (*plaintext_requests, *encrypted_requests):
                if deadline - time.monotonic() <= 0:
                    return []
                sock.sendto(request, (host, port))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                response, address = sock.recvfrom(2048)
            except socket.timeout:
                break
            if deadline - time.monotonic() <= 0:
                break
            device_id = parse_punch_packet(response)
            encrypted = False
            matched_psk = None
            if device_id is None:
                decoded_matches: list[tuple[str, str]] = []
                for seed, key in candidates:
                    try:
                        decrypted = decrypt_packet(response, key)
                        candidate_id = parse_punch_packet(decrypted)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if candidate_id is None:
                        continue
                    if require_matching_profile and psk_for_device_id(candidate_id) != seed:
                        continue
                    decoded_matches.append((candidate_id, seed))
                if len(decoded_matches) > 1:
                    raise ValueError("discovery response matched multiple transport profiles")
                if decoded_matches:
                    device_id, matched_psk = decoded_matches[0]
                    encrypted = True
            if device_id is not None:
                if expected_address is not None and address[0] != expected_address:
                    continue
                if expected_device_id is not None and device_id != expected_device_id:
                    continue
                if require_encrypted and not encrypted:
                    continue
                if deadline - time.monotonic() <= 0:
                    break
                item = LanDiscoveryResult(address[0], address[1], device_id, encrypted, matched_psk)
                results[(item.host, item.port, item.device_id, item.psk)] = item
                if stop_after_first:
                    break
    return sorted(
        results.values(),
        key=lambda item: (item.host, item.port, item.device_id, item.psk or ""),
    )


def wait_for_camera_on_lan(
    host: str,
    *,
    expected_device_id: str,
    ports: Iterable[int],
    psk: str | None = None,
    total_timeout: float,
    probe_timeout: float,
    probe_interval: float,
    require_encrypted: bool,
    source_address: str | None = None,
) -> LanDiscoveryResult | None:
    """Wait until one exact camera identity is visible through LAN discovery.

    A match proves only that ``expected_device_id`` was seen on the LAN. It does not
    prove QR acknowledgement, authentication, or control readiness.
    """

    if not isinstance(host, str) or not host.strip():
        raise ValueError("discovery host cannot be empty")
    if not isinstance(expected_device_id, str) or not expected_device_id.strip():
        raise ValueError("expected device ID cannot be empty")
    if expected_device_id != expected_device_id.strip():
        raise ValueError("expected device ID must not have surrounding whitespace")
    if not isinstance(require_encrypted, bool):
        raise ValueError("require_encrypted must be a boolean")
    if psk is not None:
        if not isinstance(psk, str):
            raise ValueError("PPPP PSK must be non-empty ASCII text")
        try:
            derive_effective_key(psk)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("PPPP PSK must be non-empty ASCII text") from exc
    mapped_psk = psk_for_device_id(expected_device_id)
    selected_psks = (psk,) if psk is not None else ((mapped_psk,) if mapped_psk else KNOWN_PSKS)
    source_address = _validate_source_address(source_address)

    try:
        destination_ports = tuple(dict.fromkeys(ports))
    except TypeError as exc:
        raise ValueError("discovery ports must be an iterable of integers") from exc
    if not destination_ports:
        raise ValueError("at least one discovery port is required")
    if any(
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        for port in destination_ports
    ):
        raise ValueError("discovery ports must be integers from 1 to 65535")

    timings: dict[str, float] = {}
    for name, value in (
        ("total_timeout", total_timeout),
        ("probe_timeout", probe_timeout),
        ("probe_interval", probe_interval),
    ):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite number")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not isfinite(number):
            raise ValueError(f"{name} must be a finite number")
        timings[name] = number
    if timings["total_timeout"] <= 0:
        raise ValueError("total_timeout must be positive")
    if timings["probe_timeout"] <= 0:
        raise ValueError("probe_timeout must be positive")
    if timings["probe_interval"] < 0:
        raise ValueError("probe_interval must be non-negative")

    deadline = time.monotonic() + timings["total_timeout"]
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        results = discover_with_seeds(
            host,
            ports=destination_ports,
            seeds=selected_psks,
            timeout=min(timings["probe_timeout"], remaining),
            expected_device_id=expected_device_id,
            require_encrypted=require_encrypted,
            require_matching_profile=psk is None,
            stop_after_first=True,
            source_address=source_address,
        )
        if deadline - time.monotonic() <= 0:
            return None
        for result in results:
            if result.device_id == expected_device_id and (
                result.encrypted or not require_encrypted
            ):
                return result

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        delay = min(timings["probe_interval"], remaining)
        if delay:
            time.sleep(delay)
