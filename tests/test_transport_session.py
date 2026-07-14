import asyncio
import struct
from enum import Enum
from types import SimpleNamespace

import pytest

from vstarcamctl.config import VStarcamConfig
from vstarcamctl.transport_aiopppp import AioppppTransport


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
            self.video_chunk_queue.put_nowait(drw_pkt)


class FakeBinaryCmdPkt:
    pass


class FakeChannel:
    Command = SimpleNamespace(value=0)
    Video = SimpleNamespace(value=1)


class FakePacketType(Enum):
    DrwAck = 0xD1
    Drw = 0xD0


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

    await session.handle_drw(SimpleNamespace(_channel=FakeChannel.Video))

    assert session.video_chunk_queue.empty()


def test_livestream_uses_legacy_response_code():
    assert (
        AioppppTransport._expected_response_code("GET /livestream.cgi?streamid=10&substream=0&")
        == 0x6037
    )


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


@pytest.mark.asyncio
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

    assert len(calls) == 3
    assert "/eye4_authentication.cgi" in calls[0][0]
    assert "/get_params.cgi" in calls[1][0]
    assert "/trans_cmd_string.cgi" in calls[2][0]


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_external_cancellation_is_not_translated_to_transport_error(monkeypatch):
    sent = asyncio.Event()

    class Session:
        outgoing_command_idx = 0

        def __init__(self):
            self.cgi_responses = asyncio.Queue()

        async def send(self, _packet):
            sent.set()

        async def _wait_ack(self, _index, *, timeout):
            await asyncio.Event().wait()

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
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_requests_are_serialized_per_transport():
    transport = AioppppTransport(observed_config())
    transport.config.auth_mode = "basic"
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


@pytest.mark.asyncio
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

    await transport.send_channel_data(3, b"voice", timeout=2.5)

    assert len(session.sent) == 1
    packet = session.sent[0]
    assert packet.type is FakePacketType.Drw
    assert packet.payload[:4] == struct.pack(">BBH", 0xD1, 3, 0x1234)
    assert packet.payload[4:] == b"voice"
    assert packet._cmd_idx == 0x1234
    assert session.acks == [(0x1234, 2.5)]


@pytest.mark.asyncio
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
