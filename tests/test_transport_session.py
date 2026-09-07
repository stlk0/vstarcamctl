import asyncio
import logging
import struct
from enum import Enum
from types import SimpleNamespace

import pytest

from vstarcamctl.config import VStarcamConfig
from vstarcamctl.discovery_udp import LanDiscoveryResult
from vstarcamctl.errors import (
    TransportAuthenticationError,
    TransportCommandCancelledError,
    TransportError,
    TransportTimeoutError,
)
from vstarcamctl.framing import CGI_HEADER, CGI_START_CODE
from vstarcamctl.transport_aiopppp import (
    AioppppTransport,
    _CgiResponseFragment,
    _CgiResponseQueue,
    _codec_for_seed,
    _secure_aiopppp_logging,
)


class FakeBinarySession:
    def __init__(self):
        self.dev = SimpleNamespace(dev_id="TEST-1-UID", encryption="fake")
        self.sent = []
        self.device_is_ready = asyncio.Event()
        self.packet_queue = asyncio.Queue()
        self.video_chunk_queue = asyncio.Queue()

    async def send(self, packet):
        self.sent.append(packet)

    async def handle_drw(self, drw_pkt):
        if drw_pkt._channel == FakeChannel.Video:
            self.video_chunk_queue.put_nowait((getattr(drw_pkt, "_epoch", 0), drw_pkt))


class FakeBinaryCmdPkt:
    pass


class FakeChannel:
    Command = SimpleNamespace(value=0)
    Video = SimpleNamespace(value=1)


class FakePacketType(Enum):
    DrwAck = 0xD1
    Drw = 0xD0


def test_aiopppp_dependency_logs_are_masked_once():
    logger = logging.getLogger("aiopppp.session")
    packet_logger = logging.getLogger("aiopppp.packets")
    root_logger = logging.getLogger()

    _secure_aiopppp_logging()
    _secure_aiopppp_logging()

    matching = [item for item in logger.filters if item.__class__.__name__ == "SecretMaskingFilter"]
    assert len(matching) == 1
    record = logging.LogRecord(
        logger.name,
        logging.WARNING,
        __file__,
        1,
        "Device %s lost",
        ("DevID(VENDOR-PRIVATE-SERIAL)",),
        None,
    )
    assert matching[0].filter(record)
    assert record.getMessage() == "Device DevID(***) lost"

    packet_matching = [
        item
        for item in packet_logger.filters
        if item.__class__.__name__ == "_AioppppPacketLogFilter"
    ]
    root_matching = [
        item for item in root_logger.filters if item.__class__.__name__ == "_AioppppPacketLogFilter"
    ]
    assert len(packet_matching) == len(root_matching) == 1
    packet_record = logging.LogRecord(
        "aiopppp.packets",
        logging.DEBUG,
        "/venv/site-packages/aiopppp/packets.py",
        1,
        "Parsed packet: %s",
        (b"private-camera-payload",),
        None,
    )
    assert packet_matching[0].filter(packet_record)
    assert packet_record.getMessage() == "aiopppp packet detail omitted"

    raw_packet_record = logging.LogRecord(
        "root",
        logging.WARNING,
        "/venv/site-packages/aiopppp/packets.py",
        1,
        "Failed to parse JSON: %s",
        (b"private-camera-payload",),
        None,
    )
    assert root_matching[0].filter(raw_packet_record)
    assert raw_packet_record.getMessage() == "aiopppp packet detail omitted"

    application_record = logging.LogRecord(
        "root",
        logging.WARNING,
        __file__,
        1,
        "application warning: %s",
        ("unchanged",),
        None,
    )
    assert root_matching[0].filter(application_record)
    assert application_record.getMessage() == "application warning: unchanged"


def video_boundary(frame_type: int, body: bytes, *, declared_size: int | None = None) -> bytes:
    header = bytearray(32)
    header[:4] = b"\x55\xaa\x15\xa8"
    struct.pack_into("<H", header, 4, frame_type)
    struct.pack_into("<I", header, 16, declared_size if declared_size is not None else len(body))
    return bytes(header) + body


def video_packet(index: int, payload: bytes, *, epoch: int = 0):
    return SimpleNamespace(
        _channel=FakeChannel.Video,
        _cmd_idx=index,
        _epoch=epoch,
        get_drw_payload=lambda: payload,
    )


async def test_raw_session_sends_only_punch_and_waits_for_camera_ready():
    punch = object()
    keepalive = object()
    ready_ack = object()
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "Packet": lambda _type, _payload: ready_ack,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
        "make_p2palive_pkt": lambda: keepalive,
        "make_punch_pkt": lambda _dev_id: punch,
    }

    session_class = AioppppTransport._session_class(api)
    session = session_class()
    await session.send_initial_packets()
    await session.setup_device()

    assert session.sent == [
        punch,
        punch,
        punch,
        punch,
        punch,
        keepalive,
        keepalive,
        keepalive,
        ready_ack,
    ]
    assert session.device_is_ready.is_set()


async def test_raw_session_binds_udp_to_configured_source(monkeypatch):
    endpoint = {}

    class Protocol:
        def __init__(self, callback):
            self.callback = callback

    class Loop:
        async def create_datagram_endpoint(self, factory, **kwargs):
            endpoint.update(kwargs)
            endpoint["protocol"] = factory()
            return "bound-transport", endpoint["protocol"]

    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "SessionUDPProtocol": Protocol,
    }
    session = AioppppTransport._session_class(api)(source_address="192.0.2.44")
    session.dev.addr = "192.0.2.10"
    session.dev.port = 24680
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.asyncio.get_running_loop", Loop)

    assert await session.create_udp() == "bound-transport"
    assert endpoint["local_addr"] == ("192.0.2.44", 0)
    assert endpoint["remote_addr"] == ("192.0.2.10", 24680)


def test_raw_session_ignores_unsupported_packet_type():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda _data: pytest.fail("unsupported packet was parsed"),
    }
    session = AioppppTransport._session_class(api)()

    session.on_receive(b"\xf1\x43\x00\x00")

    assert session.packet_queue.empty()


def test_raw_session_still_parses_supported_packet_type():
    parsed = SimpleNamespace(
        type=FakePacketType.Drw,
        get_payload=lambda: b"payload",
    )
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda _data: parsed,
    }
    session = AioppppTransport._session_class(api)()

    session.on_receive(b"\xf1\xd0\x00\x00")

    assert session.packet_queue.get_nowait() is parsed


