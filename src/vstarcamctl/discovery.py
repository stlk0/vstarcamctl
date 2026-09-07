"""High-level LAN discovery for compatible cameras."""

from __future__ import annotations

import asyncio

from .config import VStarcamConfig
from .discovery_udp import discover_with_seeds
from .errors import ConfigError, DiscoveryError
from .transport import KNOWN_PSKS, DiscoveredCamera, psk_for_device_id

_BROADCAST_HOST = "255.255.255.255"


async def discover_cameras(
    *,
    timeout: float = 3.0,
    host: str | None = None,
    source_address: str | None = None,
    psk: str | None = None,
    discovery_port: int = 32108,
    udp_port: int = 12833,
) -> list[DiscoveredCamera]:
    """Discover compatible cameras without reading configuration files or environment."""

    config = VStarcamConfig(
        host=host,
        source_address=source_address,
        psk=psk,
        discovery_port=discovery_port,
        udp_port=udp_port,
        timeout=timeout,
    )
    try:
        config.validate()
    except ConfigError as exc:
        raise DiscoveryError(str(exc)) from exc

    remote_addr = host or _BROADCAST_HOST
    seeded_options = {
        "ports": (discovery_port, udp_port),
        "seeds": (psk,) if psk is not None else KNOWN_PSKS,
        "timeout": timeout,
        "expected_host": None if remote_addr == _BROADCAST_HOST else host,
        "require_matching_profile": psk is None,
    }
    if source_address is not None:
        seeded_options["source_address"] = source_address
    try:
        seeded = await asyncio.to_thread(
            discover_with_seeds,
            remote_addr,
            **seeded_options,
        )
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"camera discovery failed: {exc}") from exc

    items = [
        DiscoveredCamera(
            host=camera.host,
            port=camera.port,
            device_id=camera.device_id,
            protocol="binary",
            encryption="PSK" if camera.encrypted else "plaintext",
            psk=camera.psk or psk,
        )
        for camera in seeded
    ]
    unique: dict[tuple[str, int, str], DiscoveredCamera] = {}
    for item in items:
        key = (item.host, item.port, item.device_id)
        existing = unique.get(key)
        if (
            existing is not None
            and existing.psk is not None
            and item.psk is not None
            and existing.psk != item.psk
        ):
            raise DiscoveryError("camera discovery returned conflicting transport profiles")
        if existing is None or (item.encryption == "PSK" and existing.encryption != "PSK"):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.host, item.port, item.device_id))


def _select_discovered_camera(
    items: list[DiscoveredCamera],
    device_id: str | None,
) -> DiscoveredCamera:
    if not items:
        raise DiscoveryError(
            "no cameras were discovered; verify LAN access and, for an unknown encrypted "
            "profile, verify or pass the correct explicit psk"
        )

    if device_id is None:
        identities = {item.device_id for item in items}
        if len(identities) != 1:
            raise DiscoveryError(
                "multiple cameras were discovered; pass host or a device_id returned by "
                "LAN discovery"
            )
        matches = items
    else:
        matches = [item for item in items if item.device_id == device_id]
        if not matches:
            raise DiscoveryError("the selected device ID was not discovered")

    encrypted = [item for item in matches if item.encryption == "PSK"]
    candidates = encrypted or matches
    endpoints = {(item.host, item.port) for item in candidates}
    if len({item.host for item in matches}) != 1 or len(endpoints) != 1:
        raise DiscoveryError(
            "the selected camera answered from multiple endpoints; pass host to narrow discovery"
        )
    return candidates[0]


async def discover_camera(
    *,
    device_id: str | None = None,
    timeout: float = 3.0,
    host: str | None = None,
    source_address: str | None = None,
    psk: str | None = None,
    discovery_port: int = 32108,
    udp_port: int = 12833,
) -> DiscoveredCamera:
    """Discover exactly one unambiguous camera endpoint."""

    if device_id is not None and (
        not isinstance(device_id, str) or not device_id.strip() or device_id != device_id.strip()
    ):
        raise DiscoveryError("device_id must be non-empty text without surrounding whitespace")
    selected_psk = psk if psk is not None else psk_for_device_id(device_id)
    items = await discover_cameras(
        timeout=timeout,
        host=host,
        source_address=source_address,
        psk=selected_psk,
        discovery_port=discovery_port,
        udp_port=udp_port,
    )
    return _select_discovered_camera(items, device_id)
