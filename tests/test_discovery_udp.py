import struct

import pytest

import vstarcamctl.discovery_udp as discovery
from vstarcamctl import wait_for_camera_on_lan
from vstarcamctl.discovery_udp import (
    MAGIC,
    PUNCH_PACKET,
    LanDiscoveryResult,
    build_search_packets,
    discover_with_seeds,
    parse_punch_packet,
)
from vstarcamctl.pppp_crypto import derive_effective_key, encrypt_packet


def test_search_packets_use_expected_wire_message():
    assert build_search_packets("vstarcam2018") == (
        bytes.fromhex("49b573d5"),
        bytes.fromhex("49b7ed6c"),
    )


def test_parse_plain_punch_packet():
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    assert parse_punch_packet(packet) == "VSTJ-000123-ABCDE"


def test_invalid_packet_is_ignored():
    assert parse_punch_packet(b"invalid") is None


def test_non_ascii_punch_packet_is_ignored():
    payload = struct.pack("!8sI8s", b"\xff" + (b"\0" * 7), 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    assert parse_punch_packet(packet) is None


class FakeSocket:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []
        self.bound = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def setsockopt(self, *_args):
        pass

    def bind(self, address):
        self.bound.append(address)

    def sendto(self, payload, address):
        self.sent.append((payload, address))

    def settimeout(self, _timeout):
        pass

    def recvfrom(self, _size):
        if self.responses:
            return self.responses.pop(0)
        raise TimeoutError


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def test_directed_discovery_filters_sender_device_id_and_encryption(monkeypatch):
    expected_host = "192.0.2.10"
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    encrypted = encrypt_packet(packet, derive_effective_key("vstarcam2018"))

    wrong_payload = struct.pack("!8sI8s", b"VSTJ", 999, b"ABCDE")
    wrong_packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(wrong_payload)) + wrong_payload
    wrong_identity = encrypt_packet(wrong_packet, derive_effective_key("vstarcam2018"))
    fake_socket = FakeSocket(
        [
            (encrypted, ("192.0.2.99", 40000)),
            (wrong_identity, (expected_host, 40001)),
            (packet, (expected_host, 40001)),
            (encrypted, (expected_host, 40001)),
        ]
    )
    monkeypatch.setattr(
        "vstarcamctl.discovery_udp.socket.socket",
        lambda *_args: fake_socket,
    )
    monkeypatch.setattr(
        "vstarcamctl.discovery_udp.socket.gethostbyname",
        lambda host: host,
    )
    results = discover_with_seeds(
        expected_host,
        seeds=("vstarcam2018",),
        expected_host=expected_host,
        expected_device_id="VSTJ-000123-ABCDE",
        require_encrypted=True,
        stop_after_first=True,
        timeout=0.1,
    )
    assert results == [
        LanDiscoveryResult(expected_host, 40001, "VSTJ-000123-ABCDE", True, "vstarcam2018")
    ]

    fake_socket.responses = [
        (packet, (expected_host, 40002)),
        (encrypted, (expected_host, 40003)),
    ]
    results = discover_with_seeds(
        expected_host,
        seeds=("vstarcam2018",),
        expected_host=expected_host,
        expected_device_id="VSTJ-000123-ABCDE",
        timeout=0.1,
    )
    assert [(item.port, item.encrypted) for item in results] == [
        (40002, False),
        (40003, True),
    ]


def test_multi_seed_discovery_uses_one_socket_and_reports_matching_profile(monkeypatch):
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    encrypted = encrypt_packet(packet, derive_effective_key("vstarcam2019"))

    fake_socket = FakeSocket([(encrypted, ("192.0.2.10", 40001))])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    results = discover_with_seeds(
        "255.255.255.255",
        ports=(32108, 32108),
        seeds=("vstarcam2018", "vstarcam2019", "vstarcam2021"),
        timeout=0.1,
        require_matching_profile=True,
    )

    assert len(fake_socket.sent) == 8
    assert len(results) == 1
    assert results[0].device_id == "VSTJ-000123-ABCDE"
    assert results[0].psk == "vstarcam2019"
    assert "vstarcam2019" not in repr(results[0])


def test_encrypted_only_discovery_does_not_send_plaintext_probes(monkeypatch):
    fake_socket = FakeSocket()
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    assert (
        discover_with_seeds(
            "255.255.255.255",
            ports=(32108,),
            seeds=("vstarcam2018", "vstarcam2019", "vstarcam2021"),
            timeout=0.1,
            require_encrypted=True,
        )
        == []
    )
    assert [payload for payload, _address in fake_socket.sent] == [
        packet
        for seed in ("vstarcam2018", "vstarcam2019", "vstarcam2021")
        for packet in build_search_packets(seed)
    ]


def test_auto_discovery_rejects_a_seed_that_conflicts_with_device_prefix(monkeypatch):
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    encrypted = encrypt_packet(packet, derive_effective_key("vstarcam2018"))

    fake_socket = FakeSocket([(encrypted, ("192.0.2.10", 40001))])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    assert (
        discover_with_seeds(
            "255.255.255.255",
            ports=(32108,),
            seeds=("vstarcam2018", "vstarcam2019"),
            timeout=0.1,
            require_matching_profile=True,
        )
        == []
    )