async def test_raw_sessions_keep_their_own_seed_codec():
    class Encryption(Enum):
        XOR1 = 1

    class SessionBase:
        def __init__(self, dev):
            self.dev = dev
            self.packet_queue = asyncio.Queue()
            self.drw_waiters = {}
            self.transport = SimpleNamespace(sent=[])
            self.transport.sendto = lambda data, address: self.transport.sent.append(
                (data, address)
            )

        async def send(self, packet):
            await self._send(packet)

    class WirePacket:
        type = FakePacketType.DrwAck

        def __bytes__(self):
            return b"\xf1\xd1\x00\x00"

    def parse_packet(data):
        return SimpleNamespace(
            type=FakePacketType.DrwAck,
            get_payload=lambda: data,
        )

    global_codec = (lambda data: b"global-decoded", lambda data: b"global-encoded")
    api = {
        "BinarySession": SessionBase,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {Encryption.XOR1: global_codec},
        "parse_packet": parse_packet,
        "Encryption": Encryption,
    }
    _, first_codec = _codec_for_seed(api, "vstarcam2018")
    _, second_codec = _codec_for_seed(api, "vstarcam2021")
    assert api["ENC_METHODS"][Encryption.XOR1] is global_codec
    session_class = AioppppTransport._session_class(api)
    device = SimpleNamespace(encryption=Encryption.XOR1, addr="192.0.2.1", port=12345)
    first = session_class(device, packet_codec=first_codec)
    second = session_class(device, packet_codec=second_codec)

    # Creating another session and changing the dependency's global mapping
    # must not alter either direction of the first session's codec.
    api["ENC_METHODS"][Encryption.XOR1] = second_codec
    plaintext = bytes(WirePacket())
    first.on_receive(first_codec[1](plaintext))
    received = first.packet_queue.get_nowait()
    assert received.get_payload() == plaintext

    await first.send(WirePacket())
    encoded, address = first.transport.sent[0]
    assert first_codec[0](encoded) == plaintext
    assert second_codec[0](encoded) != plaintext
    assert address == (device.addr, device.port)
    assert second._packet_codec is second_codec


async def test_raw_session_discards_same_session_livestream_video_chunks():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()

    await session.handle_drw(video_packet(1, b"discarded"))

    assert session.video_chunk_queue.empty()


async def test_raw_session_does_not_start_aiopppp_unbounded_video_processor():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()

    session.start_video_queue()

    assert isinstance(session.process_video_task, asyncio.Future)
    assert not isinstance(session.process_video_task, asyncio.Task)
    session.process_video_task.cancel()


async def test_opt_in_capture_returns_keyframe_across_epoch_rollover():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()
    transport = AioppppTransport(observed_config())
    transport._session = session
    transport._connected = True

    await transport.start_video_capture()
    receiver = asyncio.create_task(transport.receive_video_keyframe(timeout=1))
    await session.handle_drw(
        video_packet(0xFFFF, video_boundary(0, b"key", declared_size=9), epoch=3)
    )
    await session.handle_drw(video_packet(0, b"-frame", epoch=4))

    assert await receiver == b"key-frame"
    await transport.stop_video_capture()


async def test_video_capture_timeout_preserves_capture_for_retry():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()
    transport = AioppppTransport(observed_config())
    transport._session = session
    transport._connected = True

    await transport.start_video_capture()
    with pytest.raises(TransportTimeoutError, match="keyframe"):
        await transport.receive_video_keyframe(timeout=0.001)
    await session.handle_drw(video_packet(2, video_boundary(0, b"key")))

    assert await transport.receive_video_keyframe(timeout=1) == b"key"
    await transport.stop_video_capture()


async def test_stop_video_capture_clears_state_and_wakes_receiver():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()
    transport = AioppppTransport(observed_config())
    transport._session = session
    transport._connected = True

    await transport.start_video_capture()
    receiver = asyncio.create_task(transport.receive_video_keyframe(timeout=10))
    await asyncio.sleep(0)
    await transport.stop_video_capture()

    with pytest.raises(TransportError, match="stopped"):
        await asyncio.wait_for(receiver, timeout=0.1)
    assert session._video_keyframe_assembler is None
    assert session._video_keyframe_future is None
    assert session.video_chunk_queue.empty()
    await transport.stop_video_capture()


async def test_raw_session_preserves_command_fragment_index_and_response_code():
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()
    body = b"result=ok;"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6013, len(body), 0x0100) + body
    packet = SimpleNamespace(
        _channel=FakeChannel.Command,
        _cmd_idx=0x1234,
        get_drw_payload=lambda: wire,
    )

    await session.handle_drw(packet)
    await session.handle_drw(packet)

    fragment = session.cgi_responses.get_nowait()
    assert fragment == _CgiResponseFragment(0x1234, wire, 0x6013)
    assert session.cgi_responses.empty()


@pytest.mark.parametrize(("max_fragments", "max_bytes"), [(2, 100), (3, 10)])
def test_raw_session_bounds_response_queue_and_dedup_cache(
    monkeypatch,
    max_fragments,
    max_bytes,
):
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._MAX_CGI_FRAGMENTS", max_fragments)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._MAX_CGI_BUFFER_BYTES", max_bytes)
    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": lambda data: data,
    }
    session = AioppppTransport._session_class(api)()

    for index in range(3):
        session._queue_cgi_response(_CgiResponseFragment(index, b"data", None))

    assert session.cgi_responses.qsize() == 2
    assert session.cgi_responses.queued_bytes == 8
    assert session.cgi_responses.overflowed
    assert len(session._recent_cgi_fragments) == 2
    assert session._recent_cgi_fragment_bytes == 8

    # Exact retransmission remains deduplicated after bounded eviction.
    session._queue_cgi_response(_CgiResponseFragment(2, b"data", None))
    assert session.cgi_responses.qsize() == 2


def test_livestream_uses_legacy_response_code():
    assert (
        AioppppTransport._expected_response_code("GET /livestream.cgi?streamid=10&substream=0&")
        == 0x6037
    )


@pytest.mark.parametrize(
    ("endpoint", "response_code"),
    [
        ("/get_record.cgi", 0x6006),
        ("/wifi_scan.cgi", 0x602A),
        ("/set_alarm.cgi", 0x600C),
        ("/set_users.cgi", 0x600E),
        ("/set_wifi.cgi", 0x6011),
        ("/camera_control.cgi", 0x6012),
        ("/set_datetime.cgi", 0x6013),
        ("/decoder_control.cgi", 0x6019),
        ("/reboot.cgi", 0x6027),
        ("/trans_cmd_string.cgi", 0x60D1),
    ],
)
def test_used_legacy_endpoints_have_evidence_backed_response_codes(
    endpoint,
    response_code,
):
    assert AioppppTransport._expected_response_code(f"GET {endpoint}?x=1&") == response_code


