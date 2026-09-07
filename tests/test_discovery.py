import pytest

import vstarcamctl
import vstarcamctl.discovery as discovery
from vstarcamctl.discovery import discover_camera, discover_cameras
from vstarcamctl.discovery_udp import LanDiscoveryResult
from vstarcamctl.errors import DiscoveryError
from vstarcamctl.transport import KNOWN_PSKS, DiscoveredCamera, psk_for_device_id


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("VSTG", "vstarcam2018"),
        ("VSTH", "vstarcam2018"),
        ("VSTJ", "vstarcam2019"),
        ("VSTK", "vstarcam2019"),
        ("VSTL", "vstarcam2019"),
        ("VSTM", "vstarcam2019"),
        ("VSTN", "vstarcam2019"),
        ("VSTP", "vstarcam2019"),
        ("VSGG", "vstarcam2021"),
        ("VSGM", "vstarcam2021"),
        ("VSGS", "vstarcam2021"),
    ],
)
def test_known_device_prefix_selects_transport_psk(prefix, expected):
    assert psk_for_device_id(f"{prefix}-000001-AAAAA") == expected


@pytest.mark.parametrize("device_id", [None, "", "vstg-000001-AAAAA", "UNKNOWN-000001-AAAAA", 123])
def test_unknown_or_malformed_device_id_has_no_transport_psk(device_id):
    assert psk_for_device_id(device_id) is None


def test_known_psk_probe_order_is_stable_and_unique():
    assert KNOWN_PSKS == ("vstarcam2018", "vstarcam2019", "vstarcam2021")


def test_discovery_api_is_exported_from_package_root():
    assert vstarcamctl.DiscoveredCamera is DiscoveredCamera
    assert vstarcamctl.LanDiscoveryResult is LanDiscoveryResult
    assert vstarcamctl.discover_camera is discover_camera
    assert vstarcamctl.discover_cameras is discover_cameras
    assert not hasattr(vstarcamctl, "EncryptedDiscoveryResult")


def camera(
    host: str,
    port: int,
    device_id: str,
    *,
    encryption: str = "NONE",
) -> DiscoveredCamera:
    return DiscoveredCamera(
        host=host,
        port=port,
        device_id=device_id,
        protocol="binary",
        encryption=encryption,
    )


async def test_discover_cameras_normalizes_deduplicates_and_sorts_raw_results(monkeypatch):
    calls = {}
    seeded = [
        LanDiscoveryResult("192.0.2.20", 40002, "VSTG-000002-BBBBB", encrypted=False),
        LanDiscoveryResult("192.0.2.10", 40001, "VSTJ-000001-AAAAA", encrypted=False),
        LanDiscoveryResult(
            "192.0.2.10",
            40001,
            "VSTJ-000001-AAAAA",
            encrypted=True,
            psk="vstarcam2019",
        ),
        LanDiscoveryResult("192.0.2.5", 40003, "VSGG-000003-CCCCC", encrypted=False),
    ]

    def seeded_discovery(remote_addr, **kwargs):
        calls["seeded"] = (remote_addr, kwargs)
        return seeded

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(discovery, "discover_with_seeds", seeded_discovery)
    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    result = await discover_cameras(timeout=0.25)

    assert [
        (item.host, item.port, item.device_id, item.encryption, item.psk) for item in result
    ] == [
        ("192.0.2.10", 40001, "VSTJ-000001-AAAAA", "PSK", "vstarcam2019"),
        ("192.0.2.20", 40002, "VSTG-000002-BBBBB", "plaintext", None),
        ("192.0.2.5", 40003, "VSGG-000003-CCCCC", "plaintext", None),
    ]
    assert calls == {
        "seeded": (
            "255.255.255.255",
            {
                "ports": (32108, 12833),
                "seeds": ("vstarcam2018", "vstarcam2019", "vstarcam2021"),
                "timeout": 0.25,
                "expected_host": None,
                "require_matching_profile": True,
            },
        ),
    }


@pytest.mark.parametrize(
    ("encrypted", "expected_encryption"), [(True, "PSK"), (False, "plaintext")]
)
async def test_discover_cameras_preserves_the_selected_psk(
    monkeypatch,
    encrypted,
    expected_encryption,
):
    def seeded_discovery(_remote_addr, **kwargs):
        assert kwargs["seeds"] == ("vstarcam2019",)
        assert kwargs["require_matching_profile"] is False
        return [
            LanDiscoveryResult(
                "192.0.2.10",
                40001,
                "CUSTOM-000001-AAAAA",
                encrypted=encrypted,
                psk="vstarcam2019" if encrypted else None,
            )
        ]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(discovery, "discover_with_seeds", seeded_discovery)
    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    result = await discover_cameras(psk="vstarcam2019")

    assert len(result) == 1
    assert result[0].encryption == expected_encryption
    assert result[0].psk == "vstarcam2019"
    assert "vstarcam2019" not in repr(result[0])