def test_empty_seed_set_runs_plaintext_only_discovery(monkeypatch):
    payload = struct.pack("!8sI8s", b"PLAI", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload

    fake_socket = FakeSocket([(packet, ("192.0.2.10", 40001))])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    results = discover_with_seeds(
        "255.255.255.255",
        ports=(32108,),
        seeds=(),
        timeout=0.1,
        stop_after_first=True,
    )

    assert len(fake_socket.sent) == 2
    assert results == [
        LanDiscoveryResult(
            "192.0.2.10",
            40001,
            "PLAI-000123-ABCDE",
            encrypted=False,
        )
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"seeds": "vstarcam2018"}, "iterable of strings"),
        ({"seeds": ("",)}, "non-empty strings"),
        (
            {"seeds": (), "require_encrypted": True},
            "encrypted discovery requires at least one",
        ),
        ({"seeds": ("vstarcam2018",), "ports": ()}, "at least one discovery port"),
        ({"seeds": ("vstarcam2018",), "ports": (True,)}, "integers from 1"),
        ({"seeds": ("vstarcam2018",), "timeout": 0}, "finite positive"),
        (
            {"seeds": ("vstarcam2018",), "expected_device_id": " "},
            "expected device ID",
        ),
    ],
)
def test_multi_seed_discovery_rejects_invalid_inputs_before_network(monkeypatch, options, message):
    monkeypatch.setattr(
        discovery.socket,
        "socket",
        lambda *_args: pytest.fail("invalid discovery input must not open a socket"),
    )
    with pytest.raises(ValueError, match=message):
        discover_with_seeds("255.255.255.255", **options)


def test_multi_seed_discovery_rejects_an_ambiguous_decryption(monkeypatch):
    def punch(prefix):
        payload = struct.pack("!8sI8s", prefix, 123, b"ABCDE")
        return struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload

    decoded = {
        derive_effective_key("vstarcam2018"): punch(b"VSTG"),
        derive_effective_key("vstarcam2019"): punch(b"VSTJ"),
    }

    fake_socket = FakeSocket([(b"synthetic-encrypted-response", ("192.0.2.10", 40001))])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)
    monkeypatch.setattr(discovery, "decrypt_packet", lambda _response, key: decoded[key])

    with pytest.raises(ValueError, match="multiple transport profiles"):
        discover_with_seeds(
            "255.255.255.255",
            ports=(32108,),
            seeds=("vstarcam2018", "vstarcam2019"),
            timeout=0.1,
            require_matching_profile=True,
        )


def test_discovery_timeout_includes_slow_udp_sends(monkeypatch):
    clock = FakeClock()

    class SlowSendSocket:
        def __init__(self):
            self.send_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def setsockopt(self, *_args):
            pass

        def bind(self, _address):
            pass

        def sendto(self, _payload, _address):
            self.send_count += 1
            clock.now += 1.1

        def settimeout(self, _timeout):
            pytest.fail("receive timeout must not be configured after the deadline")

        def recvfrom(self, _size):
            pytest.fail("receive must not start after the deadline")

    fake_socket = SlowSendSocket()
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    assert (
        discover_with_seeds(
            "255.255.255.255",
            ports=(32108,),
            seeds=("vstarcam2018",),
            timeout=1.0,
        )
        == []
    )
    assert fake_socket.send_count == 1


def test_discovery_binds_to_explicit_source_address(monkeypatch):
    fake_socket = FakeSocket()
    monkeypatch.setattr(discovery.socket, "socket", lambda *_args: fake_socket)

    assert (
        discover_with_seeds(
            "255.255.255.255",
            ports=(32108,),
            seeds=("vstarcam2018",),
            timeout=0.01,
            source_address="192.0.2.44",
        )
        == []
    )
    assert fake_socket.bound == [("192.0.2.44", 0)]


def _wait_for_test(**overrides):
    arguments = {
        "expected_device_id": "VSTJ-000123-ABCDE",
        "ports": (32108, 12833),
        "total_timeout": 1.0,
        "probe_timeout": 0.2,
        "probe_interval": 0.1,
        "require_encrypted": False,
    }
    arguments.update(overrides)
    return wait_for_camera_on_lan("255.255.255.255", **arguments)


def test_wait_repeats_discovery_until_exact_identity_is_seen(monkeypatch):
    expected = LanDiscoveryResult("192.0.2.10", 40000, "VSTJ-000123-ABCDE", encrypted=True)
    replies = [[], [expected]]
    calls = []
    clock = FakeClock()

    def fake_discover(host, **kwargs):
        calls.append((host, kwargs))
        return replies.pop(0)

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test() == expected
    assert len(calls) == 2
    assert calls[0] == (
        "255.255.255.255",
        {
            "ports": (32108, 12833),
            "seeds": ("vstarcam2019",),
            "timeout": 0.2,
            "expected_device_id": "VSTJ-000123-ABCDE",
            "require_encrypted": False,
            "require_matching_profile": True,
            "stop_after_first": True,
            "source_address": None,
        },
    )