@pytest.mark.parametrize(
    "endpoint",
    [
        "/get_rtsp.cgi",
        "/set_rtsp.cgi",
        "/get_onvif.cgi",
        "/set_onvif.cgi",
        "/set_recordsch.cgi",
    ],
)
def test_unresolved_media_response_codes_are_not_asserted(endpoint):
    assert AioppppTransport._expected_response_code(f"GET {endpoint}?x=1&") is None


def test_raw_session_does_not_hide_malformed_packets():
    def reject_malformed(_data):
        raise ValueError("invalid packet")

    api = {
        "BinarySession": FakeBinarySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "PacketType": FakePacketType,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
        "parse_packet": reject_malformed,
    }
    session = AioppppTransport._session_class(api)()

    with pytest.raises(ValueError, match="invalid packet"):
        session.on_receive(b"\xf1\x43")


def observed_config() -> VStarcamConfig:
    return VStarcamConfig(
        auth_mode="observed",
        password="camera-password",
        account_id="1234",
        login_hash="device-password-record",
        login_token="account-auth-key",
    )


def lifecycle_session_class(*, ready: bool, fail_packet_on_start: bool = False):
    sessions = []

    class LifecycleSession:
        def __init__(self, descriptor, **_kwargs):
            self.dev = descriptor
            self.device_is_ready = asyncio.Event()
            self.video_chunk_queue = asyncio.Queue()
            self.transport = object()
            self.tasks = ()
            self.packet_failure = asyncio.Event()
            self.stopped = False
            self.on_disconnect = _kwargs.get("on_disconnect")
            sessions.append(self)

        async def stay_running(self):
            await asyncio.Event().wait()

        async def process_packets(self):
            await self.packet_failure.wait()
            raise RuntimeError("packet loop failed")

        def start(self):
            self.tasks = (
                asyncio.create_task(self.stay_running()),
                asyncio.create_task(self.process_packets()),
                asyncio.create_task(self.stay_running()),
            )
            if ready:
                self.device_is_ready.set()
            if fail_packet_on_start:
                self.packet_failure.set()

        async def send_close_pkt(self):
            return None

        def stop(self):
            self.stopped = True
            for task in self.tasks:
                task.cancel()
            self.transport = None

        def running_tasks(self):
            return self.tasks

    return LifecycleSession, sessions


def lifecycle_transport(monkeypatch, session_class):
    descriptor = SimpleNamespace(
        encryption="fake",
        addr="192.0.2.10",
        port=12345,
        is_json=False,
    )
    api = {
        "BinarySession": session_class,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
    }
    transport = AioppppTransport(VStarcamConfig(host=descriptor.addr, timeout=1))

    async def resolve(_api, _ports):
        return descriptor, api["ENC_METHODS"]["fake"]

    transport._resolve_descriptor = resolve
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._import_aiopppp", lambda: api)
    return transport


async def test_plaintext_discovery_precedes_configured_encrypted_fallback(monkeypatch):
    config = VStarcamConfig(
        host="192.0.2.10",
        device_id="VSTJ-000123-ABCDE",
        psk="vstarcam2018",
        timeout=2,
    )
    transport = AioppppTransport(config)
    plaintext = (object(), object())
    fallback_called = False

    async def no_encrypted_descriptor(_api, _ports):
        return None

    async def find_plaintext(_api, ports):
        assert ports == [config.discovery_port, config.udp_port]
        return plaintext

    def configured_fallback(_api):
        nonlocal fallback_called
        fallback_called = True
        return object(), object()

    transport._encrypted_descriptor = no_encrypted_descriptor
    transport._plaintext_descriptor = find_plaintext
    transport._configured_descriptor = configured_fallback

    assert (
        await transport._resolve_descriptor(
            {},
            [config.discovery_port, config.udp_port],
        )
        == plaintext
    )
    assert not fallback_called


async def test_configured_encrypted_fallback_remains_after_discovery_timeouts(monkeypatch):
    config = VStarcamConfig(
        host="192.0.2.10",
        device_id="VSTJ-000123-ABCDE",
        psk="vstarcam2018",
        timeout=2,
    )
    transport = AioppppTransport(config)
    configured = (object(), object())

    async def no_encrypted_descriptor(_api, _ports):
        return None

    async def no_plaintext_descriptor(_api, ports):
        assert ports == [32108, 12833]
        return None

    transport._encrypted_descriptor = no_encrypted_descriptor
    transport._plaintext_descriptor = no_plaintext_descriptor
    transport._configured_descriptor = lambda _api: configured

    assert await transport._resolve_descriptor({}, [32108, 12833]) == configured


async def test_plaintext_descriptor_uses_only_plaintext_packets(monkeypatch):
    config = VStarcamConfig(
        host="192.0.2.10",
        device_id="VSTJ-000123-ABCDE",
        source_address="192.0.2.44",
        timeout=8,
    )
    transport = AioppppTransport(config)
    observed = LanDiscoveryResult(
        host=config.host,
        port=24680,
        device_id=config.device_id,
        encrypted=False,
    )
    calls = []
    none = object()
    codec = (lambda data: data, lambda data: data)
    api = {
        "Encryption": SimpleNamespace(NONE=none),
        "DeviceID": lambda prefix, serial, suffix: (prefix, serial, suffix),
        "DeviceDescriptor": lambda device_id, host, port, **kwargs: SimpleNamespace(
            device_id=device_id,
            host=host,
            port=port,
            **kwargs,
        ),
        "ENC_METHODS": {none: codec},
    }

    def discover(*args, **kwargs):
        calls.append((args, kwargs))
        return [observed]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("vstarcamctl.transport_aiopppp.discover_with_seeds", discover)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.asyncio.to_thread", inline_to_thread)

    descriptor, selected_codec = await transport._plaintext_descriptor(api, [32108, 12833])

    assert descriptor.device_id == ("VSTJ", "000123", "ABCDE")
    assert descriptor.port == 24680
    assert selected_codec is codec
    assert calls == [
        (
            (config.host,),
            {
                "ports": [32108, 12833],
                "seeds": (),
                "timeout": 8,
                "expected_host": config.host,
                "expected_device_id": None,
                "stop_after_first": True,
                "source_address": config.source_address,
            },
        )
    ]


@pytest.mark.parametrize("kind", ["encrypted", "plaintext"])
async def test_discovery_descriptor_rejects_malformed_identity(kind, monkeypatch):
    observed = LanDiscoveryResult(
        "192.0.2.10", 24680, "malformed", encrypted=kind == "encrypted", psk="vstarcam2018"
    )
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp.discover_with_seeds",
        lambda *_args, **_kwargs: [observed],
    )
    transport = AioppppTransport(VStarcamConfig(host=observed.host))

    with pytest.raises(TransportError) as caught:
        await getattr(transport, f"_{kind}_descriptor")({}, [32108])

    assert str(caught.value) == f"{kind} discovery returned a malformed PPPP device ID"


