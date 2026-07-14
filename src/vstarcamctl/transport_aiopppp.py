"""Raw command-channel adapter for aiopppp with configurable VStarcam PSK."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from typing import Any

from .cgi import format_eye4_auth_request
from .config import VStarcamConfig
from .discovery_encrypted import discover_with_seed
from .errors import TransportError, TransportTimeoutError, TransportUnavailableError
from .framing import CgiFrameError, decode_cgi_frame, encode_cgi_request, expected_frame_size
from .pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet
from .secrets import mask_identifier
from .transport import DiscoveredCamera

LOGGER = logging.getLogger(__name__)


def _import_aiopppp() -> dict[str, Any]:
    try:
        from aiopppp import Discovery
        from aiopppp.const import PacketType
        from aiopppp.encrypt import ENC_METHODS
        from aiopppp.packets import (
            BinaryCmdPkt,
            DrwPkt,
            Packet,
            make_p2palive_pkt,
            make_punch_pkt,
            parse_packet,
        )
        from aiopppp.session import BinarySession
        from aiopppp.types import Channel, DeviceDescriptor, DeviceID, Encryption
    except ImportError as exc:
        raise TransportUnavailableError(
            "aiopppp is not installed; run: python -m pip install -e '.[transport]'"
        ) from exc
    return {
        "Discovery": Discovery,
        "BinaryCmdPkt": BinaryCmdPkt,
        "DrwPkt": DrwPkt,
        "Packet": Packet,
        "PacketType": PacketType,
        "make_p2palive_pkt": make_p2palive_pkt,
        "make_punch_pkt": make_punch_pkt,
        "parse_packet": parse_packet,
        "BinarySession": BinarySession,
        "Channel": Channel,
        "DeviceDescriptor": DeviceDescriptor,
        "DeviceID": DeviceID,
        "Encryption": Encryption,
        "ENC_METHODS": ENC_METHODS,
    }


def _configure_seed(api: dict[str, Any], seed: str):
    key = derive_effective_key(seed)
    encryption = api["Encryption"].XOR1
    api["ENC_METHODS"][encryption] = (
        lambda data: decrypt_packet(data, key),
        lambda data: encrypt_packet(data, key),
    )
    return encryption


async def _find_descriptor(
    host: str,
    port: int,
    timeout: float,
    *,
    expected_vuid: str | None = None,
):
    api = _import_aiopppp()
    loop = asyncio.get_running_loop()
    expected_address = await asyncio.to_thread(socket.gethostbyname, host)
    found = loop.create_future()

    def on_found(device) -> None:
        if getattr(device, "addr", None) != expected_address:
            return
        if expected_vuid is not None and _device_id(device) != expected_vuid:
            return
        if not found.done():
            found.set_result(device)

    discovery = api["Discovery"](remote_addr=host, remote_port=port)
    task = asyncio.create_task(discovery.discover(on_found, period=1))
    try:
        return await asyncio.wait_for(found, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TransportTimeoutError(f"no aiopppp discovery response on UDP port {port}") from exc
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _device_id(device) -> str:
    dev_id = getattr(device, "dev_id", "")
    return getattr(dev_id, "dev_id", None) or str(dev_id)


async def discover_cameras(
    *, remote_addr: str = "255.255.255.255", remote_port: int = 32108, timeout: float = 3.0
) -> list[DiscoveredCamera]:
    """Run aiopppp's plaintext/legacy discovery."""

    api = _import_aiopppp()
    found: dict[tuple[str, int, str], DiscoveredCamera] = {}

    def on_found(device) -> None:
        item = DiscoveredCamera(
            host=device.addr,
            port=device.port,
            vuid=_device_id(device),
            protocol="json" if device.is_json else "binary",
            encryption=getattr(device.encryption, "name", str(device.encryption)),
        )
        found[(item.host, item.port, item.vuid)] = item

    discovery = api["Discovery"](remote_addr=remote_addr, remote_port=remote_port)
    task = asyncio.create_task(discovery.discover(on_found, period=1))
    try:
        await asyncio.sleep(timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return sorted(found.values(), key=lambda item: (item.host, item.port, item.vuid))


class AioppppTransport:
    name = "aiopppp-legacy-cgi-psk"
    RESPONSE_CODES = {
        "/get_status.cgi": 0x6001,
        "/get_params.cgi": 0x6002,
        "/get_camera_params.cgi": 0x6003,
        "/audiostream.cgi": 0x6031,
        "/livestream.cgi": 0x6037,
        "/decoder_control.cgi": 0x6019,
        "/trans_cmd_string.cgi": 0x60D1,
        "/eye4_authentication.cgi": 0x7108,
    }

    def __init__(self, config: VStarcamConfig):
        self.config = config
        self._session = None
        self._connected = False
        self._observed_authenticated = False
        self._lifecycle_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    @staticmethod
    def _session_class(api):
        binary_session = api["BinarySession"]
        binary_cmd_pkt = api["BinaryCmdPkt"]
        channel = api["Channel"]

        class RawCgiSession(binary_session):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cgi_responses: asyncio.Queue[bytes] = asyncio.Queue()

            def on_receive(self, data):
                # aiopppp 0.2.3 converts the packet type to its closed Enum
                # without handling newer/unsupported types. Some VE cameras
                # repeatedly send type 0x43, which otherwise escapes from the
                # datagram callback as ``ValueError: 67 is not a valid
                # PacketType``. Unknown packets cannot be interpreted by this
                # version of aiopppp, so discard them without disturbing the
                # session; malformed and known packets retain its normal
                # parsing behaviour.
                decoded = api["ENC_METHODS"][self.dev.encryption][0](data)
                if len(decoded) >= 4 and decoded[:1] == b"\xf1":
                    try:
                        api["PacketType"](decoded[1])
                    except ValueError:
                        LOGGER.debug(
                            "ignored unsupported PPPP packet type=0x%02x",
                            decoded[1],
                        )
                        return
                pkt = api["parse_packet"](decoded)
                LOGGER.debug(
                    "received PPPP packet type=%s payload_len=%d",
                    pkt.type,
                    len(pkt.get_payload()),
                )
                self.packet_queue.put_nowait(pkt)

            async def setup_device(self):
                # CGI requests carry their own auth suffix. Do not issue
                # aiopppp's model-specific binary login/status sequence. The
                # Send three immediate keepalives after the
                # camera's P2pRdy and before its first CGI request.
                for _ in range(3):
                    await self.send(api["make_p2palive_pkt"]())
                # Follow those keepalives with this sentinel readiness
                # ACK. aiopppp 0.2.3 does not model it, but both authorized
                # marker immediately before the first CGI frame.
                await self.send(
                    api["Packet"](api["PacketType"].DrwAck, b"\xd1\x07\x00\x01\xaa\xaa")
                )
                self.device_is_ready.set()

            async def send_initial_packets(self):
                # Send three immediate PunchPkt packets before entering the session.
                # copies and two more around 90 ms later, then wait for the
                # camera's P2pRdy. BinarySession's premature outgoing P2pRdy is
                # deliberately omitted for this model.
                packet = api["make_punch_pkt"](self.dev.dev_id)
                for _ in range(3):
                    await self.send(packet)
                await asyncio.sleep(0.09)
                for _ in range(2):
                    await self.send(packet)

            async def handle_drw(self, drw_pkt):
                await super().handle_drw(drw_pkt)
                if drw_pkt._channel == channel.Video:
                    # This adapter uses RTSP for decoded media. A same-session
                    # PPPP livestream is only a talk prerequisite, so discard
                    # its already-ACKed video chunks instead of growing the
                    # aiopppp queue for the duration of talk.
                    while not self.video_chunk_queue.empty():
                        self.video_chunk_queue.get_nowait()
                    return
                if drw_pkt._channel != channel.Command:
                    return
                if isinstance(drw_pkt, binary_cmd_pkt):
                    command_value = getattr(drw_pkt.command, "value", 0)
                    if 0x6000 <= command_value <= 0x60FF:
                        self.cgi_responses.put_nowait(drw_pkt.cmd_payload)
                else:
                    self.cgi_responses.put_nowait(drw_pkt.get_drw_payload())

        return RawCgiSession

    async def _encrypted_descriptor(self, api, ports):
        if not self.config.psk or not self.config.host:
            return None
        encryption = _configure_seed(api, self.config.psk)
        is_broadcast = self.config.host == "255.255.255.255"
        if is_broadcast and not self.config.vuid:
            raise TransportError("broadcast camera connection requires a configured VUID")
        results = await asyncio.to_thread(
            discover_with_seed,
            self.config.host,
            ports=ports,
            seed=self.config.psk,
            timeout=min(5.0, self.config.timeout),
            expected_host=None if is_broadcast else self.config.host,
            expected_vuid=self.config.vuid,
            require_encrypted=True,
            stop_after_first=True,
        )
        if not results:
            return None
        result = results[0]
        try:
            prefix, serial, suffix = result.vuid.split("-", 2)
        except ValueError as exc:
            raise TransportError("encrypted discovery returned a malformed PPPP UID") from exc
        LOGGER.debug(
            "encrypted PPPP discovery port=%d vuid=%s",
            result.port,
            mask_identifier(result.vuid),
        )
        return api["DeviceDescriptor"](
            api["DeviceID"](prefix, serial, suffix),
            result.host,
            result.port,
            encryption=encryption,
            is_json=False,
        )

    def _configured_descriptor(self, api):
        if not self.config.vuid or not self.config.host or not self.config.psk:
            return None
        try:
            prefix, serial, suffix = self.config.vuid.split("-", 2)
        except ValueError as exc:
            raise TransportError("configured PPPP UID must use PREFIX-SERIAL-SUFFIX form") from exc
        encryption = _configure_seed(api, self.config.psk)
        LOGGER.debug(
            "using configured PPPP fallback port=%d vuid=%s",
            self.config.udp_port,
            mask_identifier(self.config.vuid),
        )
        return api["DeviceDescriptor"](
            api["DeviceID"](prefix, serial, suffix),
            self.config.host,
            self.config.udp_port,
            encryption=encryption,
            is_json=False,
        )

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            await self._connect_unlocked()

    async def _connect_unlocked(self) -> None:
        if self.connected:
            return
        self._observed_authenticated = False
        if not self.config.host:
            raise TransportError("host is required for aiopppp transport")
        api = _import_aiopppp()
        ports = list(dict.fromkeys((self.config.discovery_port, self.config.udp_port)))
        descriptor = await self._encrypted_descriptor(api, ports)
        if descriptor is None:
            descriptor = self._configured_descriptor(api)
        if descriptor is None:
            failures = []
            per_port_timeout = self.config.timeout / len(ports)
            for port in ports:
                try:
                    descriptor = await _find_descriptor(
                        self.config.host,
                        port,
                        per_port_timeout,
                        expected_vuid=self.config.vuid,
                    )
                    break
                except TransportTimeoutError as exc:
                    failures.append(str(exc))
            if descriptor is None:
                raise TransportTimeoutError(
                    "camera did not answer PPPP discovery on configured ports: "
                    + "; ".join(failures)
                )
        if descriptor.is_json:
            raise TransportUnavailableError(
                "camera answered with aiopppp JSON protocol; raw VStarcam CGI framing is unavailable"
            )
        if self.config.udp_port != descriptor.port:
            LOGGER.debug(
                "configured UDP port %d differs from discovered session port %d; using discovered port",
                self.config.udp_port,
                descriptor.port,
            )
        session_class = self._session_class(api)
        session = session_class(
            descriptor,
            on_disconnect=lambda *_: self._mark_disconnected(),
            login=self.config.username,
            password=self.config.password or "",
        )
        self._session = session
        session.start()
        ready_waiter = asyncio.create_task(session.device_is_ready.wait())
        try:
            main_task = getattr(session, "main_task", None)
            if isinstance(main_task, asyncio.Future):
                done, _pending = await asyncio.wait(
                    {ready_waiter, main_task},
                    timeout=self.config.timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise asyncio.TimeoutError
                if main_task in done and not ready_waiter.done():
                    if main_task.cancelled():
                        raise TransportError("PPPP session ended before becoming ready")
                    failure = main_task.exception()
                    if failure is not None:
                        raise TransportError(
                            "PPPP session failed before becoming ready"
                        ) from failure
                    raise TransportError("PPPP session ended before becoming ready")
            else:
                await asyncio.wait_for(ready_waiter, timeout=self.config.timeout)
        except asyncio.TimeoutError as exc:
            await self._close_unlocked()
            raise TransportTimeoutError("PPPP session opened but did not become ready") from exc
        except BaseException:
            await self._close_unlocked()
            raise
        finally:
            if not ready_waiter.done():
                ready_waiter.cancel()
        if self._session is not session or getattr(session, "transport", None) is None:
            await self._close_unlocked()
            raise TransportError("PPPP session became unavailable during startup")
        self._connected = True
        LOGGER.debug("PPPP session ready remote_port=%d", descriptor.port)

    def _mark_disconnected(self) -> None:
        self._connected = False

    async def close(self) -> None:
        async with self._request_lock:
            async with self._lifecycle_lock:
                await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        session = self._session
        self._session = None
        self._connected = False
        self._observed_authenticated = False
        if session is None:
            return

        cancellation: asyncio.CancelledError | None = None
        try:
            if session.transport is not None:
                await session.send_close_pkt()
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            LOGGER.debug("failed to send PPPP close packet", exc_info=True)

        try:
            session.stop()
        except (RuntimeError, asyncio.CancelledError):
            # aiopppp refuses stop() while its handshake state is still
            # DISCONNECTED. Cancel and close those resources explicitly so a
            # failed attempt does not leave a camera session slot occupied.
            for task in session.running_tasks():
                task.cancel()
            if session.transport is not None:
                session.transport.close()
                session.transport = None
        tasks = session.running_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if cancellation is not None:
            raise cancellation

    @staticmethod
    def _decode_response(chunks: list[bytes]) -> str:
        payload = b"".join(chunks)
        try:
            frame = decode_cgi_frame(payload)
        except CgiFrameError:
            # Retain compatibility with newer binary-command variants.
            pass
        else:
            payload = frame.payload
        positions = [pos for marker in (b"result", b"var ") if (pos := payload.find(marker)) >= 0]
        if positions:
            payload = payload[min(positions) :]
        return payload.decode("utf-8", errors="replace").strip("\x00\r\n ")

    @classmethod
    def _expected_response_code(cls, command: str) -> int | None:
        endpoint = command[4:].split("?", 1)[0]
        return cls.RESPONSE_CODES.get(endpoint)

    async def _send_framed(
        self,
        command: str,
        *,
        timeout: float,
        expected_code: int | None,
    ) -> str:
        session = self._session
        if session is None:
            raise TransportError("aiopppp transport is not connected")
        while not session.cgi_responses.empty():
            session.cgi_responses.get_nowait()
        api = _import_aiopppp()
        index = session.outgoing_command_idx
        session.outgoing_command_idx = (index + 1) & 0xFFFF
        packet = api["DrwPkt"](
            api["Channel"].Command.value,
            index,
            encode_cgi_request(command),
        )
        deadline = asyncio.get_running_loop().time() + timeout
        await session.send(packet)
        try:
            # Calling the private primitive avoids aiopppp re-raising a cancelled
            # session task from its generic error-check wrapper during disconnect.
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await session._wait_ack(index, timeout=remaining)
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            raise TransportError("camera closed the PPPP session before acknowledging CGI") from exc
        except asyncio.TimeoutError as exc:
            raise TransportTimeoutError(
                "camera did not acknowledge the framed CGI request"
            ) from exc
        while True:
            chunks: list[bytes] = []
            payload = bytearray()
            target_size = None
            while target_size is None or len(payload) < target_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TransportTimeoutError(
                        "PPPP command was acknowledged but its CGI response was incomplete"
                    )
                try:
                    chunk = await asyncio.wait_for(session.cgi_responses.get(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise TransportTimeoutError(
                        "PPPP command was acknowledged but no complete CGI response arrived"
                    ) from exc
                chunks.append(chunk)
                payload.extend(chunk)
                if target_size is None:
                    try:
                        target_size = expected_frame_size(payload)
                    except CgiFrameError:
                        return self._decode_response(chunks)

            try:
                frame = decode_cgi_frame(bytes(payload))
            except CgiFrameError:
                return self._decode_response(chunks)
            if expected_code is None or frame.command_code == expected_code:
                return self._decode_response(chunks)
            LOGGER.debug(
                "ignored unrelated CGI response code=0x%04x while waiting for 0x%04x",
                frame.command_code,
                expected_code,
            )

    async def request(self, command: str, *, timeout: float) -> str:
        async with self._request_lock:
            return await self._request_unlocked(command, timeout=timeout)

    async def send_channel_data(self, channel: int, payload: bytes, *, timeout: float) -> None:
        """Send one acknowledged DRW payload on a numeric PPPP channel."""

        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 255:
            raise TransportError("PPPP channel must be an integer from 0 to 255")
        if not isinstance(payload, bytes) or not payload:
            raise TransportError("PPPP channel payload must be non-empty bytes")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise TransportError("PPPP channel timeout must be greater than zero")

        async with self._request_lock:
            session = self._session
            if not self.connected or session is None:
                raise TransportError("aiopppp transport is not connected")
            api = _import_aiopppp()
            index = session.outgoing_command_idx
            session.outgoing_command_idx = (index + 1) & 0xFFFF
            packet = api["Packet"](
                api["PacketType"].Drw,
                struct.pack(">BBH", 0xD1, channel, index) + payload,
            )
            # aiopppp uses this private attribute to correlate DRW ACK packets.
            packet._cmd_idx = index
            await session.send(packet)
            try:
                await session._wait_ack(index, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise TransportTimeoutError(
                    f"camera did not acknowledge PPPP channel {channel} data"
                ) from exc

    async def _request_unlocked(self, command: str, *, timeout: float) -> str:
        if not self.connected or self._session is None:
            raise TransportError("aiopppp transport is not connected")
        if not command.startswith("GET /"):
            raise TransportError("raw aiopppp command must begin with 'GET /'")

        expected_code = self._expected_response_code(command)
        endpoint = command[4:].split("?", 1)[0]
        needs_observed_auth = endpoint not in {
            "/get_status.cgi",
            "/eye4_authentication.cgi",
        }
        if (
            self.config.auth_mode == "observed"
            and needs_observed_auth
            and not self._observed_authenticated
        ):
            try:
                auth_response = await self._send_framed(
                    format_eye4_auth_request(self.config),
                    timeout=timeout,
                    expected_code=0x7108,
                )
            except TransportTimeoutError as exc:
                if not self.connected:
                    raise TransportError(
                        "camera closed the session during account authentication"
                    ) from exc
                raise
            if "eye4_auth=1" not in auth_response.replace(" ", ""):
                raise TransportError("camera rejected the configured account authentication")
            self._observed_authenticated = True

        return await self._send_framed(
            command,
            timeout=timeout,
            expected_code=expected_code,
        )
