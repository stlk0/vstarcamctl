"""Raw command-channel adapter for aiopppp with configurable VStarcam PSK."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import struct
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable

from .cgi import format_eye4_auth_request, format_login_status_request
from .config import VStarcamConfig
from .discovery_udp import discover_with_seeds
from .errors import (
    ResponseParseError,
    TransportAuthenticationError,
    TransportCommandCancelledError,
    TransportError,
    TransportTimeoutError,
    TransportUnavailableError,
)
from .framing import (
    CGI_START_CODE,
    CgiFrameError,
    decode_cgi_frame,
    encode_cgi_request,
    expected_frame_size,
)
from .parser import parse_vstarcam_response
from .pppp_crypto import decrypt_packet, derive_effective_key, encrypt_packet
from .pppp_video import PpppVideoKeyframeAssembler
from .secrets import SecretMaskingFilter, mask_identifier
from .transport import KNOWN_PSKS, psk_for_device_id

LOGGER = logging.getLogger(__name__)
_AIOPPPP_LOG_FILTER = SecretMaskingFilter()
_AIOPPPP_LOGGERS = ("aiopppp.session",)
_AIOPPPP_PACKET_LOGGER = "aiopppp.packets"
_CGI_START_BYTES = struct.pack("<H", CGI_START_CODE)
_MAX_CGI_FRAGMENTS = 4096
_MAX_CGI_BUFFER_BYTES = 1024 * 1024
_EYE4_AUTH_ASSIGNMENT_RE = re.compile(r"(?:^|;)\s*(?:var\s+)?eye4_auth\s*=")
_AUTHENTICATION_FAILURE_RE = re.compile(rb"[\s\x00]*(?:var\s+)?result\s*=\s*-2\s*;?[\s\x00]*")


class _AioppppPacketLogFilter(logging.Filter):
    """Omit raw packet data from dependency logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        path = record.pathname.replace("\\", "/")
        if record.name == _AIOPPPP_PACKET_LOGGER or (
            record.name == "root" and path.endswith("/aiopppp/packets.py")
        ):
            record.msg = "aiopppp packet detail omitted"
            record.args = ()
        return True


_AIOPPPP_PACKET_LOG_FILTER = _AioppppPacketLogFilter()


@dataclass(frozen=True, slots=True)
class _CgiResponseFragment:
    index: int
    payload: bytes
    command_code: int | None
    binary_command: bool = False


class _CgiResponseQueue(asyncio.Queue[_CgiResponseFragment]):
    """Bound unsolicited traffic by both fragment count and retained bytes."""

    def __init__(self) -> None:
        super().__init__(maxsize=_MAX_CGI_FRAGMENTS)
        self.queued_bytes = 0
        self.overflowed = False

    def put_nowait(self, item: _CgiResponseFragment) -> None:
        item_size = len(item.payload)
        if item_size > _MAX_CGI_BUFFER_BYTES:
            self.overflowed = True
            return
        while self.qsize() >= _MAX_CGI_FRAGMENTS or (
            self.queued_bytes + item_size > _MAX_CGI_BUFFER_BYTES
        ):
            try:
                discarded = super().get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queued_bytes -= len(discarded.payload)
            self.overflowed = True
        super().put_nowait(item)
        self.queued_bytes += item_size

    def get_nowait(self) -> _CgiResponseFragment:
        item = super().get_nowait()
        self.queued_bytes -= len(item.payload)
        return item

    def reset_overflow(self) -> None:
        self.overflowed = False


def _raise_if_cancel_requested() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _secure_aiopppp_logging() -> None:
    """Apply the package's masking policy to dependency log records."""

    for name in _AIOPPPP_LOGGERS:
        logger = logging.getLogger(name)
        if _AIOPPPP_LOG_FILTER not in logger.filters:
            logger.addFilter(_AIOPPPP_LOG_FILTER)
    packet_logger = logging.getLogger(_AIOPPPP_PACKET_LOGGER)
    if _AIOPPPP_PACKET_LOG_FILTER not in packet_logger.filters:
        packet_logger.addFilter(_AIOPPPP_PACKET_LOG_FILTER)
    root_logger = logging.getLogger()
    if _AIOPPPP_PACKET_LOG_FILTER not in root_logger.filters:
        root_logger.addFilter(_AIOPPPP_PACKET_LOG_FILTER)