async def test_directed_plaintext_discovery_rejects_reused_host_identity(monkeypatch):
    config = VStarcamConfig(
        host="192.0.2.10",
        device_id="VSTJ-000123-ABCDE",
        psk="vstarcam2019",
        timeout=2,
    )
    transport = AioppppTransport(config)
    observed = LanDiscoveryResult(
        host=config.host,
        port=24680,
        device_id="OTHER-000456-ZYXWV",
        encrypted=False,
    )
    fallback_called = False

    async def no_encrypted_descriptor(_api, _ports):
        return None

    def discover(*_args, **kwargs):
        assert kwargs["expected_device_id"] is None
        return [observed]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    def configured_fallback(_api):
        nonlocal fallback_called
        fallback_called = True
        return object(), object()

    transport._encrypted_descriptor = no_encrypted_descriptor
    transport._configured_descriptor = configured_fallback
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.discover_with_seeds", discover)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.asyncio.to_thread", inline_to_thread)

    with pytest.raises(TransportError, match="different camera identity") as caught:
        await transport._resolve_descriptor({}, [32108, 12833])

    assert not fallback_called
    assert config.device_id not in str(caught.value)
    assert observed.device_id not in str(caught.value)


async def test_directed_encrypted_discovery_rejects_reused_host_identity(monkeypatch):
    config = VStarcamConfig(
        host="192.0.2.10",
        device_id="VSTJ-000123-ABCDE",
        psk="vstarcam2018",
        timeout=2,
    )
    transport = AioppppTransport(config)
    observed = LanDiscoveryResult(
        host=config.host,
        port=24680,
        device_id="OTHER-000456-ZYXWV",
        encrypted=True,
        psk="vstarcam2018",
    )
    calls = []

    def discover(*args, **kwargs):
        calls.append((args, kwargs))
        return [observed]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("vstarcamctl.transport_aiopppp.discover_with_seeds", discover)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.asyncio.to_thread", inline_to_thread)
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._codec_for_seed",
        lambda _api, _seed: ("xor", (lambda data: data, lambda data: data)),
    )

    with pytest.raises(TransportError, match="different camera identity") as caught:
        await transport._encrypted_descriptor({}, [32108, 12833])

    assert config.device_id not in str(caught.value)
    assert observed.device_id not in str(caught.value)
    assert calls[0][1]["expected_device_id"] is None


@pytest.mark.parametrize(
    ("config", "result", "expected_seeds", "matching_profiles"),
    [
        (
            VStarcamConfig(host="192.0.2.10", psk="custom-seed", timeout=2),
            LanDiscoveryResult(
                "192.0.2.10",
                24680,
                "CUSTOM-000001-AAAAA",
                encrypted=True,
                psk="custom-seed",
            ),
            ("custom-seed",),
            False,
        ),
        (
            VStarcamConfig(
                host="192.0.2.10",
                device_id="VSTJ-000001-AAAAA",
                timeout=2,
            ),
            LanDiscoveryResult(
                "192.0.2.10",
                24680,
                "VSTJ-000001-AAAAA",
                encrypted=True,
                psk="vstarcam2019",
            ),
            ("vstarcam2019",),
            True,
        ),
        (
            VStarcamConfig(host="192.0.2.10", timeout=8),
            LanDiscoveryResult(
                "192.0.2.10",
                24680,
                "VSGS-000001-AAAAA",
                encrypted=True,
                psk="vstarcam2021",
            ),
            ("vstarcam2018", "vstarcam2019", "vstarcam2021"),
            True,
        ),
    ],
)
async def test_encrypted_transport_uses_explicit_mapped_or_auto_profiles(
    monkeypatch,
    config,
    result,
    expected_seeds,
    matching_profiles,
):
    calls = []

    def discover(*args, **kwargs):
        calls.append((args, kwargs))
        return [result]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("vstarcamctl.transport_aiopppp.discover_with_seeds", discover)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp.asyncio.to_thread", inline_to_thread)
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._codec_for_seed",
        lambda _api, seed: ("xor", ("decode", seed)),
    )
    api = {
        "DeviceID": lambda prefix, serial, suffix: (prefix, serial, suffix),
        "DeviceDescriptor": lambda device_id, host, port, **kwargs: SimpleNamespace(
            device_id=device_id,
            host=host,
            port=port,
            **kwargs,
        ),
    }

    descriptor, codec = await AioppppTransport(config)._encrypted_descriptor(api, [32108])

    assert descriptor.port == result.port
    assert codec == ("decode", result.psk)
    assert calls[0][1]["seeds"] == expected_seeds
    assert calls[0][1]["timeout"] == min(5.0, config.timeout)
    assert calls[0][1]["require_encrypted"] is True
    assert calls[0][1]["require_matching_profile"] is matching_profiles


def test_configured_fallback_derives_only_a_known_device_profile(monkeypatch):
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._codec_for_seed",
        lambda _api, seed: ("xor", ("decode", seed)),
    )
    api = {
        "DeviceID": lambda prefix, serial, suffix: (prefix, serial, suffix),
        "DeviceDescriptor": lambda device_id, host, port, **kwargs: SimpleNamespace(
            device_id=device_id,
            host=host,
            port=port,
            **kwargs,
        ),
    }
    known = AioppppTransport(
        VStarcamConfig(host="192.0.2.10", device_id="VSGM-000001-AAAAA")
    )._configured_descriptor(api)
    unknown = AioppppTransport(
        VStarcamConfig(host="192.0.2.10", device_id="OTHER-000001-AAAAA")
    )._configured_descriptor(api)

    assert known is not None
    assert known[1] == ("decode", "vstarcam2021")
    assert unknown is None


async def test_connect_rejects_session_that_fails_as_readiness_is_signalled(monkeypatch):
    sessions = []

    class FailedAtReadySession:
        def __init__(self, descriptor, **_kwargs):
            self.dev = descriptor
            self.device_is_ready = asyncio.Event()
            self.device_is_ready.set()
            self.packet_queue = asyncio.Queue()
            self.video_chunk_queue = asyncio.Queue()
            self.drw_waiters = {}
            self.transport = object()
            self.main_task = None
            self.stopped = False
            sessions.append(self)

        def start(self):
            self.main_task = asyncio.get_running_loop().create_future()
            self.main_task.set_exception(RuntimeError("session loop failed"))

        async def send_close_pkt(self):
            return None

        def stop(self):
            self.stopped = True

        def running_tasks(self):
            return [self.main_task]

    descriptor = SimpleNamespace(
        encryption="fake",
        addr="192.0.2.10",
        port=12345,
        is_json=False,
    )
    api = {
        "BinarySession": FailedAtReadySession,
        "BinaryCmdPkt": FakeBinaryCmdPkt,
        "Channel": FakeChannel,
        "ENC_METHODS": {"fake": (lambda data: data, lambda data: data)},
    }
    config = VStarcamConfig(host=descriptor.addr, timeout=1)
    transport = AioppppTransport(config)

    async def resolve(_api, _ports):
        return descriptor, api["ENC_METHODS"]["fake"]

    transport._resolve_descriptor = resolve
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._import_aiopppp", lambda: api)

    with pytest.raises(TransportError, match="failed before becoming ready"):
        await transport.connect()

    assert len(sessions) == 1
    assert sessions[0].stopped
    assert not transport.connected
    assert transport._session is None


