"""High-level asynchronous API for VStarcam-compatible cameras."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable
from urllib.parse import urlencode

from ._normalize import capability_enabled
from .account import build_camera_account_password_set_path, validate_camera_account_password
from .audio_talk import (
    ADPCM_FRAME_DURATION,
    TALK_CHANNEL,
    validate_adpcm_talk_frame,
)
from .cgi import format_get_request, validate_raw_path
from .command_catalog import CommandCatalog
from .config import VStarcamConfig
from .detection import (
    build_human_detection_set_path,
    build_human_frame_set_path,
    build_human_sensitivity_set_path,
    build_human_tracking_set_path,
    build_human_zoom_tracking_set_path,
    build_motion_detection_set_path,
    parse_human_detection_status,
    parse_motion_detection_status,
)
from .errors import (
    AccountChangeUncertainError,
    AccountConfigurationError,
    ConfirmationRequiredError,
    DetectionConfigurationError,
    ExperimentalCommandError,
    MediaConfigurationError,
    MediaStreamError,
    ServiceChangeUncertainError,
    TimeConfigurationError,
    TransportError,
    TransportTimeoutError,
    WifiChangeUncertainError,
)
from .media import (
    AudioVolumeTarget,
    build_audio_volume_set_path,
    build_onvif_set_path,
    build_pppp_livestream_path,
    build_record_audio_set_path,
    build_rtsp_set_path,
    parse_audio_status,
    parse_onvif_status,
    parse_rtsp_status,
)
from .media_stream import RTSPStream, RTSPTransport, StreamQuality
from .night_vision import (
    NightVisionMode,
    build_infrared_light_set_path,
    build_night_vision_set_paths,
    parse_infrared_light_status,
    parse_night_vision_status,
)
from .parser import parse_vstarcam_response
from .secrets import mask_identifier, mask_secrets
from .time_settings import (
    build_time_settings_set_path,
    parse_time_settings,
    validate_ntp_server,
    validate_unix_time,
    validate_utc_offset,
)
from .transport import CameraTransport
from .wifi import (
    build_wifi_set_path,
    complete_wifi_metadata,
    extract_wifi_status,
    normalize_wifi_scan,
    validate_wifi_credentials,
)

LOGGER = logging.getLogger(__name__)


class VStarcamCamera:
    def __init__(
        self,
        config: VStarcamConfig,
        *,
        transport: CameraTransport | None = None,
        catalog: CommandCatalog | None = None,
    ):
        self.config = config
        self.catalog = catalog or CommandCatalog.load()
        self.transport = transport or self._make_transport(config)

    @staticmethod
    def _make_transport(config: VStarcamConfig) -> CameraTransport:
        if config.transport == "aiopppp":
            from .transport_aiopppp import AioppppTransport

            return AioppppTransport(config)
        raise TransportError(
            "the fake transport must be injected by tests, not selected from the CLI"
        )

    @property
    def connected(self) -> bool:
        return self.transport.connected

    async def connect(self) -> None:
        self.config.validate(require_target=True, require_auth=True)
        LOGGER.debug(
            "connecting transport=%s port=%d vuid=%s auth_mode=%s",
            self.transport.name,
            self.config.udp_port,
            mask_identifier(self.config.vuid),
            self.config.auth_mode,
        )
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            try:
                await self.transport.connect()
                return
            except (asyncio.TimeoutError, TimeoutError, OSError, TransportError) as exc:
                await self.transport.close()
                if attempt + 1 >= attempts:
                    if isinstance(exc, TransportError):
                        raise
                    raise TransportTimeoutError(
                        f"camera connection failed after {attempts} attempt(s): {exc}"
                    ) from exc
                LOGGER.debug("connection attempt %d failed; retrying", attempt + 1)

    async def close(self) -> None:
        await self.transport.close()

    async def __aenter__(self) -> "VStarcamCamera":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    def format_raw_cgi(self, path: str) -> str:
        return format_get_request(validate_raw_path(path), self.config)

    async def _request_with_retry(self, command: str) -> str:
        if not self.connected:
            await self.connect()
        attempts = self.config.retries + 1
        started = asyncio.get_running_loop().time()
        for attempt in range(attempts):
            try:
                response = await self.transport.request(command, timeout=self.config.timeout)
                elapsed = asyncio.get_running_loop().time() - started
                LOGGER.debug(
                    "response transport=%s length=%d elapsed=%.3fs",
                    self.transport.name,
                    len(response),
                    elapsed,
                )
                return response
            except (asyncio.TimeoutError, TimeoutError, OSError, TransportError) as exc:
                if attempt + 1 >= attempts:
                    if isinstance(exc, TransportError):
                        raise
                    raise TransportTimeoutError(
                        f"camera request failed after {attempts} attempt(s): {exc}"
                    ) from exc
                LOGGER.debug("request attempt %d failed; reconnecting", attempt + 1)
                await self.transport.close()
                await self.connect()
        raise AssertionError("retry loop exhausted unexpectedly")

    async def _request_once(self, command: str) -> str:
        if not self.connected:
            await self.connect()
        started = asyncio.get_running_loop().time()
        response = await self.transport.request(command, timeout=self.config.timeout)
        elapsed = asyncio.get_running_loop().time() - started
        LOGGER.debug(
            "response transport=%s length=%d elapsed=%.3fs",
            self.transport.name,
            len(response),
            elapsed,
        )
        return response

    async def send_raw_cgi(
        self,
        path: str,
        *,
        experimental: bool = False,
        confirm: bool = False,
        recovery_ready: bool = False,
        retry_requests: bool = True,
    ) -> str:
        path = validate_raw_path(path)
        definition = self.catalog.match_path(path)
        if definition is None and not experimental:
            raise ExperimentalCommandError(
                "unknown raw CGI command; inspect it first and pass --experimental to send"
            )
        if definition is not None:
            definition.require_permission(experimental=experimental, confirm=confirm)
            if definition.recovery_required and not recovery_ready:
                guarded_name = (
                    "guarded wifi set"
                    if definition.name == "wifi_set"
                    else "guarded account password"
                )
                raise ConfirmationRequiredError(
                    f"{definition.name}: use its guarded command ({guarded_name}) "
                    "with --recovery-ready"
                )
            if definition.retry_policy == "one_shot" and retry_requests:
                raise ConfirmationRequiredError(
                    f"{definition.name}: request retries are forbidden because the "
                    "outcome may be uncertain; use its guarded high-level command"
                )
        command = self.format_raw_cgi(path)
        LOGGER.debug("command> %s", mask_secrets(command))
        if retry_requests:
            return await self._request_with_retry(command)
        return await self._request_once(command)

    async def get_status(self) -> dict:
        query = urlencode({"vuid": self.config.vuid}) if self.config.vuid else ""
        path = f"/get_status.cgi?{query}" if query else "/get_status.cgi"
        return parse_vstarcam_response(await self.send_raw_cgi(path))

    async def get_params(self) -> dict:
        return parse_vstarcam_response(await self.send_raw_cgi("/get_params.cgi"))

    async def _send_service_change(
        self,
        path: str,
        operation: str,
        *,
        experimental: bool,
        confirm: bool,
    ) -> object:
        if not self.connected:
            await self.connect()
        try:
            response = await self.send_raw_cgi(
                path,
                experimental=experimental,
                confirm=confirm,
                retry_requests=False,
            )
        except (asyncio.TimeoutError, TimeoutError, OSError, TransportError) as exc:
            raise ServiceChangeUncertainError(
                f"{operation} was sent once without retry, but no acknowledgement arrived; "
                "the outcome is unknown and must be verified with the status command"
            ) from exc
        return parse_vstarcam_response(response)

    async def _get_rtsp_payload(self) -> dict:
        return parse_vstarcam_response(await self.send_raw_cgi("/get_rtsp.cgi"))

    async def get_rtsp_settings(self) -> dict:
        payload = await self._get_rtsp_payload()
        params = await self.get_params()
        return parse_rtsp_status(payload, params)

    async def get_rtsp_stream(
        self,
        *,
        quality: StreamQuality = "main",
        transport: RTSPTransport = "tcp",
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> RTSPStream:
        """Resolve a playable RTSP stream without exposing its credentials.

        When ``port`` is omitted, the RTSP settings getter supplies the
        port, enable state, and credential source. An explicit port skips that
        PPPP lookup and permits RTSP-only use.
        """

        if (username is None) != (password is None):
            raise MediaConfigurationError("RTSP username and password must be provided together")

        settings: dict | None = None
        params: dict | None = None
        if port is None:
            payload = await self._get_rtsp_payload()
            params = await self.get_params()
            settings = parse_rtsp_status(payload, params)
            if not settings["enabled"]:
                raise MediaConfigurationError("RTSP is disabled on the camera")
            port = settings["port"]

        if username is None and password is None:
            if settings is None:
                raise MediaConfigurationError(
                    "an explicit RTSP port also requires an explicit RTSP username and password"
                )
            if (
                settings["authentication_enabled"]
                and settings["credential_source"] == "camera_account"
            ):
                web_password = params.get("WebPwd") if params is not None else None
                if not isinstance(web_password, str) or not web_password:
                    raise MediaConfigurationError(
                        "camera-account RTSP authentication is enabled but WebPwd is unavailable"
                    )
                username = self.config.username
                password = web_password
            elif settings["authentication_enabled"]:
                raise MediaConfigurationError(
                    "RTSP uses dedicated or unknown credentials; provide both "
                    "RTSP username and password explicitly"
                )

        return RTSPStream(
            host=self.config.host,
            port=port,
            username=username,
            password=password,
            quality=quality,
            transport=transport,
        )

    def format_rtsp_set_cgi(
        self,
        enabled: bool,
        *,
        port: int,
        username: str,
        password: str,
    ) -> str:
        return self.format_raw_cgi(build_rtsp_set_path(enabled, port, username, password))

    async def set_rtsp(
        self,
        enabled: bool,
        *,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        definition = self.catalog.get("rtsp_set")
        definition.require_permission(experimental=experimental, confirm=confirm)

        if port is None or username is None or password is None:
            current = await self._get_rtsp_payload()
            port = current.get("rtspport") if port is None else port
            username = current.get("rtspuser") if username is None else username
            password = current.get("rtsppwd") if password is None else password
        path = build_rtsp_set_path(enabled, port, username, password)
        return await self._send_service_change(
            path,
            "RTSP configuration",
            experimental=experimental,
            confirm=confirm,
        )

    async def get_onvif_settings(self) -> dict:
        payload = parse_vstarcam_response(await self.send_raw_cgi("/get_onvif.cgi"))
        return parse_onvif_status(payload)

    def format_camera_account_password_set_cgi(
        self,
        password: str,
        *,
        username: str | None = None,
    ) -> str:
        username = self.config.username if username is None else username
        return self.format_raw_cgi(build_camera_account_password_set_path(username, password))

    async def set_camera_account_password(
        self,
        password: str,
        *,
        username: str | None = None,
        experimental: bool = False,
        confirm: bool = False,
        recovery_ready: bool = False,
    ) -> object:
        """Send the external/WebPwd change once and never retry it."""

        definition = self.catalog.get("camera_account_password_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        if not recovery_ready:
            raise ConfirmationRequiredError(
                "camera_account_password_set: pass --recovery-ready only after "
                "verifying physical reset/recovery"
            )

        username = self.config.username if username is None else username
        validate_camera_account_password(username, password)
        try:
            params = await self.get_params()
            if params.get("user3_name") != username or not isinstance(params.get("WebPwd"), str):
                raise AccountConfigurationError(
                    "camera WebPwd metadata does not match the configured username; "
                    "refusing a potentially locking password write"
                )

            path = build_camera_account_password_set_path(username, password)
            try:
                response = await self.send_raw_cgi(
                    path,
                    experimental=experimental,
                    confirm=confirm,
                    recovery_ready=True,
                    retry_requests=False,
                )
            except (asyncio.TimeoutError, TimeoutError, OSError, TransportError) as exc:
                raise AccountChangeUncertainError(
                    "camera-account password was sent once without retry, but no "
                    "acknowledgement arrived; verify before attempting another write"
                ) from exc
            return parse_vstarcam_response(response)
        finally:
            await self.transport.close()

    def format_onvif_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_onvif_set_path(enabled))

    async def set_onvif(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        definition = self.catalog.get("onvif_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        return await self._send_service_change(
            build_onvif_set_path(enabled),
            "ONVIF configuration",
            experimental=experimental,
            confirm=confirm,
        )

    async def _get_record_payload(self) -> dict:
        return parse_vstarcam_response(await self.send_raw_cgi("/get_record.cgi"))

    async def _get_camera_params_payload(self) -> dict:
        return parse_vstarcam_response(await self.send_raw_cgi("/get_camera_params.cgi"))

    async def get_audio_settings(self) -> dict:
        params = await self.get_params()
        record = await self._get_record_payload()
        camera_params = await self._get_camera_params_payload()
        status = await self.get_status()
        return parse_audio_status(params, record, camera_params, status)

    def format_record_audio_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_record_audio_set_path(enabled))

    async def set_record_audio(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        definition = self.catalog.get("record_audio_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        return await self._send_service_change(
            build_record_audio_set_path(enabled),
            "record-audio configuration",
            experimental=experimental,
            confirm=confirm,
        )

    def format_audio_volume_set_cgi(self, target: AudioVolumeTarget, level: int) -> str:
        return self.format_raw_cgi(build_audio_volume_set_path(target, level))

    async def set_audio_volume(
        self,
        target: AudioVolumeTarget,
        level: int,
        *,
        experimental: bool = False,
    ) -> object:
        path = build_audio_volume_set_path(target, level)
        definition = self.catalog.get(f"{target}_volume_set")
        definition.require_permission(experimental=experimental, confirm=False)
        return await self._send_service_change(
            path,
            f"{target} volume configuration",
            experimental=experimental,
            confirm=False,
        )

    async def send_talk_audio(
        self,
        frames: AsyncIterable[bytes],
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Send paced half-duplex ADPCM with a same-session livestream.

        Firmware without EchoCancellationVer uses the ADPCM channel-3
        path. A PPPP livestream is started on the same session before audio;
        its inverse stop is always attempted after bounded talk.
        """

        for name in (
            "audio_talk_adpcm_with_livestream",
            "pppp_livestream_start",
            "pppp_livestream_stop",
        ):
            self.catalog.get(name).require_permission(experimental=experimental, confirm=confirm)

        status = await self.get_status()
        if "EchoCancellationVer" in status and capability_enabled(status["EchoCancellationVer"]):
            raise MediaConfigurationError(
                "camera reports full-duplex audio; direct half-duplex ADPCM is not applicable"
            )
        g711_value = status.get("support_g711a", status.get("support_audio_g711a"))
        if g711_value is not None and capability_enabled(g711_value):
            raise MediaConfigurationError(
                "camera reports G.711 talk; direct ADPCM is not applicable"
            )

        frame_count = 0
        iterator = frames.__aiter__()
        primary_error: BaseException | None = None
        stream_attempted = False
        start_response: object | None = None
        stop_response: object | None = None
        try:
            try:
                first_frame = await iterator.__anext__()
            except StopAsyncIteration as exc:
                raise MediaStreamError("talk input produced no audio") from exc
            validate_adpcm_talk_frame(first_frame)
            if not self.connected:
                await self.connect()
            stream_attempted = True
            start_response = await self._send_service_change(
                build_pppp_livestream_path(enabled=True, substream=0),
                "PPPP livestream prerequisite start",
                experimental=experimental,
                confirm=confirm,
            )
            talk_started = asyncio.get_running_loop().time()
            await self.transport.send_channel_data(
                TALK_CHANNEL,
                first_frame,
                timeout=self.config.timeout,
            )
            frame_count += 1
            async for frame in iterator:
                validate_adpcm_talk_frame(frame)
                deadline = talk_started + frame_count * ADPCM_FRAME_DURATION
                delay = deadline - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.transport.send_channel_data(
                    TALK_CHANNEL,
                    frame,
                    timeout=self.config.timeout,
                )
                frame_count += 1
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except BaseException as exc:
                    if primary_error is None:
                        cleanup_error = exc
                    else:
                        LOGGER.warning("talk input cleanup failed", exc_info=True)
            if stream_attempted:
                try:
                    stop_response = await self._send_service_change(
                        build_pppp_livestream_path(enabled=False),
                        "PPPP livestream prerequisite stop",
                        experimental=experimental,
                        confirm=confirm,
                    )
                except BaseException as exc:
                    if primary_error is None:
                        if cleanup_error is not None:
                            LOGGER.warning(
                                "talk input cleanup also failed before livestream stop failure: %s",
                                cleanup_error,
                            )
                        cleanup_error = exc
                    else:
                        LOGGER.warning(
                            "PPPP livestream cleanup stop failed after talk error",
                            exc_info=True,
                        )
            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

        return {
            "frames_sent": frame_count,
            "audio_seconds": round(frame_count * ADPCM_FRAME_DURATION, 3),
            "codec": "ima_adpcm",
            "full_duplex": False,
            "livestream_start_response": start_response,
            "livestream_stop_response": stop_response,
        }

    async def get_motion_detection_settings(self) -> dict:
        return parse_motion_detection_status(await self.get_params())

    async def get_time_settings(self) -> dict:
        return parse_time_settings(await self.get_params())

    def format_time_settings_set_cgi(
        self,
        *,
        timezone_offset_seconds: int,
        ntp_enabled: bool,
        ntp_server: str,
        unix_time: int,
    ) -> str:
        return self.format_raw_cgi(
            build_time_settings_set_path(
                timezone_offset_seconds,
                ntp_enabled,
                ntp_server,
                unix_time,
            )
        )

    async def set_time_settings(
        self,
        *,
        timezone_offset_seconds: int | None = None,
        ntp_enabled: bool | None = None,
        ntp_server: str | None = None,
        unix_time: int | None = None,
        experimental: bool = False,
    ) -> object:
        """Preserve omitted fields and send the confirmed datetime write once."""

        definition = self.catalog.get("datetime_set")
        definition.require_permission(experimental=experimental, confirm=False)

        params = None
        if timezone_offset_seconds is None or ntp_enabled is None or ntp_server is None:
            params = await self.get_params()

        if timezone_offset_seconds is None:
            raw_timezone = params.get("tz") if params is not None else None
            if raw_timezone is None:
                raise TimeConfigurationError(
                    "camera did not report tz; pass timezone_offset_seconds explicitly"
                )
            try:
                timezone_offset_seconds = -int(raw_timezone)
            except (TypeError, ValueError) as exc:
                raise TimeConfigurationError("camera reported an invalid tz value") from exc
        timezone_offset_seconds = validate_utc_offset(timezone_offset_seconds)

        if ntp_enabled is None:
            raw_enabled = params.get("ntp_enable") if params is not None else None
            if raw_enabled not in (0, 1, "0", "1", False, True):
                raise TimeConfigurationError(
                    "camera did not report ntp_enable; pass ntp_enabled explicitly"
                )
            ntp_enabled = bool(int(raw_enabled))
        if not isinstance(ntp_enabled, bool):
            raise TimeConfigurationError("NTP enabled state must be boolean")

        if ntp_server is None:
            raw_server = params.get("ntp_svr") if params is not None else None
            if not isinstance(raw_server, str):
                raise TimeConfigurationError(
                    "camera did not report ntp_svr; pass ntp_server explicitly"
                )
            ntp_server = raw_server
        ntp_server = validate_ntp_server(ntp_server, enabled=ntp_enabled)

        if unix_time is None:
            unix_time = int(time.time())
        unix_time = validate_unix_time(unix_time)
        path = build_time_settings_set_path(
            timezone_offset_seconds,
            ntp_enabled,
            ntp_server,
            unix_time,
        )
        return await self._send_service_change(
            path,
            "time configuration",
            experimental=experimental,
            confirm=False,
        )

    async def get_night_vision_settings(self) -> dict:
        return parse_night_vision_status(await self._get_camera_params_payload())

    async def get_infrared_light_settings(self) -> dict:
        payload = parse_vstarcam_response(
            await self.send_raw_cgi("/trans_cmd_string.cgi?cmd=2120&command=1")
        )
        return parse_infrared_light_status(payload)

    def format_night_vision_set_cgi(self, mode: NightVisionMode) -> tuple[str, ...]:
        return tuple(self.format_raw_cgi(path) for path in build_night_vision_set_paths(mode))

    def format_infrared_light_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_infrared_light_set_path(enabled))

    async def set_night_vision(
        self,
        mode: NightVisionMode,
        *,
        experimental: bool = False,
    ) -> list[object]:
        """Send the documented two-step mode transition once per request."""

        paths = build_night_vision_set_paths(mode)
        for definition_name in (
            "night_vision_color_set",
            "night_vision_low_light_set",
        ):
            self.catalog.get(definition_name).require_permission(
                experimental=experimental, confirm=False
            )

        responses = []
        for path in paths:
            responses.append(
                await self._send_service_change(
                    path,
                    f"night-vision transition to {mode}",
                    experimental=experimental,
                    confirm=False,
                )
            )
        return responses

    async def set_infrared_light(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
    ) -> object:
        definition = self.catalog.get("infrared_light_set")
        definition.require_permission(experimental=experimental, confirm=False)
        return await self._send_service_change(
            build_infrared_light_set_path(enabled),
            "infrared-light configuration",
            experimental=experimental,
            confirm=False,
        )

    def format_motion_detection_set_cgi(
        self,
        enabled: bool,
        sensitivity: int,
        *,
        alarm_audio_enabled: bool,
    ) -> str:
        return self.format_raw_cgi(
            build_motion_detection_set_path(
                enabled,
                sensitivity,
                alarm_audio_enabled=alarm_audio_enabled,
            )
        )

    async def set_motion_detection(
        self,
        enabled: bool,
        *,
        sensitivity: int | None = None,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        """Preserve adjacent alarm fields and send the candidate exactly once."""

        definition = self.catalog.get("motion_detection_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        params = await self.get_params()
        current = parse_motion_detection_status(params)
        if sensitivity is None:
            if "sensitivity" not in current:
                raise DetectionConfigurationError(
                    "camera did not report alarm_motion_sensitivity; pass it explicitly"
                )
            sensitivity = current["sensitivity"]
        alarm_audio = params.get("alarm_audio")
        if alarm_audio not in (0, 1, "0", "1", False, True):
            raise DetectionConfigurationError(
                "camera did not report a boolean alarm_audio value; refusing to overwrite it"
            )
        path = build_motion_detection_set_path(
            enabled,
            sensitivity,
            alarm_audio_enabled=bool(int(alarm_audio)),
        )
        return await self._send_service_change(
            path,
            "motion-detection configuration",
            experimental=experimental,
            confirm=confirm,
        )

    async def get_human_detection_settings(
        self,
        *,
        include_tracking: bool = False,
        experimental: bool = False,
    ) -> dict:
        tracking_definition = None
        if include_tracking:
            tracking_definition = self.catalog.get("human_tracking_status")
            if tracking_definition.is_experimental and not experimental:
                raise ExperimentalCommandError(
                    "human_tracking_status: experimental/unconfirmed command; "
                    "pass --experimental to test it"
                )
        detection = parse_vstarcam_response(
            await self.send_raw_cgi("/trans_cmd_string.cgi?cmd=2126&command=1")
        )
        tracking = None
        if tracking_definition is not None:
            tracking = parse_vstarcam_response(
                await self.send_raw_cgi(tracking_definition.path, experimental=experimental)
            )
        return parse_human_detection_status(detection, tracking_payload=tracking)

    def format_human_detection_set_cgi(
        self,
        enabled: bool,
        *,
        sensitivity: int,
        distance: int,
    ) -> str:
        return self.format_raw_cgi(build_human_detection_set_path(enabled, sensitivity, distance))

    def format_human_sensitivity_set_cgi(self, sensitivity: int) -> str:
        return self.format_raw_cgi(build_human_sensitivity_set_path(sensitivity))

    def format_human_frame_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_human_frame_set_path(enabled))

    def format_human_tracking_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_human_tracking_set_path(enabled))

    def format_human_zoom_tracking_set_cgi(self, enabled: bool) -> str:
        return self.format_raw_cgi(build_human_zoom_tracking_set_path(enabled))

    async def set_human_detection(
        self,
        enabled: bool,
        *,
        sensitivity: int,
        distance: int,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        return await self._send_detection_change(
            "human_detection_set",
            build_human_detection_set_path(enabled, sensitivity, distance),
            experimental=experimental,
            confirm=confirm,
        )

    async def set_human_sensitivity(
        self,
        sensitivity: int,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        return await self._send_detection_change(
            "human_sensitivity_set",
            build_human_sensitivity_set_path(sensitivity),
            experimental=experimental,
            confirm=confirm,
        )

    async def set_human_frame(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        return await self._send_detection_change(
            "human_frame_set",
            build_human_frame_set_path(enabled),
            experimental=experimental,
            confirm=confirm,
        )

    async def set_human_tracking(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        return await self._send_detection_change(
            "human_tracking_set",
            build_human_tracking_set_path(enabled),
            experimental=experimental,
            confirm=confirm,
        )

    async def set_human_zoom_tracking(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> object:
        return await self._send_detection_change(
            "human_zoom_tracking_set",
            build_human_zoom_tracking_set_path(enabled),
            experimental=experimental,
            confirm=confirm,
        )

    async def _send_detection_change(
        self,
        definition_name: str,
        path: str,
        *,
        experimental: bool,
        confirm: bool,
    ) -> object:
        definition = self.catalog.get(definition_name)
        definition.require_permission(experimental=experimental, confirm=confirm)
        return await self._send_service_change(
            path,
            definition_name.replace("_", " "),
            experimental=experimental,
            confirm=confirm,
        )

    async def get_wifi_status(self) -> dict:
        return extract_wifi_status(await self.get_params())

    async def scan_wifi(self, *, experimental: bool = False) -> dict:
        definition = self.catalog.get("wifi_scan")
        if definition.is_experimental and not experimental:
            raise ExperimentalCommandError(
                "wifi_scan: experimental/unconfirmed command; pass --experimental to test it"
            )
        response = parse_vstarcam_response(
            await self.send_raw_cgi(definition.path, experimental=experimental)
        )
        return {"networks": normalize_wifi_scan(response), "response": response}

    def format_wifi_set_cgi(
        self,
        ssid: str,
        password: str,
        *,
        channel: int,
        auth_type: int,
    ) -> str:
        return self.format_raw_cgi(build_wifi_set_path(ssid, password, channel, auth_type))

    async def set_wifi(
        self,
        ssid: str,
        password: str,
        *,
        channel: int | None = None,
        auth_type: int | None = None,
        experimental: bool = False,
        confirm: bool = False,
        recovery_ready: bool = False,
    ) -> object:
        """Send the unconfirmed Wi-Fi candidate once and never retry it."""

        definition = self.catalog.get("wifi_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        if not recovery_ready:
            raise ConfirmationRequiredError(
                "wifi_set: pass --recovery-ready only after verifying physical reset/recovery"
            )
        validate_wifi_credentials(ssid, password)

        try:
            if channel is None or auth_type is None:
                scan = await self.scan_wifi(experimental=experimental)
                channel, auth_type = complete_wifi_metadata(
                    scan["networks"], ssid, channel, auth_type
                )
            path = build_wifi_set_path(ssid, password, channel, auth_type)

            if not self.connected:
                await self.connect()
            try:
                response = await self.send_raw_cgi(
                    path,
                    experimental=experimental,
                    confirm=confirm,
                    recovery_ready=True,
                    retry_requests=False,
                )
            except (asyncio.TimeoutError, TimeoutError, OSError, TransportError) as exc:
                raise WifiChangeUncertainError(
                    "Wi-Fi command was sent once without retry, but no acknowledgement arrived; "
                    "its outcome is unknown, so use the recovery plan instead of resending it"
                ) from exc
            return parse_vstarcam_response(response)
        finally:
            await self.transport.close()

    async def set_siren(self, enabled: bool, *, experimental: bool = False) -> object:
        definition = self.catalog.get("siren_on" if enabled else "siren_off")
        return parse_vstarcam_response(
            await self.send_raw_cgi(definition.path, experimental=experimental)
        )

    async def set_light(self, enabled: bool, *, experimental: bool = False) -> object:
        definition = self.catalog.get("white_light_on" if enabled else "white_light_off")
        return parse_vstarcam_response(
            await self.send_raw_cgi(definition.path, experimental=experimental)
        )