def _import_aiopppp() -> dict[str, Any]:
    _secure_aiopppp_logging()
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
    from aiopppp.session import BinarySession, SessionUDPProtocol
    from aiopppp.types import Channel, DeviceDescriptor, DeviceID, Encryption

    return {
        "BinaryCmdPkt": BinaryCmdPkt,
        "DrwPkt": DrwPkt,
        "Packet": Packet,
        "PacketType": PacketType,
        "make_p2palive_pkt": make_p2palive_pkt,
        "make_punch_pkt": make_punch_pkt,
        "parse_packet": parse_packet,
        "BinarySession": BinarySession,
        "SessionUDPProtocol": SessionUDPProtocol,
        "Channel": Channel,
        "DeviceDescriptor": DeviceDescriptor,
        "DeviceID": DeviceID,
        "Encryption": Encryption,
        "ENC_METHODS": ENC_METHODS,
    }


def _codec_for_seed(api: dict[str, Any], seed: str):
    key = derive_effective_key(seed)
    return api["Encryption"].XOR1, (
        lambda data: decrypt_packet(data, key),
        lambda data: encrypt_packet(data, key),
    )


class AioppppTransport:
    name = "aiopppp-legacy-cgi-psk"
    UNSOLICITED_RESPONSE_CODES = {0x6001, 0x6040}
    RESPONSE_CODES = {
        "/get_status.cgi": 0x6001,
        "/get_params.cgi": 0x6002,
        "/get_camera_params.cgi": 0x6003,
        "/get_record.cgi": 0x6006,
        "/wifi_scan.cgi": 0x602A,
        "/set_alarm.cgi": 0x600C,
        "/set_users.cgi": 0x600E,
        "/set_wifi.cgi": 0x6011,
        "/camera_control.cgi": 0x6012,
        "/set_datetime.cgi": 0x6013,
        "/reboot.cgi": 0x6027,
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
        self._authenticated = False
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
            def __init__(self, *args, packet_codec=None, source_address=None, **kwargs):
                super().__init__(*args, **kwargs)
                self._packet_codec = packet_codec or api["ENC_METHODS"][self.dev.encryption]
                self._source_address = source_address
                self._p2p_ready_seen = False
                self.cgi_responses = _CgiResponseQueue()
                self._recent_cgi_fragments: set[tuple[int, int | None, bool, bytes]] = set()
                self._recent_cgi_fragment_order: deque[tuple[int, int | None, bool, bytes]] = (
                    deque()
                )
                self._recent_cgi_fragment_bytes = 0
                self._video_keyframe_assembler: PpppVideoKeyframeAssembler | None = None
                self._video_keyframe_future: asyncio.Future[bytes | TransportError] | None = None
                self._video_capture_waiting = False
                self._video_capture_delivered = False

            async def create_udp(self):
                if self._source_address is None:
                    return await super().create_udp()
                loop = asyncio.get_running_loop()
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: api["SessionUDPProtocol"](lambda data: self.on_receive(data)),
                    local_addr=(self._source_address, 0),
                    remote_addr=(self.dev.addr, self.dev.port),
                )
                return transport

            def start_video_queue(self) -> None:
                # BinarySession.stop() expects this handle, but its stock video
                # processor retains an unbounded frame history. RawCgiSession
                # consumes every queued chunk synchronously in handle_drw().
                self.process_video_task = asyncio.get_running_loop().create_future()

            def _drain_video_chunks(self) -> None:
                while not self.video_chunk_queue.empty():
                    self.video_chunk_queue.get_nowait()

            def _start_video_capture(self) -> None:
                if self._video_keyframe_future is not None:
                    raise TransportError("PPPP video capture is already active")
                self._drain_video_chunks()
                self._video_keyframe_assembler = PpppVideoKeyframeAssembler()
                self._video_keyframe_future = asyncio.get_running_loop().create_future()
                self._video_capture_waiting = False
                self._video_capture_delivered = False

            def _begin_video_capture_wait(self) -> asyncio.Future[bytes | TransportError]:
                future = self._video_keyframe_future
                if future is None:
                    raise TransportError("PPPP video capture is not active")
                if self._video_capture_delivered:
                    raise TransportError("PPPP video keyframe was already delivered")
                if self._video_capture_waiting:
                    raise TransportError("PPPP video capture already has a receiver")
                self._video_capture_waiting = True
                return future

            def _end_video_capture_wait(
                self,
                future: asyncio.Future[bytes | TransportError],
                *,
                delivered: bool,
            ) -> None:
                if self._video_keyframe_future is future:
                    self._video_capture_waiting = False
                    self._video_capture_delivered = delivered

            def _abort_video_capture(self, message: str) -> None:
                self._video_keyframe_assembler = None
                future = self._video_keyframe_future
                if future is not None and not future.done():
                    future.set_result(TransportError(message))

            def _stop_video_capture(self) -> None:
                self._abort_video_capture("PPPP video capture stopped")
                self._video_keyframe_future = None
                self._video_capture_waiting = False
                self._video_capture_delivered = False
                self._drain_video_chunks()

            def _queue_cgi_response(self, fragment: _CgiResponseFragment) -> None:
                key = (
                    fragment.index,
                    fragment.command_code,
                    fragment.binary_command,
                    fragment.payload,
                )
                if key in self._recent_cgi_fragments:
                    return
                fragment_size = len(fragment.payload)
                while self._recent_cgi_fragment_order and (
                    len(self._recent_cgi_fragment_order) >= _MAX_CGI_FRAGMENTS
                    or self._recent_cgi_fragment_bytes + fragment_size > _MAX_CGI_BUFFER_BYTES
                ):
                    expired = self._recent_cgi_fragment_order.popleft()
                    self._recent_cgi_fragments.remove(expired)
                    self._recent_cgi_fragment_bytes -= len(expired[3])
                if fragment_size <= _MAX_CGI_BUFFER_BYTES:
                    self._recent_cgi_fragment_order.append(key)
                    self._recent_cgi_fragments.add(key)
                    self._recent_cgi_fragment_bytes += fragment_size
                self.cgi_responses.put_nowait(fragment)

            async def _send(self, pkt):
                # aiopppp resolves encryption through its module-global
                # ENC_METHODS table for every packet. Keep the codec on this
                # session instead so opening a camera with another seed cannot
                # change an already-running session.
                if pkt.type == api["PacketType"].Drw:
                    self.drw_waiters[pkt._cmd_idx] = asyncio.get_running_loop().create_future()
                encoded = self._packet_codec[1](bytes(pkt))
                self.transport.sendto(encoded, (self.dev.addr, self.dev.port))

            def on_receive(self, data):
                # aiopppp 0.2.3 converts the packet type to its closed Enum
                # without handling newer/unsupported types. Some VE cameras
                # repeatedly send type 0x43, which otherwise escapes from the
                # datagram callback as ``ValueError: 67 is not a valid
                # PacketType``. Unknown packets cannot be interpreted by this
                # version of aiopppp, so discard them without disturbing the
                # session; malformed and known packets retain its normal
                # parsing behaviour.
                decoded = self._packet_codec[0](data)
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

            async def handle_incoming_packet(self, pkt):
                if pkt.type == api["PacketType"].P2pRdy:
                    if self._p2p_ready_seen:
                        return
                    # Repeated ready packets must not keep extending aiopppp's
                    # 200 ms debounce and postpone setup until the camera stops.
                    self._p2p_ready_seen = True
                await super().handle_incoming_packet(pkt)

            async def setup_device(self):
                # CGI requests carry their own auth suffix, so skip aiopppp's
                # model-specific binary login/status sequence. Send three
                # immediate keepalives after P2pRdy and before the first CGI.
                for _ in range(3):
                    await self.send(api["make_p2palive_pkt"]())
                # aiopppp 0.2.3 does not model the required sentinel readiness
                # ACK immediately before the first CGI frame.
                await self.send(
                    api["Packet"](api["PacketType"].DrwAck, b"\xd1\x07\x00\x01\xaa\xaa")
                )
                self.device_is_ready.set()

            async def send_initial_packets(self):
                # Compatible devices require three PunchPkt copies and two more
                # around 90 ms later before their P2pRdy response. Omit
                # BinarySession's premature outgoing P2pRdy.
                packet = api["make_punch_pkt"](self.dev.dev_id)
                for _ in range(3):
                    await self.send(packet)
                await asyncio.sleep(0.09)
                for _ in range(2):
                    await self.send(packet)

            async def handle_drw(self, drw_pkt):
                await super().handle_drw(drw_pkt)
                if drw_pkt._channel == channel.Video:
                    while not self.video_chunk_queue.empty():
                        epoch, packet = self.video_chunk_queue.get_nowait()
                        assembler = self._video_keyframe_assembler
                        future = self._video_keyframe_future
                        if assembler is None or future is None or future.done():
                            continue
                        try:
                            keyframe = assembler.feed(
                                epoch * 0x10000 + packet._cmd_idx,
                                packet.get_drw_payload(),
                            )
                        except TransportError as exc:
                            self._video_keyframe_assembler = None
                            future.set_result(exc)
                        else:
                            if keyframe is not None:
                                self._video_keyframe_assembler = None
                                future.set_result(keyframe)
                    return
                if drw_pkt._channel != channel.Command:
                    return
                if isinstance(drw_pkt, binary_cmd_pkt):
                    command_value = getattr(drw_pkt.command, "value", 0)
                    if 0x6000 <= command_value <= 0x60FF:
                        self._queue_cgi_response(
                            _CgiResponseFragment(
                                index=drw_pkt._cmd_idx,
                                payload=drw_pkt.cmd_payload,
                                command_code=command_value,
                                binary_command=True,
                            )
                        )
                else:
                    payload = drw_pkt.get_drw_payload()
                    command_code = None
                    try:
                        if expected_frame_size(payload) is not None:
                            command_code = struct.unpack_from("<H", payload, 2)[0]
                    except CgiFrameError:
                        pass
                    self._queue_cgi_response(
                        _CgiResponseFragment(
                            index=drw_pkt._cmd_idx,
                            payload=payload,
                            command_code=command_code,
                        )
                    )

        return RawCgiSession

    async def _encrypted_descriptor(self, api, ports):
        return await self._discover_descriptor(api, ports, encrypted=True)

    async def _plaintext_descriptor(self, api, ports):
        return await self._discover_descriptor(api, ports, encrypted=False)

    async def _discover_descriptor(self, api, ports, *, encrypted: bool):
        if not self.config.host:
            return None
        kind = "encrypted" if encrypted else "plaintext"
        seeds = ()
        profile_options = {}
        if encrypted:
            explicit_psk = self.config.psk
            mapped_psk = psk_for_device_id(self.config.device_id)
            seeds = (
                (explicit_psk,)
                if explicit_psk is not None
                else ((mapped_psk,) if mapped_psk is not None else KNOWN_PSKS)
            )
            profile_options = {
                "require_encrypted": True,
                "require_matching_profile": explicit_psk is None,
            }
        is_broadcast = self.config.host == "255.255.255.255"
        if encrypted and is_broadcast and not self.config.device_id:
            raise TransportError("broadcast camera connection requires a configured device ID")
        try:
            results = await asyncio.to_thread(
                discover_with_seeds,
                self.config.host,
                ports=ports,
                seeds=seeds,
                timeout=min(5.0, self.config.timeout) if encrypted else self.config.timeout,
                expected_host=None if is_broadcast else self.config.host,
                expected_device_id=self.config.device_id if is_broadcast else None,
                stop_after_first=True,
                source_address=self.config.source_address,
                **profile_options,
            )
        except (OSError, ValueError) as exc:
            raise TransportError(f"{kind} camera discovery failed: {exc}") from exc
        if not results:
            return None
        result = results[0]
        if encrypted and result.psk is None:
            raise TransportError("encrypted discovery did not identify its transport profile")
        if self.config.device_id is not None and result.device_id != self.config.device_id:
            raise TransportError(
                f"{kind} discovery found a different camera identity at the configured "
                "host; refusing the configured endpoint fallback"
            )
        try:
            prefix, serial, suffix = result.device_id.split("-", 2)
        except ValueError as exc:
            raise TransportError(f"{kind} discovery returned a malformed PPPP device ID") from exc
        if encrypted:
            encryption, packet_codec = _codec_for_seed(api, result.psk)
        else:
            encryption = api["Encryption"].NONE
            packet_codec = api["ENC_METHODS"][encryption]
        LOGGER.debug(
            "%s PPPP discovery port=%d device_id=%s",
            kind,
            result.port,
            mask_identifier(result.device_id),
        )
        return (
            api["DeviceDescriptor"](
                api["DeviceID"](prefix, serial, suffix),
                result.host,
                result.port,
                encryption=encryption,
                is_json=False,
            ),
            packet_codec,
        )

    def _configured_descriptor(self, api):
        psk = self.config.psk or psk_for_device_id(self.config.device_id)
        if not self.config.device_id or not self.config.host or not psk:
            return None
        try:
            prefix, serial, suffix = self.config.device_id.split("-", 2)
        except ValueError as exc:
            raise TransportError(
                "configured PPPP device ID must use PREFIX-SERIAL-SUFFIX form"
            ) from exc
        encryption, packet_codec = _codec_for_seed(api, psk)
        LOGGER.debug(
            "using configured PPPP fallback port=%d device_id=%s",
            self.config.udp_port,
            mask_identifier(self.config.device_id),
        )
        return (
            api["DeviceDescriptor"](
                api["DeviceID"](prefix, serial, suffix),
                self.config.host,
                self.config.udp_port,
                encryption=encryption,
                is_json=False,
            ),
            packet_codec,
        )

    async def _resolve_descriptor(self, api, ports):
        resolved = await self._encrypted_descriptor(api, ports)
        if resolved is not None:
            return resolved

        resolved = await self._plaintext_descriptor(api, ports)
        if resolved is not None:
            return resolved

        # Some encrypted cameras answer neither discovery form while still
        # accepting a connection on the configured fallback port. Preserve
        # that behavior, but only after giving plaintext discovery a chance.
        resolved = self._configured_descriptor(api)
        if resolved is not None:
            return resolved
        raise TransportTimeoutError("camera did not answer PPPP discovery on configured ports")

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            await self._connect_unlocked()

    async def _connect_unlocked(self) -> None:
        if self.connected:
            return
        if self._session is not None:
            await self._close_unlocked()
        self._authenticated = False
        if not self.config.host:
            raise TransportError("host is required for aiopppp transport")
        api = _import_aiopppp()
        ports = list(dict.fromkeys((self.config.discovery_port, self.config.udp_port)))
        descriptor, packet_codec = await self._resolve_descriptor(api, ports)
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
            on_disconnect=lambda *_: self._mark_disconnected(session),
            login=self.config.username,
            password=self.config.password or "",
            packet_codec=packet_codec,
            source_address=self.config.source_address,
        )
        self._session = session
        session.start()
        session_tasks = tuple(session.running_tasks())

        def session_task_done(task: asyncio.Future[Any]) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                task.exception()
            if self._session is not session:
                return
            self._mark_disconnected(session)
            with contextlib.suppress(RuntimeError):
                session.stop()

        for task in session_tasks:
            task.add_done_callback(session_task_done)
        ready_waiter = asyncio.create_task(session.device_is_ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_waiter, *session_tasks},
                timeout=self.config.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            _raise_if_cancel_requested()
            if not done:
                raise asyncio.TimeoutError
            # Readiness and a failed session task can become observable in the
            # same loop turn. A completed session task always wins: no live
            # session remains even if the readiness event was set.
            completed_tasks = [task for task in session_tasks if task.done()]
            for task in completed_tasks:
                if not task.cancelled() and (failure := task.exception()) is not None:
                    raise TransportError("PPPP session failed before becoming ready") from failure
            if completed_tasks:
                raise TransportError("PPPP session ended before becoming ready")
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

    def _mark_disconnected(self, session) -> None:
        if self._session is not session:
            return
        self._connected = False
        abort = getattr(session, "_abort_video_capture", None)
        if abort is not None:
            abort("PPPP session ended during video capture")

    def _quarantine_session(self, session) -> None:
        """Synchronously stop a session whose response correlation is no longer safe."""

        if self._session is not session:
            return
        self._mark_disconnected(session)
        try:
            session.stop()
        except (Exception, asyncio.CancelledError):
            # A cancellation can interrupt setup before aiopppp reaches its
            # CONNECTED state. Its regular stop() rejects that state, so close
            # the already-created resources directly.
            running_tasks = getattr(session, "running_tasks", None)
            for task in running_tasks() if callable(running_tasks) else ():
                task.cancel()
        transport = getattr(session, "transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
            session.transport = None

    async def close(self) -> None:
        async with self._lifecycle_lock:
            async with self._request_lock:
                await self._close_unlocked()

    @asynccontextmanager
    async def session_lease(self) -> AsyncIterator[None]:
        """Prevent reconnect or close from replacing an in-flight media session."""

        async with self._lifecycle_lock:
            if not self.connected:
                raise TransportError("aiopppp transport is not connected")
            yield

    async def _close_unlocked(self) -> None:
        session = self._session
        if session is not None:
            abort = getattr(session, "_abort_video_capture", None)
            if abort is not None:
                abort("PPPP session closed during video capture")
        self._session = None
        self._connected = False
        self._authenticated = False
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

    @classmethod
    def _response_code_matches(cls, command_code: int, expected_code: int | None) -> bool:
        if expected_code is not None:
            return command_code == expected_code
        # Periodic status and notification frames are known to arrive without
        # a matching request. The protocol has no request token relating the
        # camera's DRW index to the client's index, so a never-before-seen late
        # frame cannot be distinguished for an endpoint whose response code is
        # unknown. The session-level index/payload cache rejects retransmits;
        # reject the remaining unsolicited codes we can identify here. Add an
        # endpoint to RESPONSE_CODES as soon as its wire code is evidenced.
        return command_code not in cls.UNSOLICITED_RESPONSE_CODES and command_code != 0

    @staticmethod
    def _could_start_cgi_frame(payload: bytes) -> bool:
        if not payload:
            return False
        if len(payload) < len(_CGI_START_BYTES):
            return _CGI_START_BYTES.startswith(payload)
        return payload.startswith(_CGI_START_BYTES)

    @classmethod
    def _take_complete_response(
        cls,
        pending: dict[int, _CgiResponseFragment],
        *,
        expected_code: int | None,
    ) -> tuple[bool, str]:
        for start_index, first in tuple(pending.items()):
            if first.binary_command:
                pending.pop(start_index, None)
                if cls._response_code_matches(first.command_code or 0, expected_code):
                    return True, cls._decode_response([first.payload])
                LOGGER.debug(
                    "ignored unrelated binary CGI response code=0x%04x",
                    first.command_code or 0,
                )
                continue
            if not cls._could_start_cgi_frame(first.payload):
                # BinaryCmdPkt carries a separately parsed command code and is
                # handled above. A raw, unframed DRW payload has no endpoint
                # correlation evidence, so fail closed instead of accepting it
                # as the current request's response.
                continue

            chunks: list[bytes] = []
            used_indices: list[int] = []
            payload = bytearray()
            index = start_index
            target_size = None
            command_code = None
            while True:
                fragment = pending.get(index)
                if fragment is None or fragment.binary_command:
                    break
                if used_indices and fragment.command_code is not None:
                    break
                chunks.append(fragment.payload)
                used_indices.append(index)
                payload.extend(fragment.payload)
                if target_size is None:
                    try:
                        target_size = expected_frame_size(payload)
                    except CgiFrameError:
                        break
                    if target_size is not None:
                        command_code = struct.unpack_from("<H", payload, 2)[0]
                if target_size is not None and len(payload) >= target_size:
                    for used_index in used_indices:
                        pending.pop(used_index, None)
                    if command_code is None:
                        break
                    if cls._response_code_matches(command_code, expected_code):
                        return True, cls._decode_response(chunks)
                    if (
                        command_code == 0x6001
                        and expected_code not in {None, 0x6001, 0x7108}
                        and len(payload) == target_size
                        and _AUTHENTICATION_FAILURE_RE.fullmatch(
                            decode_cgi_frame(bytes(payload)).payload
                        )
                    ):
                        raise TransportAuthenticationError(
                            "camera reported authentication failure (result=-2); "
                            "the expected CGI response did not arrive"
                        )
                    if expected_code is None:
                        LOGGER.debug(
                            "ignored unsolicited CGI response code=0x%04x",
                            command_code,
                        )
                    else:
                        LOGGER.debug(
                            "ignored unrelated CGI response code=0x%04x while waiting for 0x%04x",
                            command_code,
                            expected_code,
                        )
                    break
                index = (index + 1) & 0xFFFF
        return False, ""

    @staticmethod
    def _cleanup_unacknowledged_waiter(session: Any, index: int) -> None:
        """Remove only a waiter that the dependency's ACK handler does not own."""

        waiters = getattr(session, "drw_waiters", {})
        waiter = waiters.get(index)
        if waiter is None:
            return
        # aiopppp resolves the future, yields, then deletes the entry. Removing
        # a successfully resolved waiter here would race that final deletion.
        if waiter.done() and not waiter.cancelled():
            return
        waiters.pop(index, None)

    @staticmethod
    def _raise_if_response_queue_overflowed(session: Any) -> None:
        if getattr(session.cgi_responses, "overflowed", False):
            raise TransportError("camera response traffic exceeded the safe CGI buffer")

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
        reset_overflow = getattr(session.cgi_responses, "reset_overflow", None)
        if reset_overflow is not None:
            reset_overflow()
        api = _import_aiopppp()
        index = session.outgoing_command_idx
        session.outgoing_command_idx = (index + 1) & 0xFFFF
        packet = api["DrwPkt"](
            api["Channel"].Command.value,
            index,
            encode_cgi_request(command),
        )
        deadline = asyncio.get_running_loop().time() + timeout
        send_started = False
        try:
            try:
                send_started = True
                await session.send(packet)
                _raise_if_cancel_requested()
                # Calling the private primitive avoids aiopppp re-raising a cancelled
                # session task from its generic error-check wrapper during disconnect.
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await session._wait_ack(index, timeout=remaining)
                _raise_if_cancel_requested()
            except asyncio.CancelledError as exc:
                self._cleanup_unacknowledged_waiter(session, index)
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise TransportCommandCancelledError(
                        "camera command cancellation occurred after send started; outcome unknown"
                    ) from exc
                raise TransportError(
                    "camera closed the PPPP session before acknowledging CGI"
                ) from exc
            except asyncio.TimeoutError as exc:
                self._cleanup_unacknowledged_waiter(session, index)
                raise TransportTimeoutError(
                    "camera did not acknowledge the framed CGI request"
                ) from exc
            except Exception as exc:
                self._cleanup_unacknowledged_waiter(session, index)
                if isinstance(exc, TransportError):
                    raise
                raise TransportError("camera command send failed") from exc
            except BaseException:
                self._cleanup_unacknowledged_waiter(session, index)
                raise

            pending: dict[int, _CgiResponseFragment] = {}
            authentication_failure: TransportAuthenticationError | None = None
            while True:
                self._raise_if_response_queue_overflowed(session)
                try:
                    complete, response = self._take_complete_response(
                        pending,
                        expected_code=expected_code,
                    )
                except TransportAuthenticationError as exc:
                    # A status frame has no request token and may be late. Keep
                    # waiting so a matching response takes precedence over it.
                    authentication_failure = exc
                    continue
                if complete:
                    return response
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if authentication_failure is not None:
                        raise authentication_failure
                    raise TransportTimeoutError(
                        "PPPP command was acknowledged but its CGI response was incomplete"
                    )
                try:
                    fragment = await asyncio.wait_for(
                        session.cgi_responses.get(),
                        timeout=remaining,
                    )
                    _raise_if_cancel_requested()
                except asyncio.CancelledError as exc:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise TransportCommandCancelledError(
                            "camera command cancellation occurred after send started; "
                            "outcome unknown"
                        ) from exc
                    raise TransportError(
                        "camera closed the PPPP session before completing its CGI response"
                    ) from exc
                except asyncio.TimeoutError as exc:
                    if authentication_failure is not None:
                        raise authentication_failure from exc
                    raise TransportTimeoutError(
                        "PPPP command was acknowledged but no complete CGI response arrived"
                    ) from exc
                self._raise_if_response_queue_overflowed(session)
                previous = pending.get(fragment.index)
                if previous is None:
                    pending_bytes = sum(len(item.payload) for item in pending.values())
                    if (
                        len(pending) >= _MAX_CGI_FRAGMENTS
                        or pending_bytes + len(fragment.payload) > _MAX_CGI_BUFFER_BYTES
                    ):
                        raise TransportError(
                            "camera response exceeded the safe CGI reassembly buffer"
                        )
                    pending[fragment.index] = fragment
                elif previous != fragment:
                    LOGGER.debug(
                        "ignored conflicting duplicate CGI fragment index=%d",
                        fragment.index,
                    )
        except BaseException:
            if send_started:
                # The protocol has no request token. Never reuse a session after
                # an interrupted/incomplete command because its late same-code
                # response could otherwise be accepted for the next request.
                self._quarantine_session(session)
            raise

    async def request(self, command: str, *, timeout: float) -> str:
        async with self._request_lock:
            return await self._request_unlocked(command, timeout=timeout)

    async def send_channel_parts(
        self,
        channel: int,
        parts: Iterable[bytes],
        *,
        timeout: float,
    ) -> None:
        """Send acknowledged DRW payloads atomically with respect to CGI requests."""

        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 255:
            raise TransportError("PPPP channel must be an integer from 0 to 255")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise TransportError("PPPP channel timeout must be a finite positive number")
        if isinstance(parts, (bytes, bytearray)):
            raise TransportError("PPPP channel parts must be an iterable of bytes payloads")
        payloads = tuple(parts)
        if not payloads:
            raise TransportError("PPPP channel parts must contain at least one payload")
        if any(not isinstance(payload, bytes) or not payload for payload in payloads):
            raise TransportError("PPPP channel payload must be non-empty bytes")

        async with self._request_lock:
            session = self._session
            if not self.connected or session is None:
                raise TransportError("aiopppp transport is not connected")
            api = _import_aiopppp()
            deadline = asyncio.get_running_loop().time() + timeout
            for payload in payloads:
                index = session.outgoing_command_idx
                session.outgoing_command_idx = (index + 1) & 0xFFFF
                packet = api["Packet"](
                    api["PacketType"].Drw,
                    struct.pack(">BBH", 0xD1, channel, index) + payload,
                )
                # aiopppp uses this private attribute to correlate DRW ACK packets.
                packet._cmd_idx = index
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await session.send(packet)
                    _raise_if_cancel_requested()
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await session._wait_ack(index, timeout=remaining)
                    _raise_if_cancel_requested()
                except asyncio.CancelledError as exc:
                    self._cleanup_unacknowledged_waiter(session, index)
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise TransportCommandCancelledError(
                            "camera channel cancellation occurred after send started; "
                            "outcome unknown"
                        ) from exc
                    raise TransportError(
                        f"camera closed the PPPP session before acknowledging channel {channel}"
                    ) from exc
                except asyncio.TimeoutError as exc:
                    self._cleanup_unacknowledged_waiter(session, index)
                    raise TransportTimeoutError(
                        f"camera did not acknowledge PPPP channel {channel} data"
                    ) from exc
                except Exception as exc:
                    self._cleanup_unacknowledged_waiter(session, index)
                    if isinstance(exc, TransportError):
                        raise
                    raise TransportError(f"PPPP channel {channel} send failed") from exc
                except BaseException:
                    self._cleanup_unacknowledged_waiter(session, index)
                    raise

    async def start_video_capture(self) -> None:
        async with self._request_lock:
            session = self._session
            if not self.connected or session is None:
                raise TransportError("aiopppp transport is not connected")
            session._start_video_capture()

    async def receive_video_keyframe(self, *, timeout: float) -> bytes:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise TransportError("PPPP video capture timeout must be a finite positive number")

        async with self._request_lock:
            session = self._session
            if not self.connected or session is None:
                raise TransportError("aiopppp transport is not connected")
            future = session._begin_video_capture_wait()

        delivered = False
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
                _raise_if_cancel_requested()
            except asyncio.TimeoutError as exc:
                raise TransportTimeoutError("camera did not provide a PPPP video keyframe") from exc
            except asyncio.CancelledError as exc:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                raise TransportError("PPPP video capture stopped") from exc

            if isinstance(result, TransportError):
                raise result
            if self._session is not session or not self.connected:
                raise TransportError("PPPP session changed during video capture")
            delivered = True
            return result
        finally:
            session._end_video_capture_wait(future, delivered=delivered)

    async def stop_video_capture(self) -> None:
        async with self._request_lock:
            session = self._session
            if session is not None:
                stop = getattr(session, "_stop_video_capture", None)
                if stop is not None:
                    stop()

    async def _request_unlocked(self, command: str, *, timeout: float) -> str:
        if not self.connected or self._session is None:
            raise TransportError("aiopppp transport is not connected")
        if not command.startswith("GET /"):
            raise TransportError("raw aiopppp command must begin with 'GET /'")

        expected_code = self._expected_response_code(command)
        endpoint = command[4:].split("?", 1)[0]
        needs_auth = endpoint not in {
            "/get_status.cgi",
            "/eye4_authentication.cgi",
        }
        if needs_auth and not self._authenticated:
            try:
                login_response = await self._send_framed(
                    format_login_status_request(self.config),
                    timeout=timeout,
                    expected_code=0x6001,
                )
                try:
                    login_payload = parse_vstarcam_response(login_response)
                except ResponseParseError as exc:
                    raise TransportError("camera returned malformed login metadata") from exc
                login_result = login_payload.get("result")
                if (type(login_result) is int and login_result < 0) or (
                    isinstance(login_result, str) and re.fullmatch(r"-\d+", login_result)
                ):
                    raise TransportAuthenticationError("camera rejected the configured login")
                dual_authentication = login_payload.get("DualAuthentication", 0)
                if self.config.auth_mode == "basic" and (
                    type(dual_authentication) is not int or dual_authentication not in (0, 1, 2)
                ):
                    raise TransportError("camera returned invalid DualAuthentication metadata")
                needs_dual_auth = self.config.auth_mode == "observed" or dual_authentication != 0
                if needs_dual_auth:
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
            if needs_dual_auth:
                try:
                    auth_payload = parse_vstarcam_response(auth_response)
                except ResponseParseError as exc:
                    raise TransportError(
                        "camera returned malformed account authentication metadata"
                    ) from exc
                if (
                    len(_EYE4_AUTH_ASSIGNMENT_RE.findall(auth_response)) != 1
                    or type(auth_payload.get("eye4_auth")) is not int
                    or auth_payload["eye4_auth"] != 1
                ):
                    raise TransportError("camera rejected the configured account authentication")
            self._authenticated = True

        return await self._send_framed(
            command,
            timeout=timeout,
            expected_code=expected_code,
        )