async def test_connect_rejects_pre_ready_packet_task_failure(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=False, fail_packet_on_start=True)
    transport = lifecycle_transport(monkeypatch, session_class)

    with pytest.raises(TransportError, match="failed before becoming ready"):
        await transport.connect()

    assert sessions[0].stopped
    assert not transport.connected
    assert transport._session is None


async def test_late_session_task_failure_disconnects_and_stops_session(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()
    session = sessions[0]

    session.packet_failure.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not transport.connected
    assert session.stopped

    await transport.connect()
    assert len(sessions) == 2
    assert transport.connected
    await transport.close()


async def test_session_task_failure_wakes_video_receiver_immediately(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()
    await transport.start_video_capture()
    receiver = asyncio.create_task(transport.receive_video_keyframe(timeout=10))
    await asyncio.sleep(0)

    sessions[0].packet_failure.set()

    with pytest.raises(TransportError, match="session ended"):
        await asyncio.wait_for(receiver, timeout=0.1)
    assert not transport.connected


async def test_disconnect_callback_wakes_video_receiver_without_touching_replacement(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()
    old_session = sessions[0]
    await transport.start_video_capture()
    receiver = asyncio.create_task(transport.receive_video_keyframe(timeout=10))
    await asyncio.sleep(0)

    old_session.on_disconnect()

    with pytest.raises(TransportError, match="session ended"):
        await asyncio.wait_for(receiver, timeout=0.1)
    await transport.close()
    await transport.connect()
    replacement = sessions[1]
    old_session.on_disconnect()
    assert transport.connected
    assert transport._session is replacement
    assert replacement._video_keyframe_future is None
    await transport.close()


async def test_close_wakes_video_receiver_and_reconnect_starts_clean(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()
    await transport.start_video_capture()
    receiver = asyncio.create_task(transport.receive_video_keyframe(timeout=10))
    await asyncio.sleep(0)

    await transport.close()

    with pytest.raises(TransportError, match="session closed"):
        await asyncio.wait_for(receiver, timeout=0.1)
    await transport.connect()
    assert sessions[1]._video_keyframe_future is None
    await transport.start_video_capture()
    await transport.stop_video_capture()
    await transport.close()


async def test_session_lease_blocks_close_without_holding_request_lock(monkeypatch):
    session_class, _sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()

    async with transport.session_lease():
        close_task = asyncio.create_task(transport.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        await asyncio.wait_for(transport._request_lock.acquire(), timeout=0.1)
        transport._request_lock.release()

    await asyncio.wait_for(close_task, timeout=0.1)
    assert not transport.connected


async def test_old_session_task_failure_does_not_disconnect_replacement(monkeypatch):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    await transport.connect()
    old_session = sessions[0]
    replacement = object()
    transport._session = replacement
    transport._connected = True

    old_session.packet_failure.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert transport._session is replacement
    assert transport.connected
    assert not old_session.stopped

    transport._session = None
    transport._connected = False
    for task in old_session.running_tasks():
        task.cancel()
    await asyncio.gather(*old_session.running_tasks(), return_exceptions=True)


async def test_observed_authentication_runs_once_per_protected_session():
    transport = AioppppTransport(observed_config())
    transport._connected = True
    transport._session = object()
    calls = []

    async def fake_send(command, *, timeout, expected_code):
        calls.append((command, timeout, expected_code))
        if "/eye4_authentication.cgi" in command:
            return "result=0;eye4_auth=1;"
        return "result=0;"

    transport._send_framed = fake_send

    await transport.request("GET /get_params.cgi?x=1&", timeout=2.0)
    await transport.request("GET /trans_cmd_string.cgi?cmd=2109&command=0&light=0&", timeout=2.0)

    assert len(calls) == 4
    assert "/get_status.cgi?name=admin" in calls[0][0]
    assert calls[0][2] == 0x6001
    assert "/eye4_authentication.cgi" in calls[1][0]
    assert calls[1][2] == 0x7108
    assert "/get_params.cgi" in calls[2][0]
    assert "/trans_cmd_string.cgi" in calls[3][0]


@pytest.mark.parametrize("account_id", [None, "0"])
@pytest.mark.parametrize("dual_authentication", [None, 0, 1, 2])
async def test_no_account_authentication_orders_requests_and_repeats_after_reconnect(
    monkeypatch, account_id, dual_authentication
):
    session_class, sessions = lifecycle_session_class(ready=True)
    transport = lifecycle_transport(monkeypatch, session_class)
    transport.config = VStarcamConfig(
        host="192.0.2.10", password="camera-password", account_id=account_id, timeout=1
    )
    calls = []

    async def fake_send(command, *, timeout, expected_code):
        calls.append((transport._session, command, expected_code))
        if "/get_status.cgi" in command:
            return "result=0;" + (
                f"DualAuthentication={dual_authentication};"
                if dual_authentication is not None
                else ""
            )
        if "/eye4_authentication.cgi" in command:
            assert "loginAccount=0&loginToken=&" in command
            return "result=0;eye4_auth=1;"
        return "result=0;"

    transport._send_framed = fake_send
    expected = [("/get_status.cgi", 0x6001)]
    if dual_authentication in (1, 2):
        expected.append(("/eye4_authentication.cgi", 0x7108))
    expected.extend([("/get_params.cgi", 0x6002), ("/trans_cmd_string.cgi", 0x60D1)])

    for _ in range(2):
        await transport.connect()
        try:
            await transport.request("GET /get_params.cgi?x=1&", timeout=2)
            await transport.request(
                "GET /trans_cmd_string.cgi?cmd=2109&command=0&light=0&", timeout=2
            )
        finally:
            await transport.close()

    for session in sessions:
        assert [
            (command[4:].split("?", 1)[0], code)
            for called_session, command, code in calls
            if called_session is session
        ] == expected
    assert len(sessions) == 2


@pytest.mark.parametrize("auth_mode", ["basic", "observed"])
@pytest.mark.parametrize("result", ["-1", "-2", "-3", "'-3'"])
async def test_rejected_login_status_stops_before_authentication_or_protected_write(
    auth_mode, result
):
    config = observed_config() if auth_mode == "observed" else VStarcamConfig(password="secret")
    transport = AioppppTransport(config)
    transport._connected = True
    transport._session = object()
    calls = []

    async def fake_send(command, *, timeout, expected_code):
        calls.append(command)
        return f"var result={result}; var DualAuthentication=2;"

    transport._send_framed = fake_send
    with pytest.raises(TransportAuthenticationError):
        await transport.request("GET /trans_cmd_string.cgi?cmd=2109&light=1&", timeout=2)
    assert len(calls) == 1
    assert "/get_status.cgi?name=admin" in calls[0]
    assert not transport._authenticated


@pytest.mark.parametrize("auth_mode", ["basic", "observed"])
@pytest.mark.parametrize(
    "auth_response",
    [
        "result=0;",
        "result=0;eye4_auth=10;",
        "result=0;eye4_auth='1';",
        "result=0;eye4_auth=true;",
        "result=0;eye4_auth=1foo;",
        "result=0;eye4_auth=0;eye4_auth=1;",
        "result=0;eye4_auth=1;eye4_auth=0;",
    ],
)
async def test_authentication_requires_one_exact_integer_success(auth_mode, auth_response):
    config = observed_config() if auth_mode == "observed" else VStarcamConfig(password="secret")
    transport = AioppppTransport(config)
    transport._connected = True
    transport._session = object()
    calls = []

    async def fake_send(command, *, timeout, expected_code):
        calls.append(command)
        if "/eye4_authentication.cgi" in command:
            return auth_response
        return "result=0;DualAuthentication=2;"

    transport._send_framed = fake_send

    with pytest.raises(TransportError, match="rejected"):
        await transport.request("GET /trans_cmd_string.cgi?cmd=2109&light=1&", timeout=2.0)
    assert len(calls) == 2
    assert "/get_status.cgi?name=admin" in calls[0]
    assert "/eye4_authentication.cgi" in calls[1]
    assert not transport._authenticated


async def test_status_does_not_trigger_observed_authentication():
    transport = AioppppTransport(observed_config())
    transport._connected = True
    transport._session = object()
    calls = []

    async def fake_send(command, *, timeout, expected_code):
        calls.append(command)
        return "result=0;"

    transport._send_framed = fake_send

    await transport.request("GET /get_status.cgi?x=1&", timeout=2.0)

    assert len(calls) == 1
    assert "/get_status.cgi" in calls[0]


async def test_external_cancellation_is_not_translated_to_transport_error(monkeypatch):
    sent = asyncio.Event()

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = asyncio.Queue()
            self.drw_waiters = {}

        async def send(self, _packet):
            self.drw_waiters[0] = asyncio.get_running_loop().create_future()
            sent.set()

        async def _wait_ack(self, _index, *, timeout):
            await self.drw_waiters[0]

    api = {
        "DrwPkt": lambda *_args: object(),
        "Channel": FakeChannel,
    }
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: api,
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()
    task = asyncio.create_task(
        transport._send_framed("GET /get_params.cgi?x=1&", timeout=5, expected_code=0x6002)
    )
    await sent.wait()
    task.cancel()
    with pytest.raises(TransportCommandCancelledError, match="outcome unknown"):
        await task
    assert not transport._session.drw_waiters


async def test_framed_send_exception_cleans_registered_ack_waiter(monkeypatch):
    class Packet:
        def __init__(self, _channel, index, _payload):
            self._cmd_idx = index

    class Session:
        outgoing_command_idx = 4

        def __init__(self):
            self.cgi_responses = asyncio.Queue()
            self.drw_waiters = {}

        async def send(self, packet):
            self.drw_waiters[packet._cmd_idx] = asyncio.get_running_loop().create_future()
            raise RuntimeError("send failed")

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": Packet, "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()

    with pytest.raises(TransportError, match="command send failed") as caught:
        await transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=1,
            expected_code=0x6002,
        )
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not transport._session.drw_waiters


async def test_framed_ack_does_not_swallow_external_cancellation(monkeypatch):
    waiting = asyncio.Event()

    class Packet:
        def __init__(self, _channel, index, _payload):
            self._cmd_idx = index

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = asyncio.Queue()
            self.drw_waiters = {}

        async def send(self, packet):
            self.drw_waiters[packet._cmd_idx] = asyncio.get_running_loop().create_future()

        async def _wait_ack(self, index, *, timeout):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Model asyncio.wait_for choosing the concurrently completed
                # ACK and returning while the outer task remains cancelling.
                self.drw_waiters[index].set_result(object())
                asyncio.get_running_loop().call_soon(self.drw_waiters.pop, index, None)
                return

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": Packet, "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()
    task = asyncio.create_task(
        transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=1,
            expected_code=0x6002,
        )
    )
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not transport._session.drw_waiters


async def test_framed_response_does_not_swallow_external_cancellation(monkeypatch):
    waiting = asyncio.Event()
    body = b"result=ok;"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6002, len(body), 0x0100) + body

    class CancellationSwallowingQueue:
        overflowed = False

        @staticmethod
        def empty():
            return True

        async def get(self):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return _CgiResponseFragment(1, wire, 0x6002)

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = CancellationSwallowingQueue()

        async def send(self, _packet):
            return None

        async def _wait_ack(self, _index, *, timeout):
            return None

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": lambda *_args: object(), "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()
    transport._connected = True
    task = asyncio.create_task(
        transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=1,
            expected_code=0x6002,
        )
    )
    await waiting.wait()
    task.cancel()

    with pytest.raises(TransportCommandCancelledError, match="outcome unknown"):
        await task
    assert not transport.connected


@pytest.mark.parametrize(
    ("response_kind", "expected_error"),
    [
        ("none", TransportTimeoutError),
        ("auth-failure", TransportAuthenticationError),
        ("incomplete-auth-failure", TransportTimeoutError),
    ],
)
async def test_missing_expected_response_quarantines_session_before_late_response(
    monkeypatch,
    response_kind,
    expected_error,
):
    body = b"var result=-2;"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6001, len(body), 0x0100) + body

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = asyncio.Queue()
            # A queued status from before this request must not diagnose its failure.
            self.cgi_responses.put_nowait(_CgiResponseFragment(90, wire, 0x6001))
            self.stopped = False
            self.transport = SimpleNamespace(close=lambda: None)
            self.task = asyncio.create_task(asyncio.Event().wait())

        async def send(self, _packet):
            if response_kind != "none":
                payload = wire if response_kind == "auth-failure" else wire[:-1]
                self.cgi_responses.put_nowait(_CgiResponseFragment(91, payload, 0x6001))

        async def _wait_ack(self, _index, *, timeout):
            return None

        def stop(self):
            self.stopped = True
            self.task.cancel()
            self.transport.close()
            self.transport = None

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": lambda *_args: object(), "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True

    with pytest.raises(expected_error) as caught:
        await transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=0.001,
            expected_code=0x6002,
        )

    if response_kind == "auth-failure":
        assert "authentication failure (result=-2)" in str(caught.value)
    else:
        assert "acknowledged" in str(caught.value)
    session.cgi_responses.put_nowait(_CgiResponseFragment(1, b"late", 0x6002))
    assert session.stopped
    assert session.transport is None
    await asyncio.sleep(0)
    assert session.task.cancelled()
    assert not transport.connected
    with pytest.raises(TransportError, match="not connected"):
        await transport.request(
            "GET /get_params.cgi?x=1&",
            timeout=1,
        )
    assert session.cgi_responses.qsize() == 1


