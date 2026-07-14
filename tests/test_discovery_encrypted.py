import struct

from vstarcamctl.discovery_encrypted import (
    LAN_SEARCH,
    MAGIC,
    PUNCH_PACKET,
    build_search_packets,
    parse_punch_packet,
)
from vstarcamctl.pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet


def test_search_packets_use_expected_wire_message():
    key = derive_effective_key("vstarcam2018")
    first, second = build_search_packets("vstarcam2018")
    assert decrypt_packet(first, key) == struct.pack("!BBH", MAGIC, LAN_SEARCH, 0)
    assert decrypt_packet(second, key) == struct.pack("!BBH", MAGIC, 0x32, 0)


def test_parse_plain_and_encrypted_punch_packet():
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    assert parse_punch_packet(packet) == "VSTJ-000123-ABCDE"
    key = derive_effective_key("vstarcam2018")
    assert parse_punch_packet(decrypt_packet(encrypt_packet(packet, key), key)) == (
        "VSTJ-000123-ABCDE"
    )


def test_invalid_packet_is_ignored():
    assert parse_punch_packet(b"invalid") is None


def test_non_ascii_punch_packet_is_ignored():
    payload = struct.pack("!8sI8s", b"\xff" + (b"\0" * 7), 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    assert parse_punch_packet(packet) is None


def test_directed_discovery_filters_sender_vuid_and_encryption(monkeypatch):
    expected_host = "192.0.2.10"
    payload = struct.pack("!8sI8s", b"VSTJ", 123, b"ABCDE")
    packet = struct.pack("!BBH", MAGIC, PUNCH_PACKET, len(payload)) + payload
    encrypted = encrypt_packet(packet, derive_effective_key("vstarcam2018"))

    class FakeSocket:
        def __init__(self):
            self.responses = [
                (encrypted, ("192.0.2.99", 40000)),
                (encrypted, (expected_host, 40001)),
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def setsockopt(self, *_args):
            pass

        def bind(self, _address):
            pass

        def sendto(self, _payload, _address):
            pass

        def settimeout(self, _timeout):
            pass

        def recvfrom(self, _size):
            if self.responses:
                return self.responses.pop(0)
            raise TimeoutError

    fake_socket = FakeSocket()
    monkeypatch.setattr(
        "vstarcamctl.discovery_encrypted.socket.socket",
        lambda *_args: fake_socket,
    )
    monkeypatch.setattr(
        "vstarcamctl.discovery_encrypted.socket.gethostbyname",
        lambda host: host,
    )
    from vstarcamctl.discovery_encrypted import discover_with_seed

    results = discover_with_seed(
        expected_host,
        expected_host=expected_host,
        expected_vuid="VSTJ-000123-ABCDE",
        require_encrypted=True,
        stop_after_first=True,
        timeout=0.1,
    )
    assert [(item.host, item.port) for item in results] == [(expected_host, 40001)]