async def test_discover_cameras_rejects_conflicting_profile_metadata(monkeypatch):
    seeded = [
        LanDiscoveryResult(
            "192.0.2.10",
            40001,
            "VSTG-000001-AAAAA",
            encrypted=True,
            psk="vstarcam2018",
        ),
        LanDiscoveryResult(
            "192.0.2.10",
            40001,
            "VSTG-000001-AAAAA",
            encrypted=True,
            psk="conflicting-seed",
        ),
    ]

    async def inline_to_thread(_function, *_args, **_kwargs):
        return seeded

    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    with pytest.raises(DiscoveryError, match="conflicting transport profiles"):
        await discover_cameras()


async def test_discover_cameras_binds_the_raw_backend_to_source_address(monkeypatch):
    calls = []

    def seeded_discovery(remote_addr, **kwargs):
        calls.append((remote_addr, kwargs))
        return []

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(discovery, "discover_with_seeds", seeded_discovery)
    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    assert await discover_cameras(source_address="192.0.2.44") == []
    assert calls == [
        (
            "255.255.255.255",
            {
                "ports": (32108, 12833),
                "seeds": ("vstarcam2018", "vstarcam2019", "vstarcam2021"),
                "timeout": 3.0,
                "expected_host": None,
                "require_matching_profile": True,
                "source_address": "192.0.2.44",
            },
        )
    ]


async def test_explicit_broadcast_host_does_not_filter_camera_senders(monkeypatch):
    calls = []

    def seeded_discovery(remote_addr, **kwargs):
        calls.append((remote_addr, kwargs["expected_host"]))
        return []

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(discovery, "discover_with_seeds", seeded_discovery)
    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    assert await discover_cameras(host="255.255.255.255") == []
    assert calls == [("255.255.255.255", None)]


@pytest.mark.parametrize(
    ("items", "device_id", "message"),
    [
        ([], None, "no cameras were discovered"),
        (
            [camera("192.0.2.10", 40001, "CAMERA-A")],
            "CAMERA-B",
            "selected device ID",
        ),
        (
            [
                camera("192.0.2.10", 40001, "CAMERA-A"),
                camera("192.0.2.11", 40002, "CAMERA-B"),
            ],
            None,
            "multiple cameras",
        ),
        (
            [
                camera("192.0.2.10", 40001, "CAMERA-A", encryption="PSK"),
                camera("192.0.2.11", 40001, "CAMERA-A", encryption="PSK"),
            ],
            "CAMERA-A",
            "multiple endpoints",
        ),
    ],
)
async def test_discover_camera_rejects_missing_or_ambiguous_results(
    monkeypatch,
    items,
    device_id,
    message,
):
    async def discover(**_kwargs):
        return items

    monkeypatch.setattr(discovery, "discover_cameras", discover)

    with pytest.raises(DiscoveryError, match=message):
        await discover_camera(device_id=device_id)


async def test_discover_camera_prefers_one_psk_endpoint_for_selected_identity(monkeypatch):
    expected = camera("192.0.2.10", 40002, "CAMERA-A", encryption="PSK")
    items = [
        camera("192.0.2.10", 40001, "CAMERA-A"),
        expected,
        camera("192.0.2.11", 40003, "CAMERA-B"),
    ]

    async def discover(**_kwargs):
        return items

    monkeypatch.setattr(discovery, "discover_cameras", discover)

    assert await discover_camera(device_id="CAMERA-A") == expected


async def test_discover_camera_uses_only_the_profile_mapped_by_a_known_device_id(monkeypatch):
    expected = camera("192.0.2.10", 40002, "VSTJ-000001-AAAAA", encryption="PSK")

    async def discover(**kwargs):
        assert kwargs["psk"] == "vstarcam2019"
        return [expected]

    monkeypatch.setattr(discovery, "discover_cameras", discover)

    assert await discover_camera(device_id=expected.device_id) == expected


async def test_discover_camera_returns_one_unambiguous_identity(monkeypatch):
    expected = camera("192.0.2.10", 40002, "CAMERA-A", encryption="PSK")

    async def discover(**_kwargs):
        return [expected]

    monkeypatch.setattr(discovery, "discover_cameras", discover)

    assert await discover_camera() == expected


async def test_discover_cameras_wraps_network_errors(monkeypatch):
    def fail_discovery(*_args, **_kwargs):
        raise OSError("synthetic network failure")

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(discovery, "discover_with_seeds", fail_discovery)
    monkeypatch.setattr(discovery.asyncio, "to_thread", inline_to_thread)

    with pytest.raises(DiscoveryError, match="camera discovery failed"):
        await discover_cameras(source_address="192.0.2.44")


async def test_discover_cameras_rejects_invalid_options_before_network(monkeypatch):
    def raw_discovery(*_args, **_kwargs):
        pytest.fail("discovery must not run for invalid input")

    monkeypatch.setattr(discovery, "discover_with_seeds", raw_discovery)

    with pytest.raises(DiscoveryError, match="finite number greater than zero"):
        await discover_cameras(timeout=0)


async def test_discover_camera_rejects_invalid_device_id_before_network(monkeypatch):
    async def discover(**_kwargs):
        pytest.fail("discovery must not run for invalid device ID")

    monkeypatch.setattr(discovery, "discover_cameras", discover)

    with pytest.raises(DiscoveryError, match="non-empty text"):
        await discover_camera(device_id=" ")