@pytest.mark.parametrize(
    ("expected_code", "late_body"),
    [
        (0x6002, b"var result=0;var status='late';"),
        (0x6002, b"var result=-2;"),
        (None, b"var result=-2;"),
    ],
    ids=["successful-status", "late-auth-failure", "unknown-code"],
)
async def test_framed_response_ignores_unsolicited_data_and_reorders_deduplicates_fragments(
    monkeypatch,
    expected_code,
    late_body,
):
    expected_body = b"var answer='ok';"
    expected_wire = (
        CGI_HEADER.pack(CGI_START_CODE, 0x6002, len(expected_body), 0x0100) + expected_body
    )
    late_wire = CGI_HEADER.pack(CGI_START_CODE, 0x6001, len(late_body), 0x0100) + late_body
    first, second = expected_wire[:12], expected_wire[12:]

    class Session:
        outgoing_command_idx = 7

        def __init__(self):
            self.cgi_responses = asyncio.Queue()

        async def send(self, _packet):
            # A periodic status arrives late, and the expected response's
            # second fragment arrives before its first. Retransmission repeats
            # the same DRW index and must not duplicate its payload.
            for fragment in (
                _CgiResponseFragment(90, late_wire, 0x6001),
                _CgiResponseFragment(91, b"result=0;", None),
                _CgiResponseFragment(92, b"var event='motion';", 0x6040, binary_command=True),
                _CgiResponseFragment(102, second, None),
                _CgiResponseFragment(102, second, None),
                _CgiResponseFragment(101, first, 0x6002),
            ):
                self.cgi_responses.put_nowait(fragment)

        async def _wait_ack(self, _index, *, timeout):
            assert timeout > 0

    api = {
        "DrwPkt": lambda *_args: object(),
        "Channel": FakeChannel,
    }
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: api,
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()

    response = await transport._send_framed(
        "GET /get_params.cgi?x=1&",
        timeout=1,
        expected_code=expected_code,
    )

    assert response == expected_body.decode()