def test_wait_uses_an_explicit_psk_as_the_only_profile(monkeypatch):
    expected = LanDiscoveryResult(
        "192.0.2.10",
        40000,
        "CUSTOM-000123-ABCDE",
        encrypted=True,
        psk="custom-profile",
    )
    calls = []
    clock = FakeClock()

    def fake_discover(host, **kwargs):
        calls.append((host, kwargs))
        return [expected]

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)

    assert (
        _wait_for_test(
            expected_device_id=expected.device_id,
            psk="custom-profile",
        )
        == expected
    )
    assert calls[0][1]["seeds"] == ("custom-profile",)
    assert calls[0][1]["require_matching_profile"] is False


def test_wait_defensively_ignores_unrelated_identity(monkeypatch):
    unrelated = LanDiscoveryResult("192.0.2.9", 40000, "OTHER-000001-CODE", True)
    expected = LanDiscoveryResult("192.0.2.10", 40000, "VSTJ-000123-ABCDE", encrypted=True)
    replies = [[unrelated], [expected]]
    clock = FakeClock()
    monkeypatch.setattr(discovery, "discover_with_seeds", lambda *_args, **_kwargs: replies.pop(0))
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test(probe_interval=0) == expected


def test_wait_defensively_ignores_plaintext_when_encryption_is_required(monkeypatch):
    plaintext = LanDiscoveryResult("192.0.2.10", 40000, "VSTJ-000123-ABCDE", encrypted=False)
    encrypted = LanDiscoveryResult("192.0.2.10", 40001, "VSTJ-000123-ABCDE", encrypted=True)
    replies = [[plaintext], [encrypted]]
    clock = FakeClock()

    def fake_discover(*_args, **kwargs):
        clock.now += 0.01
        return replies.pop(0)

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test(require_encrypted=True, probe_interval=0) == encrypted
    assert replies == []
    assert clock.sleeps == []


def test_wait_rejects_an_exact_result_returned_after_total_deadline(monkeypatch):
    expected = LanDiscoveryResult("192.0.2.10", 40000, "VSTJ-000123-ABCDE", encrypted=True)
    clock = FakeClock()

    def fake_discover(*_args, **kwargs):
        clock.now += kwargs["timeout"] + 0.1
        return [expected]

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test(total_timeout=0.2) is None
    assert clock.sleeps == []


def test_wait_caps_probe_and_sleep_at_total_deadline(monkeypatch):
    clock = FakeClock()
    probe_timeouts = []

    def fake_discover(*_args, **kwargs):
        probe_timeouts.append(kwargs["timeout"])
        clock.now += kwargs["timeout"]
        return []

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test(total_timeout=1.0, probe_timeout=0.7, probe_interval=0.2) is None
    assert probe_timeouts == pytest.approx([0.7, 0.1])
    assert clock.sleeps == pytest.approx([0.2])
    assert clock.now == pytest.approx(1.0)


def test_wait_caps_final_sleep_and_starts_no_probe_after_deadline(monkeypatch):
    clock = FakeClock()
    probe_timeouts = []

    def fake_discover(*_args, **kwargs):
        probe_timeouts.append(kwargs["timeout"])
        clock.now += kwargs["timeout"]
        return []

    monkeypatch.setattr(discovery, "discover_with_seeds", fake_discover)
    monkeypatch.setattr(discovery.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(discovery.time, "sleep", clock.sleep)

    assert _wait_for_test(total_timeout=0.5, probe_timeout=0.4, probe_interval=0.3) is None
    assert probe_timeouts == [0.4]
    assert clock.sleeps == pytest.approx([0.1])
    assert clock.now == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_device_id": ""}, "expected device ID"),
        ({"expected_device_id": " VSTJ-000123-ABCDE "}, "surrounding whitespace"),
        ({"ports": ()}, "at least one discovery port"),
        ({"ports": (True,)}, "ports must be integers"),
        ({"psk": ""}, "PSK must be non-empty ASCII"),
        ({"psk": "café"}, "PSK must be non-empty ASCII"),
        ({"total_timeout": 0}, "total_timeout must be positive"),
        ({"probe_timeout": -1}, "probe_timeout must be positive"),
        ({"probe_interval": -0.1}, "probe_interval must be non-negative"),
        ({"probe_interval": True}, "probe_interval must be a finite number"),
        ({"total_timeout": float("inf")}, "total_timeout must be a finite number"),
        ({"source_address": "::1"}, "source address must be an IPv4 address"),
    ],
)
def test_wait_rejects_invalid_inputs_before_discovery(monkeypatch, overrides, message):
    monkeypatch.setattr(
        discovery,
        "discover_with_seeds",
        lambda *_args, **_kwargs: pytest.fail("discovery must not run for invalid input"),
    )

    with pytest.raises(ValueError, match=message) as error:
        _wait_for_test(**overrides)

    assert "VSTJ-000123-ABCDE" not in str(error.value)
