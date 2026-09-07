"""Offline contract checks for the pinned aiopppp adapter dependency."""

from __future__ import annotations

import asyncio

import pytest

from vstarcamctl.config import VStarcamConfig
from vstarcamctl.errors import TransportTimeoutError
from vstarcamctl.transport_aiopppp import AioppppTransport, _import_aiopppp


def test_pinned_aiopppp_exposes_the_adapter_contract():
    api = _import_aiopppp()

    assert {
        "BinaryCmdPkt",
        "BinarySession",
        "Channel",
        "DeviceDescriptor",
        "DeviceID",
        "DrwPkt",
        "ENC_METHODS",
        "Encryption",
        "Packet",
        "PacketType",
        "SessionUDPProtocol",
        "make_p2palive_pkt",
        "make_punch_pkt",
        "parse_packet",
    } <= api.keys()

    descriptor = api["DeviceDescriptor"](
        api["DeviceID"]("TEST", 1, "ABCDE"),
        "192.0.2.1",
        32108,
        api["Encryption"].XOR1,
    )
    session_class = AioppppTransport._session_class(api)
    session = session_class(descriptor, lambda: None)

    assert hasattr(session, "_wait_ack")
    assert isinstance(session.drw_waiters, dict)
    packet = api["DrwPkt"](api["Channel"].Command.value, 7, b"payload")
    assert packet._cmd_idx == 7
    assert packet.get_drw_payload() == b"payload"


@pytest.mark.parametrize("send_ready", [False, True], ids=["no-ready", "repeated-ready"])
async def test_pinned_session_requires_ready_but_does_not_wait_for_retransmits_to_stop(
    monkeypatch, send_ready
):
    api = _import_aiopppp()
    packet_type = api["PacketType"]
    descriptor = api["DeviceDescriptor"](
        api["DeviceID"]("TEST", 1, "ABCDE"),
        "192.0.2.1",
        32108,
        api["Encryption"].XOR1,
    )
    transport = AioppppTransport(VStarcamConfig(host=descriptor.addr, timeout=0.8))
    endpoint_ready = asyncio.Event()
    alive_ack = asyncio.Event()

    class FakeUDP:
        protocol = None
        closed = False

        def __init__(self):
            self.sent_types = []

        def sendto(self, data, _address):
            self.sent_types.append(packet_type(data[1]))
            if data[1] == packet_type.P2PAliveAck.value:
                alive_ack.set()

        def close(self):
            self.closed = True

        def receive(self, packet):
            self.protocol.datagram_received(bytes(packet), (descriptor.addr, descriptor.port))

    udp = FakeUDP()

    async def create_endpoint(factory, **kwargs):
        assert kwargs == {"remote_addr": (descriptor.addr, descriptor.port)}
        udp.protocol = factory()
        udp.protocol.connection_made(udp)
        endpoint_ready.set()
        return udp, udp.protocol

    async def resolve(_api, _ports):
        return descriptor, (lambda data: data, lambda data: data)

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", create_endpoint)
    monkeypatch.setattr(transport, "_resolve_descriptor", resolve)
    connect = asyncio.create_task(transport.connect())
    producer = None
    try:
        await asyncio.wait_for(endpoint_ready.wait(), timeout=1)
        udp.receive(api["make_p2palive_pkt"]())
        await asyncio.wait_for(alive_ack.wait(), timeout=1)
        assert not transport.connected

        if not send_ready:
            with pytest.raises(TransportTimeoutError, match="did not become ready"):
                await connect
            assert packet_type.DrwAck not in udp.sent_types
            return

        ready = api["make_punch_pkt"](descriptor.dev_id)
        ready.type = packet_type.P2pRdy

        async def repeat_ready():
            while True:
                udp.receive(ready)
                await asyncio.sleep(0.02)

        producer = asyncio.create_task(repeat_ready())
        await asyncio.wait_for(connect, timeout=1)
        assert transport.connected
        assert not producer.done()

        # Keep retransmitting beyond a full debounce period after setup. The
        # readiness sentinel must be sent once, and keepalive handling must work.
        alive_ack.clear()
        udp.receive(api["make_p2palive_pkt"]())
        await asyncio.wait_for(alive_ack.wait(), timeout=1)
        await asyncio.sleep(0.25)
        assert udp.sent_types.count(packet_type.DrwAck) == 1
        assert udp.sent_types.count(packet_type.P2PAlive) == 3
        assert udp.sent_types.count(packet_type.P2PAliveAck) == 2
    finally:
        if producer is not None:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        if not connect.done():
            connect.cancel()
        await asyncio.gather(connect, return_exceptions=True)
        await transport.close()
        assert udp.closed