async def test_framed_response_fails_closed_when_session_queue_overflows(
    monkeypatch,
):
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._MAX_CGI_BUFFER_BYTES", 10)

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = _CgiResponseQueue()

        async def send(self, _packet):
            for index in range(3):
                self.cgi_responses.put_nowait(_CgiResponseFragment(index, b"data", None))

        async def _wait_ack(self, _index, *, timeout):
            assert timeout > 0

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": lambda *_args: object(), "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()

    with pytest.raises(TransportError, match="safe CGI buffer"):
        await transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=1,
            expected_code=0x6002,
        )


@pytest.mark.parametrize(
    "body",
    [
        b"var result=0;",
        b"var result=-1;",
        b"var result='-2';",
        b"var result=-2.0;",
        b"var result=true;",
        b"var result=0;var result=-2;",
        b"var result=-2;var detail='unterminated;",
        b"not_result=-2;",
        b"var result=-2;var detail='other';",
    ],
    ids=[
        "success",
        "unconfirmed-code",
        "string",
        "float",
        "boolean",
        "duplicate",
        "malformed",
        "field-prefix",
        "extra-field",
    ],
)
def test_unrelated_status_needs_unambiguous_integer_auth_failure(body):
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6001, len(body), 0x0100) + body
    pending = {10: _CgiResponseFragment(10, wire, 0x6001)}

    assert AioppppTransport._take_complete_response(pending, expected_code=0x6002) == (
        False,
        "",
    )


@pytest.mark.parametrize("body", [b"result=-2", b"\x00 \r\nvar result = -2;\r\n\x00"])
def test_unrelated_status_recognizes_only_the_padded_auth_failure_assignment(body):
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6001, len(body), 0x0100) + body
    pending = {10: _CgiResponseFragment(10, wire, 0x6001)}

    with pytest.raises(TransportAuthenticationError):
        AioppppTransport._take_complete_response(pending, expected_code=0x6002)


def test_status_with_trailing_data_is_not_classified_as_auth_failure():
    body = b"var result=-2;"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6001, len(body), 0x0100) + body
    pending = {10: _CgiResponseFragment(10, wire + b"unclassified-trailing-data", 0x6001)}

    assert AioppppTransport._take_complete_response(pending, expected_code=0x6002) == (
        False,
        "",
    )


@pytest.mark.parametrize(("max_fragments", "max_bytes"), [(1, 100), (4096, 12)])
async def test_framed_response_bounds_per_request_reassembly(
    monkeypatch,
    max_fragments,
    max_bytes,
):
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._MAX_CGI_FRAGMENTS", max_fragments)
    monkeypatch.setattr("vstarcamctl.transport_aiopppp._MAX_CGI_BUFFER_BYTES", max_bytes)

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = asyncio.Queue()

        async def send(self, _packet):
            self.cgi_responses.put_nowait(_CgiResponseFragment(1, b"12345678", None))
            self.cgi_responses.put_nowait(_CgiResponseFragment(2, b"abcdefgh", None))

        async def _wait_ack(self, _index, *, timeout):
            assert timeout > 0

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"DrwPkt": lambda *_args: object(), "Channel": FakeChannel},
    )
    transport = AioppppTransport(observed_config())
    transport._session = Session()

    with pytest.raises(TransportError, match="safe CGI reassembly buffer"):
        await transport._send_framed(
            "GET /get_params.cgi?x=1&",
            timeout=1,
            expected_code=0x6002,
        )


def test_incomplete_response_does_not_skip_a_missing_fragment():
    body = b"var answer='complete';"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6002, len(body), 0x0100) + body
    pending = {
        10: _CgiResponseFragment(10, wire[:12], 0x6002),
        12: _CgiResponseFragment(12, wire[16:], None),
    }

    complete, _response = AioppppTransport._take_complete_response(
        pending,
        expected_code=0x6002,
    )
    assert not complete

    pending[11] = _CgiResponseFragment(11, wire[12:16], None)
    complete, response = AioppppTransport._take_complete_response(
        pending,
        expected_code=0x6002,
    )
    assert complete
    assert response == body.decode()


def test_response_reassembly_handles_a_split_cgi_header():
    body = b"result=ok;"
    wire = CGI_HEADER.pack(CGI_START_CODE, 0x6013, len(body), 0x0100) + body
    pending = {
        0xFFFF: _CgiResponseFragment(0xFFFF, wire[:1], None),
        0: _CgiResponseFragment(0, wire[1:6], None),
        1: _CgiResponseFragment(1, wire[6:], None),
    }

    complete, response = AioppppTransport._take_complete_response(
        pending,
        expected_code=0x6013,
    )

    assert complete
    assert response == body.decode()


async def test_requests_are_serialized_per_transport():
    transport = AioppppTransport(VStarcamConfig(password="camera-password"))
    transport._connected = True
    transport._session = object()
    active = 0
    maximum_active = 0

    async def fake_send(command, *, timeout, expected_code):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return "result=0;"

    transport._send_framed = fake_send
    await asyncio.gather(
        transport.request("GET /get_params.cgi?x=1&", timeout=2),
        transport.request("GET /get_params.cgi?x=2&", timeout=2),
    )
    assert maximum_active == 1


async def test_numeric_channel_data_uses_drw_header_and_waits_for_ack(monkeypatch):
    class Packet:
        def __init__(self, packet_type, payload):
            self.type = packet_type
            self.payload = payload

    class Session:
        outgoing_command_idx = 0x1234

        def __init__(self):
            self.sent = []
            self.acks = []

        async def send(self, packet):
            self.sent.append(packet)

        async def _wait_ack(self, index, *, timeout):
            self.acks.append((index, timeout))

    api = {"Packet": Packet, "PacketType": FakePacketType}
    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: api,
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True

    await transport.send_channel_parts(3, (b"voice",), timeout=2.5)

    assert len(session.sent) == 1
    packet = session.sent[0]
    assert packet.type is FakePacketType.Drw
    assert packet.payload[:4] == struct.pack(">BBH", 0xD1, 3, 0x1234)
    assert packet.payload[4:] == b"voice"
    assert packet._cmd_idx == 0x1234
    assert session.acks[0][0] == 0x1234
    assert 0 < session.acks[0][1] <= 2.5


@pytest.mark.parametrize("timeout", [True, "1", float("nan"), float("inf"), 0, -1])
async def test_numeric_channel_timeout_must_be_a_finite_positive_number(timeout):
    transport = AioppppTransport(observed_config())

    with pytest.raises(TransportError, match="finite positive"):
        await transport.send_channel_parts(3, (b"voice",), timeout=timeout)


async def test_channel_parts_are_separate_drw_packets_and_cannot_interleave(monkeypatch):
    first_ack = asyncio.Event()

    class Packet:
        def __init__(self, packet_type, payload):
            self.type = packet_type
            self.payload = payload

    class Session:
        outgoing_command_idx = 10

        def __init__(self):
            self.sent = []

        async def send(self, packet):
            self.sent.append(packet)

        async def _wait_ack(self, index, *, timeout):
            assert 0 < timeout <= 2
            if index == 10:
                await first_ack.wait()

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"Packet": Packet, "PacketType": FakePacketType},
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True

    parts_task = asyncio.create_task(
        transport.send_channel_parts(3, (b"header", b"payload"), timeout=2)
    )
    while not session.sent:
        await asyncio.sleep(0)
    other_task = asyncio.create_task(transport.send_channel_parts(3, (b"other",), timeout=2))
    await asyncio.sleep(0)
    first_ack.set()
    await asyncio.gather(parts_task, other_task)

    assert [packet.payload[4:] for packet in session.sent] == [
        b"header",
        b"payload",
        b"other",
    ]
    assert [packet._cmd_idx for packet in session.sent] == [10, 11, 12]


async def test_channel_parts_share_one_batch_deadline(monkeypatch):
    class Packet:
        def __init__(self, packet_type, payload):
            self.type = packet_type
            self.payload = payload

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.sent = []

        async def send(self, packet):
            self.sent.append(packet)

        async def _wait_ack(self, _index, *, timeout):
            assert timeout > 0
            await asyncio.sleep(0.02)

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"Packet": Packet, "PacketType": FakePacketType},
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True

    with pytest.raises(TransportTimeoutError, match="channel 3"):
        await transport.send_channel_parts(3, (b"header", b"payload"), timeout=0.01)
    assert [packet.payload[4:] for packet in session.sent] == [b"header"]


async def test_channel_send_exception_cleans_registered_ack_waiter(monkeypatch):
    class Packet:
        def __init__(self, packet_type, payload):
            self.type = packet_type
            self.payload = payload

    class Session:
        outgoing_command_idx = 8

        def __init__(self):
            self.drw_waiters = {}

        async def send(self, packet):
            self.drw_waiters[packet._cmd_idx] = asyncio.get_running_loop().create_future()
            raise RuntimeError("channel send failed")

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"Packet": Packet, "PacketType": FakePacketType},
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True

    with pytest.raises(TransportError, match="channel 3 send failed") as caught:
        await transport.send_channel_parts(3, (b"voice",), timeout=1)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not session.drw_waiters


async def test_channel_ack_does_not_continue_batch_after_external_cancellation(
    monkeypatch,
):
    waiting = asyncio.Event()

    class Packet:
        def __init__(self, packet_type, payload):
            self.type = packet_type
            self.payload = payload

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.sent = []
            self.drw_waiters = {}

        async def send(self, packet):
            self.sent.append(packet)
            self.drw_waiters[packet._cmd_idx] = asyncio.get_running_loop().create_future()

        async def _wait_ack(self, index, *, timeout):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.drw_waiters[index].set_result(object())
                asyncio.get_running_loop().call_soon(self.drw_waiters.pop, index, None)
                return

    monkeypatch.setattr(
        "vstarcamctl.transport_aiopppp._import_aiopppp",
        lambda: {"Packet": Packet, "PacketType": FakePacketType},
    )
    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True
    task = asyncio.create_task(transport.send_channel_parts(3, (b"header", b"payload"), timeout=1))
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert [packet.payload[4:] for packet in session.sent] == [b"header"]
    assert not session.drw_waiters


async def test_close_propagates_external_cancellation():
    started = asyncio.Event()

    class Session:
        def __init__(self):
            self.transport = object()
            self.stopped = False

        async def send_close_pkt(self):
            started.set()
            await asyncio.Event().wait()

        def stop(self):
            self.stopped = True

        def running_tasks(self):
            return []

    transport = AioppppTransport(observed_config())
    session = Session()
    transport._session = session
    transport._connected = True
    task = asyncio.create_task(transport.close())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.stopped
    assert not transport.connected
