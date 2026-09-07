"""High-level asynchronous API for VStarcam-compatible cameras."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterable, Awaitable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Never, Self, TypeVar
from urllib.parse import urlencode

from ._capabilities import capability_state
from .account import (
    CAMERA_REBOOT_PATH,
    build_camera_account_password_set_path,
    build_camera_account_plaintext_enable_path,
    build_camera_owner_set_path,
    validate_account_step_response,
)
from .actuator_state import (
    ACTUATOR_STATUS_PATH,
    build_alarm_led_set_path,
    parse_actuator_set_response,
    parse_alarm_led_set_response,
    parse_alarm_led_state,
    parse_light_state,
    parse_siren_state,
)
from .audio_talk import (
    ADPCM_FRAME_DURATION,
    TALK_CHANNEL,
    AdpcmTalkFrame,
    validate_adpcm_talk_frame,
)
from .cgi import append_auth, build_login_status_path, format_get_request, validate_raw_path
from .command_catalog import CommandCatalog, CommandDefinition
from .config import VStarcamConfig
from .detection import (
    MOTION_REGIONS_STATUS_PATH,
    build_human_detection_set_path,
    build_human_frame_set_path,
    build_human_sensitivity_set_path,
    build_human_tracking_set_path,
    build_human_zoom_tracking_set_path,
    build_motion_detection_set_path,
    parse_human_detection_status,
    parse_human_frame_set_response,
    parse_human_sensitivity_set_response,
    parse_motion_detection_regions,
    parse_motion_detection_status,
)
from .device_info import parse_device_software_info
from .errors import (
    AccountChangeCancelledError,
    AccountChangeUncertainError,
    AccountConfigurationError,
    ActuatorStateConfigurationError,
    CapabilityUnavailableError,
    ConfirmationRequiredError,
    DetectionConfigurationError,
    ExperimentalCommandError,
    MediaConfigurationError,
    MediaStreamError,
    NightVisionConfigurationError,
    PTZConfigurationError,
    ResponseParseError,
    ServiceChangeCancelledError,
    ServiceChangeUncertainError,
    TimeConfigurationError,
    TransportAuthenticationError,
    TransportCommandCancelledError,
    TransportError,
    TransportTimeoutError,
    TransportUnavailableError,
    VStarcamError,
    WifiChangeCancelledError,
    WifiChangeUncertainError,
)
from .imaging import CAMERA_PARAMS_PATH, parse_image_adjustments
from .media import (
    AUDIO_VOLUME_MAX,
    AUDIO_VOLUME_MIN,
    AudioVolumeTarget,
    build_audio_volume_set_path,
    build_onvif_set_path,
    build_pppp_livestream_path,
    build_record_audio_set_path,
    build_rtsp_set_path,
    parse_audio_status,
    parse_audio_volume_set_response,
    parse_livestream_set_response,
    parse_onvif_status,
    parse_rtsp_status,
)
from .media_stream import (
    RTSPStream,
    RTSPTransport,
    StreamQuality,
    capture_h264_snapshot,
    prepare_media_output,
)
from .night_vision import (
    NightVisionMode,
    build_infrared_light_set_path,
    build_night_vision_set_paths,
    parse_infrared_light_set_response,
    parse_infrared_light_status,
    parse_night_vision_set_response,
    parse_night_vision_status,
)
from .osd import (
    LOGO_OSD_STATUS_PATH,
    OSD_12H_STATUS_PATH,
    build_logo_osd_set_path,
    build_osd_12h_set_path,
    parse_logo_osd_set_response,
    parse_logo_osd_status,
    parse_osd_12h_set_response,
    parse_osd_12h_status,
    parse_timestamp_osd_status,
)
from .parser import parse_vstarcam_response
from .ptz import (
    PTZDirection,
    build_ptz_start_path,
    build_ptz_stop_path,
    parse_ptz_response,
    validate_ptz_duration,
)
from .secrets import mask_identifier, mask_secrets
from .time_settings import (
    build_time_settings_set_path,
    parse_time_settings,
    parse_time_settings_set_response,
)
from .transport import CameraTransport, DiscoveredCamera
from .wifi import (
    build_wifi_set_path,
    complete_wifi_metadata,
    extract_wifi_status,
    parse_wifi_scan_response,
    validate_wifi_credentials,
)

LOGGER = logging.getLogger(__name__)
_TRANSPORT_FAILURES = (OSError, TransportError)
_DOMAIN_CANCELLATIONS = (
    AccountChangeCancelledError,
    TransportCommandCancelledError,
    ServiceChangeCancelledError,
    WifiChangeCancelledError,
)
_CleanupResult = TypeVar("_CleanupResult")


@contextmanager
def _service_acknowledgement(operation: str, getter: str) -> Iterator[None]:
    """A rejected acknowledgement after a write means an uncertain outcome."""

    try:
        yield
    except VStarcamError as exc:
        raise ServiceChangeUncertainError(
            f"{operation} was sent once, but its acknowledgement was invalid; "
            f"the outcome is unknown and must be checked with the {getter} "
            "before another write"
        ) from exc


def _discard_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _talk_capability_enabled(
    status: Mapping[str, object],
    fields: tuple[str, ...],
) -> bool:
    """Read an SDK talk selector without treating malformed data as disabled."""

    for field in fields:
        if field not in status:
            continue
        state = capability_state(status, (field,))
        if state == "unknown":
            raise MediaConfigurationError(f"{field} talk capability is malformed")
        if state == "supported":
            return True
    return False


async def _await_cancellation_safe_cleanup(
    cleanup: Awaitable[_CleanupResult],
    *,
    operation: str,
) -> _CleanupResult:
    cleanup_task = asyncio.ensure_future(cleanup)
    cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            if cleanup_task.done() and not (current_task is not None and current_task.cancelling()):
                # shield reports a child task's CancelledError as a fresh plain
                # CancelledError. Let result() below recover the original domain
                # subclass (for example ServiceChangeCancelledError).
                break
            if cancellation is None:
                cancellation = exc
        except BaseException:
            break
    try:
        result = cleanup_task.result()
    except BaseException as cleanup_error:
        if cancellation is None:
            raise
        if isinstance(cleanup_error, _DOMAIN_CANCELLATIONS):
            raise cleanup_error from cancellation
        LOGGER.warning("%s failed after cancellation", operation, exc_info=True)
        raise cancellation
    if cancellation is not None:
        raise cancellation
    return result


class VStarcamCamera:
    @classmethod
    def from_discovery(
        cls,
        discovered: DiscoveredCamera,
        *,
        password: str,
        username: str = "admin",
        psk: str | None = None,
        source_address: str | None = None,
        discovery_port: int = 32108,
        timeout: float = 8.0,
        retries: int = 1,
    ) -> Self:
        if discovered.protocol != "binary":
            raise TransportUnavailableError(
                f"discovered camera uses {discovered.protocol!r} protocol; "
                "raw VStarcam CGI framing requires binary protocol"
            )
        transport_psk = psk if psk is not None else discovered.psk
        config = VStarcamConfig(
            host=discovered.host,
            source_address=source_address,
            device_id=discovered.device_id,
            username=username,
            password=password,
            psk=transport_psk,
            udp_port=discovered.port,
            discovery_port=discovery_port,
            auth_mode="basic",
            timeout=timeout,
            retries=retries,
        )
        config.validate(require_target=True, require_auth=True)
        return cls(config)

    def __init__(
        self,
        config: VStarcamConfig,
        *,
        transport: CameraTransport | None = None,
    ):
        self.config = config
        self.catalog = CommandCatalog.load()
        if transport is None:
            from .transport_aiopppp import AioppppTransport

            transport = AioppppTransport(config)
        self.transport = transport
        self._account_lock = asyncio.Lock()
        self._livestream_lock = asyncio.Lock()
        self._night_vision_lock = asyncio.Lock()
        self._ptz_move_lock = asyncio.Lock()
        self._time_lock = asyncio.Lock()
        self._wifi_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.transport.connected

    async def connect(self) -> None:
        self.config.validate(require_target=True, require_auth=True)
        LOGGER.debug(
            "connecting transport=%s port=%d device_id=%s auth_mode=%s",
            self.transport.name,
            self.config.udp_port,
            mask_identifier(self.config.device_id),
            self.config.auth_mode,
        )
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            try:
                await self.transport.connect()
                return
            except _TRANSPORT_FAILURES as exc:
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

    async def _request(
        self,
        command: str,
        *,
        retry: bool,
        connect_if_needed: bool = True,
    ) -> str:
        if not self.connected:
            if not connect_if_needed:
                raise TransportError("the existing PPPP session is no longer connected")
            await self.connect()
        attempts = self.config.retries + 1 if retry else 1
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
            except asyncio.CancelledError:
                raise
            except TransportAuthenticationError:
                raise
            except _TRANSPORT_FAILURES as exc:
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
        if (
            definition is not None
            and definition.confirmation_status != "dangerous_do_not_run"
            and (
                not definition.raw_accessible
                or (definition.read_or_write == "write" and "{" in definition.path)
            )
        ):
            raise ExperimentalCommandError(
                f"{definition.name}: available only through its guarded high-level API"
            )
        return await self._send_raw_path(
            path,
            definition,
            experimental=experimental,
            confirm=confirm,
            recovery_ready=recovery_ready,
            retry_requests=retry_requests,
        )

    async def _send_raw_path(
        self,
        path: str,
        definition: CommandDefinition | None,
        *,
        experimental: bool,
        confirm: bool,
        recovery_ready: bool,
        retry_requests: bool,
        connect_if_needed: bool = True,
    ) -> str:
        if definition is None and not experimental:
            raise ExperimentalCommandError(
                "unknown raw CGI command; inspect it first and pass --experimental to send"
            )
        if definition is None and not confirm:
            raise ConfirmationRequiredError(
                "unknown raw CGI command: pass --confirm after reviewing its write risk"
            )
        if definition is None:
            retry_requests = False
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
        command = format_get_request(path, self.config)
        LOGGER.debug("command> %s", mask_secrets(command))
        return await self._request(
            command,
            retry=retry_requests,
            connect_if_needed=connect_if_needed,
        )

    async def _send_cgi(
        self,
        path: str,
        *,
        experimental: bool = False,
        confirm: bool = False,
        recovery_ready: bool = False,
        retry_requests: bool = True,
        connect_if_needed: bool = True,
    ) -> dict:
        path = validate_raw_path(path)
        response = await self._send_raw_path(
            path,
            self.catalog.match_path(path),
            experimental=experimental,
            confirm=confirm,
            recovery_ready=recovery_ready,
            retry_requests=retry_requests,
            connect_if_needed=connect_if_needed,
        )
        return parse_vstarcam_response(response)

    async def get_status(self) -> dict:
        query = urlencode({"vuid": self.config.device_id}) if self.config.device_id else ""
        path = f"/get_status.cgi?{query}" if query else "/get_status.cgi"
        return await self._send_cgi(path)

    async def get_device_software_info(self) -> dict:
        return parse_device_software_info(await self._get_login_status())

    async def _get_login_status(self) -> dict:
        return await self._send_cgi(build_login_status_path(self.config.username))

    async def get_params(self) -> dict:
        return await self._send_cgi("/get_params.cgi")

    async def _send_service_change(
        self,
        path: str,
        operation: str,
        *,
        experimental: bool,
        confirm: bool,
        require_existing_session: bool = False,
    ) -> dict:
        definition = self.catalog.match_path(path)
        if definition is not None:
            definition.require_permission(experimental=experimental, confirm=confirm)
        if not self.connected:
            if require_existing_session:
                raise TransportError(
                    f"cannot send {operation}: the original PPPP session is disconnected"
                )
            await self.connect()
        try:
            return await self._send_cgi(
                path,
                experimental=experimental,
                confirm=confirm,
                retry_requests=False,
                connect_if_needed=not require_existing_session,
            )
        except TransportCommandCancelledError as exc:
            raise ServiceChangeCancelledError(
                f"{operation} was interrupted after its one-shot send may have started; "
                "the outcome is unknown and must be verified before another write"
            ) from exc
        except _TRANSPORT_FAILURES as exc:
            raise ServiceChangeUncertainError(
                f"{operation} was sent once without retry, but no acknowledgement arrived; "
                "the outcome is unknown and must be verified before another write"
            ) from exc
        except ResponseParseError as exc:
            raise ServiceChangeUncertainError(
                f"{operation} was sent once without retry, but its acknowledgement "
                "was malformed; the outcome is unknown and must be verified before "
                "another write"
            ) from exc

    async def _require_command_applicable(self, definition_name: str) -> None:
        """Reject a model-specific operation only when firmware explicitly says no."""

        definition = self.catalog.get(definition_name)
        if not definition.capability_flags:
            return
        status = await self._get_login_status()
        if capability_state(status, definition.capability_flags) == "unsupported":
            raise CapabilityUnavailableError(
                f"{definition.name}: camera reports this feature as unsupported "
                f"({', '.join(definition.capability_flags)})"
            )

    async def get_rtsp_settings(self) -> dict:
        payload = await self._send_cgi("/get_rtsp.cgi")
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
        """Resolve an RTSP stream descriptor without exposing its credentials.

        When ``port`` is omitted, the RTSP settings getter supplies the
        reported port and credential source. An explicit port skips that PPPP
        lookup and permits RTSP-only use. Use ``probe_rtsp`` to establish
        listener and media availability.
        """

        if (username is None) != (password is None):
            raise MediaConfigurationError("RTSP username and password must be provided together")

        settings: dict | None = None
        params: dict | None = None
        if port is None:
            payload = await self._send_cgi("/get_rtsp.cgi")
            params = await self.get_params()
            settings = parse_rtsp_status(payload, params)
            port = settings["reported_port"]
            if settings["configured_enabled"] is False and port == 0:
                raise MediaConfigurationError(
                    "RTSP is disabled and the camera reports no usable port; "
                    "configure RTSP before requesting a stream"
                )

        if username is None and password is None:
            if settings is None:
                raise MediaConfigurationError(
                    "an explicit RTSP port also requires an explicit RTSP username and password"
                )
            if settings["authentication_enabled"] is None:
                raise MediaConfigurationError(
                    "RTSP authentication state is unknown; provide both RTSP username "
                    "and password explicitly"
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

    async def set_rtsp(
        self,
        enabled: bool,
        *,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        definition = self.catalog.get("rtsp_set")
        definition.require_permission(experimental=experimental, confirm=confirm)

        if port is None or username is None or password is None:
            current = await self._send_cgi("/get_rtsp.cgi")
            if port is None:
                port = current.get("rtspport")
                if enabled and (
                    not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
                ):
                    raise MediaConfigurationError(
                        "camera reports no usable RTSP port; provide an explicit port"
                    )
            username = current.get("rtspuser") if username is None else username
            password = current.get("rtsppwd") if password is None else password
        path = build_rtsp_set_path(enabled, port, username, password)
        await self._send_service_change(
            path,
            "RTSP configuration",
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "RTSP configuration was sent once and a response arrived, but its exact "
            "response command code is not proven; the outcome is unknown and must be "
            "checked with the RTSP getter before another write"
        )

    async def get_onvif_settings(self) -> dict:
        payload = await self._send_cgi("/get_onvif.cgi")
        return parse_onvif_status(payload)

    async def set_camera_account_password(
        self,
        password: str,
        *,
        username: str | None = None,
        experimental: bool = False,
        confirm: bool = False,
        recovery_ready: bool = False,
    ) -> dict:
        """Enable or change external/WebPwd access and verify a fresh session."""

        async with self._account_lock:
            return await self._set_camera_account_password(
                password,
                username=username,
                experimental=experimental,
                confirm=confirm,
                recovery_ready=recovery_ready,
            )

    async def _set_camera_account_password(
        self,
        password: str,
        *,
        username: str | None,
        experimental: bool,
        confirm: bool,
        recovery_ready: bool,
    ) -> dict:
        definition = self.catalog.get("camera_account_password_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        if not recovery_ready:
            raise ConfirmationRequiredError(
                "camera_account_password_set: pass --recovery-ready only after "
                "verifying physical reset/recovery"
            )

        username = self.config.username if username is None else username
        password_path = build_camera_account_password_set_path(username, password)
        try:
            params = await self.get_params()
            current_web_password = params.get("WebPwd")
            if params.get("user3_name") != username or (
                "WebPwd" in params and not isinstance(current_web_password, str)
            ):
                raise AccountConfigurationError(
                    "camera WebPwd metadata does not match the configured username; "
                    "refusing a potentially locking password write"
                )
            status = await self._get_login_status() if not current_web_password else {}
        except BaseException:
            await _await_cancellation_safe_cleanup(
                self.transport.close(),
                operation="camera-account preflight session close",
            )
            raise

        async def change_and_verify() -> dict:
            response: dict | None = None
            write_error: Exception | None = None
            write_cancellation: TransportCommandCancelledError | None = None
            try:
                if not current_web_password:
                    response = await self._enable_camera_account_password(
                        status,
                        username,
                        password_path,
                        experimental=experimental,
                        confirm=confirm,
                    )
                else:
                    try:
                        payload = await self._send_cgi(
                            password_path,
                            experimental=experimental,
                            confirm=confirm,
                            recovery_ready=True,
                            retry_requests=False,
                        )
                        response = validate_account_step_response(
                            payload,
                            dual_authentication=2,
                        )
                    except TransportCommandCancelledError as exc:
                        write_cancellation = exc
                    except Exception as exc:
                        write_error = exc
            finally:
                await self.transport.close()

            try:
                verified = await self.get_params()
            except TransportCommandCancelledError as exc:
                raise AccountChangeCancelledError(
                    "camera-account verification was cancelled after the password write; "
                    "the outcome is unknown and the write must not be retried"
                ) from exc
            except Exception as exc:
                raise AccountChangeUncertainError(
                    "camera-account password was sent once, but its effect could not be "
                    "verified through a fresh session; do not retry"
                ) from exc
            finally:
                await self.transport.close()
            if verified.get("WebPwd") != password:
                if write_cancellation is not None:
                    raise AccountChangeCancelledError(
                        "camera-account password delivery was cancelled and the fresh getter "
                        "did not confirm its effect; the outcome is unknown and must not be retried"
                    ) from write_cancellation
                error = AccountChangeUncertainError(
                    "camera-account password was sent once, but get_params did not confirm "
                    "the requested WebPwd; do not retry"
                )
                if write_error is not None:
                    raise error from write_error
                raise error
            if write_cancellation is not None:
                raise AccountChangeCancelledError(
                    "camera-account password delivery was cancelled after send started; "
                    "the requested WebPwd was verified, but cancellation is preserved"
                ) from write_cancellation
            if write_error is not None:
                raise AccountChangeUncertainError(
                    "camera-account password took effect, but its acknowledgement was "
                    "missing or malformed; do not retry"
                ) from write_error
            assert response is not None
            return {
                **response,
                "web_password_verified": True,
            }

        return await _await_cancellation_safe_cleanup(
            change_and_verify(),
            operation="camera-account password change verification",
        )

    async def _enable_camera_account_password(
        self,
        status: dict,
        username: str,
        password_path: str,
        *,
        experimental: bool,
        confirm: bool,
    ) -> dict:
        if self.config.auth_mode != "observed" and self.config.account_id not in (None, "0"):
            raise AccountConfigurationError(
                "first WebPwd enable requires local account_id=0 or authorized observed account credentials"
            )

        dual_authentication = status.get("DualAuthentication")
        if type(dual_authentication) is not int or dual_authentication not in {0, 1, 2}:
            raise AccountConfigurationError(
                "camera did not report a supported DualAuthentication state"
            )

        async def send_step(path: str, expected_state: int) -> dict:
            try:
                payload = await self._send_cgi(
                    path,
                    experimental=experimental,
                    confirm=confirm,
                    recovery_ready=True,
                    retry_requests=False,
                    connect_if_needed=False,
                )
                return validate_account_step_response(
                    payload,
                    dual_authentication=expected_state,
                )
            except TransportCommandCancelledError as exc:
                raise AccountChangeCancelledError(
                    "camera-account enable step was cancelled after send started; its state "
                    "is unknown and the step must not be retried"
                ) from exc
            except Exception as exc:
                raise AccountChangeUncertainError(
                    "camera-account enable step was sent once, but its exact state "
                    "was not acknowledged; do not retry"
                ) from exc

        if dual_authentication == 0:
            await send_step(
                build_camera_owner_set_path(
                    self.config.account_id or "0",
                    (
                        self.config.login_hash
                        if self.config.auth_mode == "observed"
                        else self.config.password
                    )
                    or "",
                ),
                1,
            )
            try:
                owner_status = await self._send_cgi(
                    build_login_status_path(self.config.username),
                    retry_requests=False,
                    connect_if_needed=False,
                )
            except TransportCommandCancelledError as exc:
                raise AccountChangeCancelledError(
                    "camera-account owner-state verification was cancelled after the owner "
                    "write; the outcome is unknown and the write must not be retried"
                ) from exc
            except Exception as exc:
                raise AccountChangeUncertainError(
                    "camera accepted the owner step, but its state could not be read; do not retry"
                ) from exc
            owner_state = owner_status.get("DualAuthentication")
            if type(owner_state) is not int or owner_state != 1:
                raise AccountChangeUncertainError(
                    "camera accepted the owner step, but status did not confirm it; do not retry"
                )
            dual_authentication = 1

        if dual_authentication == 1:
            await send_step(build_camera_account_plaintext_enable_path(username), 2)

        response = await send_step(password_path, 2)
        try:
            reboot_response = await self._send_cgi(
                CAMERA_REBOOT_PATH,
                experimental=experimental,
                confirm=confirm,
                recovery_ready=True,
                retry_requests=False,
                connect_if_needed=False,
            )
            if reboot_response.get("result") != "ok":
                raise AccountConfigurationError("camera did not acknowledge its restart")
        except TransportCommandCancelledError as exc:
            raise AccountChangeCancelledError(
                "camera restart was cancelled after the account writes completed; the "
                "outcome is unknown and the password writes must not be repeated"
            ) from exc
        except Exception as exc:
            raise AccountChangeUncertainError(
                "camera account was enabled, but its required restart was not acknowledged; "
                "do not repeat the password writes"
            ) from exc
        return response

    async def set_onvif(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        await self._send_service_change(
            build_onvif_set_path(enabled),
            "ONVIF configuration",
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "ONVIF configuration was sent once and a response arrived, but its exact "
            "response command code and acknowledgement are not proven; the outcome is "
            "unknown and must be checked before another write"
        )

    async def get_audio_settings(self) -> dict:
        params = await self.get_params()
        record = await self._send_cgi("/get_record.cgi")
        camera_params = await self._send_cgi("/get_camera_params.cgi")
        status = await self._get_login_status()
        return parse_audio_status(params, record, camera_params, status)

    async def set_record_audio(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        await self._send_service_change(
            build_record_audio_set_path(enabled),
            "record-audio configuration",
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "record-audio configuration was sent once and a response arrived, but its "
            "exact response command code and acknowledgement are not proven; the outcome "
            "is unknown and must be checked with the audio getter before another write"
        )

    async def set_audio_volume(
        self,
        target: AudioVolumeTarget,
        level: int,
        *,
        experimental: bool = False,
    ) -> dict:
        path = build_audio_volume_set_path(target, level)
        payload = await self._send_service_change(
            path,
            f"{target} volume configuration",
            experimental=experimental,
            confirm=False,
        )
        with _service_acknowledgement(f"{target} volume configuration", "audio getter"):
            return parse_audio_volume_set_response(payload)

    async def _change_pppp_livestream(
        self,
        *,
        enabled: bool,
        substream: int = 0,
        operation: str,
        experimental: bool,
        confirm: bool,
    ) -> dict:
        payload = await self._send_service_change(
            build_pppp_livestream_path(enabled=enabled, substream=substream),
            operation,
            experimental=experimental,
            confirm=confirm,
            require_existing_session=True,
        )
        try:
            return parse_livestream_set_response(payload)
        except MediaConfigurationError as exc:
            raise ServiceChangeUncertainError(
                f"{operation} was sent once, but its acknowledgement was invalid; "
                "the livestream state is unknown"
            ) from exc

    async def send_talk_audio(
        self,
        frames: AsyncIterable[AdpcmTalkFrame],
        *,
        duration: float | None = None,
        max_speaker_volume: int | None = None,
        experimental: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Send paced half-duplex ADPCM with a same-session livestream.

        Firmware with absent or exact-zero EchoCancellationVer and G.711
        selectors uses the ADPCM channel-3 path. A PPPP livestream is started
        on the same session before audio; its inverse stop is always attempted
        after bounded talk.
        """

        for name in (
            "audio_talk_adpcm_with_livestream",
            "pppp_livestream_start",
            "pppp_livestream_stop",
        ):
            self.catalog.get(name).require_permission(experimental=experimental, confirm=confirm)

        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration < ADPCM_FRAME_DURATION
        ):
            raise MediaConfigurationError(
                f"talk duration must be finite and at least {ADPCM_FRAME_DURATION} seconds"
            )
        if (
            not isinstance(max_speaker_volume, int)
            or isinstance(max_speaker_volume, bool)
            or not AUDIO_VOLUME_MIN <= max_speaker_volume <= AUDIO_VOLUME_MAX
        ):
            raise MediaConfigurationError(
                f"talk max speaker volume must be an integer from "
                f"{AUDIO_VOLUME_MIN} to {AUDIO_VOLUME_MAX}"
            )

        status = await self._get_login_status()
        if _talk_capability_enabled(status, ("EchoCancellationVer",)):
            raise MediaConfigurationError(
                "camera reports full-duplex audio; direct half-duplex ADPCM is not applicable"
            )
        if _talk_capability_enabled(status, ("support_g711a", "support_audio_g711a")):
            raise MediaConfigurationError(
                "camera reports G.711 talk; direct ADPCM is not applicable"
            )
        camera_params = await self._send_cgi(CAMERA_PARAMS_PATH)
        audio_status = parse_audio_status({}, {}, camera_params)
        if not audio_status.get("speaker_volume_present"):
            raise MediaConfigurationError(
                "camera did not report a valid speaker volume; refusing audible talk"
            )
        speaker_volume = audio_status["speaker_volume"]
        if speaker_volume > max_speaker_volume:
            raise MediaConfigurationError(
                "camera speaker volume exceeds the caller-approved talk limit"
            )

        async with self._livestream_lock:
            if not self.connected:
                await self.connect()
            async with self.transport.session_lease():
                result = await self._send_talk_audio_on_session(
                    frames,
                    duration=duration,
                    experimental=experimental,
                    confirm=confirm,
                )
        return {
            **result,
            "speaker_volume": speaker_volume,
            "speaker_volume_limit": max_speaker_volume,
        }

    async def _send_talk_audio_on_session(
        self,
        frames: AsyncIterable[AdpcmTalkFrame],
        *,
        duration: float,
        experimental: bool,
        confirm: bool,
    ) -> dict:

        frame_count = 0
        iterator = frames.__aiter__()
        pending_source_task: asyncio.Task | None = None
        primary_error: BaseException | None = None
        stream_attempted = False
        start_response: dict | None = None
        stop_response: dict | None = None

        async def next_frame(timeout: float) -> AdpcmTalkFrame:
            nonlocal pending_source_task
            task = asyncio.create_task(iterator.__anext__())
            try:
                done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
            except BaseException:
                task.cancel()
                pending_source_task = task
                raise
            if not done:
                task.cancel()
                pending_source_task = task
                raise TimeoutError
            return task.result()

        try:
            try:
                first_frame = await next_frame(min(duration, self.config.timeout))
            except StopAsyncIteration as exc:
                raise MediaStreamError("talk input produced no audio") from exc
            except TimeoutError as exc:
                raise MediaStreamError("talk input did not produce audio before timeout") from exc
            validate_adpcm_talk_frame(first_frame)
            stream_attempted = True
            start_response = await self._change_pppp_livestream(
                enabled=True,
                substream=0,
                operation="PPPP livestream prerequisite start",
                experimental=experimental,
                confirm=confirm,
            )
            talk_started = asyncio.get_running_loop().time()
            talk_deadline = talk_started + duration
            max_frames = max(1, int(duration / ADPCM_FRAME_DURATION))

            async def send_frame(frame: AdpcmTalkFrame) -> None:
                remaining = talk_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ServiceChangeUncertainError(
                        "talk duration elapsed after livestream start; an acknowledged audio "
                        "prefix may have played"
                    )
                try:
                    await self.transport.send_channel_parts(
                        TALK_CHANNEL,
                        (frame.header, frame.payload),
                        timeout=min(self.config.timeout, remaining),
                    )
                except TransportCommandCancelledError as exc:
                    raise ServiceChangeCancelledError(
                        "talk audio delivery was cancelled after livestream start; an unknown "
                        "acknowledged prefix may have played"
                    ) from exc
                except Exception as exc:
                    raise ServiceChangeUncertainError(
                        "talk audio delivery failed after livestream start; an unknown "
                        "acknowledged prefix may have played"
                    ) from exc

            await send_frame(first_frame)
            frame_count += 1
            while frame_count < max_frames:
                remaining = talk_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    frame = await next_frame(remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise ServiceChangeUncertainError(
                        "talk input stalled after audio delivery began; the acknowledged "
                        "prefix may have played"
                    ) from exc
                except Exception as exc:
                    raise ServiceChangeUncertainError(
                        "talk input failed after audio delivery began; the acknowledged "
                        "prefix may have played"
                    ) from exc
                try:
                    validate_adpcm_talk_frame(frame)
                except MediaConfigurationError as exc:
                    raise ServiceChangeUncertainError(
                        "talk input became invalid after audio delivery began; the "
                        "acknowledged prefix may have played"
                    ) from exc
                deadline = talk_started + frame_count * ADPCM_FRAME_DURATION
                delay = deadline - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await send_frame(frame)
                frame_count += 1
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if stream_attempted:
                try:
                    stop_response = await _await_cancellation_safe_cleanup(
                        self._change_pppp_livestream(
                            enabled=False,
                            operation="PPPP livestream prerequisite stop",
                            experimental=experimental,
                            confirm=False,
                        ),
                        operation="PPPP livestream cleanup stop",
                    )
                except BaseException as exc:
                    if primary_error is None:
                        cleanup_error = exc
                    else:
                        LOGGER.warning(
                            "PPPP livestream cleanup stop failed after talk error",
                            exc_info=True,
                        )
            source_busy = False
            if pending_source_task is not None:
                try:
                    done, _ = await _await_cancellation_safe_cleanup(
                        asyncio.wait({pending_source_task}, timeout=self.config.timeout),
                        operation="talk pending source cancellation",
                    )
                    if done:
                        _discard_task_result(pending_source_task)
                    else:
                        source_busy = True
                        pending_source_task.add_done_callback(_discard_task_result)
                        LOGGER.warning("talk input did not stop within the cleanup timeout")
                except BaseException:
                    source_busy = True
                    pending_source_task.add_done_callback(_discard_task_result)
                    LOGGER.warning("talk input cancellation cleanup failed", exc_info=True)
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose) and not source_busy:

                async def close_source() -> None:
                    close_task = asyncio.create_task(aclose())
                    try:
                        done, _ = await asyncio.wait(
                            {close_task},
                            timeout=self.config.timeout,
                        )
                    except BaseException:
                        close_task.cancel()
                        close_task.add_done_callback(_discard_task_result)
                        raise
                    if not done:
                        close_task.cancel()
                        close_task.add_done_callback(_discard_task_result)
                        raise MediaStreamError(
                            "talk input cleanup did not finish within the request timeout"
                        )
                    try:
                        close_task.result()
                    except asyncio.CancelledError as exc:
                        raise MediaStreamError("talk input cleanup was cancelled") from exc
                    except Exception as exc:
                        raise MediaStreamError("talk input cleanup failed") from exc

                try:
                    await _await_cancellation_safe_cleanup(
                        close_source(),
                        operation="talk input cleanup",
                    )
                except BaseException as exc:
                    if primary_error is None and cleanup_error is None:
                        cleanup_error = exc
                    else:
                        LOGGER.warning("talk input cleanup failed", exc_info=True)
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

    async def capture_pppp_snapshot(
        self,
        output: str | Path,
        *,
        timeout: float | None = None,
        overwrite: bool = False,
        experimental: bool = False,
        executable: str = "ffmpeg",
    ) -> dict:
        """Capture one decoded image through the compatible PPPP video path."""

        for name in ("pppp_livestream_start", "pppp_livestream_stop"):
            self.catalog.get(name).require_permission(experimental=experimental, confirm=False)
        timeout = self.config.timeout if timeout is None else timeout
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise MediaConfigurationError("snapshot timeout must be finite and greater than zero")
        output_path = prepare_media_output(output, overwrite=overwrite)

        async with self._livestream_lock:
            if not self.connected:
                await self.connect()
            async with self.transport.session_lease():
                access_unit = await self._capture_pppp_keyframe_on_session(
                    timeout=timeout,
                    experimental=experimental,
                )
        return await capture_h264_snapshot(
            access_unit,
            output_path,
            timeout=timeout,
            overwrite=overwrite,
            executable=executable,
        )

    async def _capture_pppp_keyframe_on_session(
        self,
        *,
        timeout: float,
        experimental: bool,
    ) -> bytes:
        await self.transport.start_video_capture()
        start_attempted = False
        primary_error: BaseException | None = None
        access_unit: bytes | None = None
        try:
            start_attempted = True
            await self._change_pppp_livestream(
                enabled=True,
                substream=1,
                operation="PPPP snapshot livestream start",
                experimental=experimental,
                confirm=False,
            )
            access_unit = await self.transport.receive_video_keyframe(timeout=timeout)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if start_attempted:
                try:
                    await _await_cancellation_safe_cleanup(
                        self._change_pppp_livestream(
                            enabled=False,
                            operation="PPPP snapshot livestream stop",
                            experimental=experimental,
                            confirm=False,
                        ),
                        operation="PPPP snapshot livestream cleanup stop",
                    )
                except BaseException as exc:
                    if primary_error is None:
                        cleanup_error = exc
                    else:
                        LOGGER.warning(
                            "PPPP snapshot livestream stop failed after capture error",
                            exc_info=True,
                        )
            try:
                await _await_cancellation_safe_cleanup(
                    self.transport.stop_video_capture(),
                    operation="PPPP snapshot local capture disarm",
                )
            except BaseException as exc:
                if primary_error is None and cleanup_error is None:
                    cleanup_error = exc
                else:
                    LOGGER.warning("PPPP snapshot local disarm failed", exc_info=True)
            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

        if access_unit is None:
            raise AssertionError("PPPP snapshot completed without an access unit")
        return access_unit

    async def get_motion_detection_settings(self) -> dict:
        return parse_motion_detection_status(await self.get_params())

    async def get_motion_detection_regions(self) -> dict:
        await self._require_command_applicable("motion_regions_status")
        return parse_motion_detection_regions(await self._send_cgi(MOTION_REGIONS_STATUS_PATH))

    async def get_time_settings(self) -> dict:
        return parse_time_settings(await self.get_params())

    async def _require_osd_12h_available(self, definition: CommandDefinition) -> None:
        params = await self.get_params()
        if capability_state(params, definition.capability_flags) == "unsupported":
            raise CapabilityUnavailableError(
                f"{definition.name}: camera reports this feature as unsupported "
                f"({', '.join(definition.capability_flags)})"
            )

    async def get_osd_12h_mode(self) -> bool:
        definition = self.catalog.get("osd_12h_status")
        await self._require_osd_12h_available(definition)
        payload = await self._send_cgi(OSD_12H_STATUS_PATH)
        return parse_osd_12h_status(payload)

    async def get_timestamp_osd(self) -> bool:
        return parse_timestamp_osd_status(await self._get_login_status())

    async def set_osd_12h_mode(self, twelve_hour: bool) -> bool:
        path = build_osd_12h_set_path(twelve_hour)
        definition = self.catalog.get("osd_12h_set")
        await self._require_osd_12h_available(definition)
        payload = await self._send_service_change(
            path,
            "OSD clock-mode configuration",
            experimental=False,
            confirm=False,
        )
        with _service_acknowledgement("OSD clock-mode configuration", "OSD clock getter"):
            return parse_osd_12h_set_response(payload, twelve_hour)

    async def get_logo_osd(self) -> bool:
        definition = self.catalog.get("logo_osd_set")
        await self._require_command_applicable(definition.name)
        return parse_logo_osd_status(await self._send_cgi(LOGO_OSD_STATUS_PATH))

    async def set_logo_osd(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
    ) -> dict[str, int]:
        path = build_logo_osd_set_path(enabled)
        definition = self.catalog.get("logo_osd_set")
        definition.require_permission(experimental=experimental, confirm=False)
        await self._require_command_applicable(definition.name)
        parse_logo_osd_status(await self._send_cgi(LOGO_OSD_STATUS_PATH))
        payload = await self._send_service_change(
            path,
            "logo OSD configuration",
            experimental=experimental,
            confirm=False,
        )
        with _service_acknowledgement("logo OSD configuration", "logo OSD getter"):
            return parse_logo_osd_set_response(payload)

    async def set_time_settings(
        self,
        *,
        timezone_offset_seconds: int | None = None,
        ntp_enabled: bool | None = None,
        ntp_server: str | None = None,
        unix_time: int | None = None,
    ) -> dict:
        """Preserve omitted fields and send the confirmed datetime write once."""

        async with self._time_lock:
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
            if ntp_enabled is None:
                raw_enabled = params.get("ntp_enable") if params is not None else None
                if raw_enabled not in (0, 1, "0", "1", False, True):
                    raise TimeConfigurationError(
                        "camera did not report ntp_enable; pass ntp_enabled explicitly"
                    )
                ntp_enabled = bool(int(raw_enabled))

            if ntp_server is None:
                raw_server = params.get("ntp_svr") if params is not None else None
                if not isinstance(raw_server, str):
                    raise TimeConfigurationError(
                        "camera did not report ntp_svr; pass ntp_server explicitly"
                    )
                ntp_server = raw_server

            if unix_time is None:
                unix_time = int(time.time())
            path = build_time_settings_set_path(
                timezone_offset_seconds,
                ntp_enabled,
                ntp_server,
                unix_time,
            )
            payload = await self._send_service_change(
                path,
                "time configuration",
                experimental=False,
                confirm=False,
            )
            with _service_acknowledgement("time configuration", "time getter"):
                return parse_time_settings_set_response(payload)

    async def get_night_vision_settings(self) -> dict:
        return parse_night_vision_status(await self._send_cgi(CAMERA_PARAMS_PATH))

    async def get_image_adjustments(self) -> dict:
        return parse_image_adjustments(await self._send_cgi(CAMERA_PARAMS_PATH))

    async def get_infrared_light_settings(self) -> dict:
        payload = await self._send_cgi("/trans_cmd_string.cgi?cmd=2120&command=1")
        return parse_infrared_light_status(payload)

    async def get_siren_state(self) -> bool:
        payload = await self._send_cgi(ACTUATOR_STATUS_PATH)
        return parse_siren_state(payload)

    async def get_light_state(self) -> bool:
        await self._require_command_applicable("white_light_on")
        payload = await self._send_cgi(ACTUATOR_STATUS_PATH)
        return parse_light_state(payload)

    async def get_alarm_led(self) -> bool:
        payload = await self._send_cgi(ACTUATOR_STATUS_PATH)
        return parse_alarm_led_state(payload)

    async def set_night_vision(
        self,
        mode: NightVisionMode,
        *,
        experimental: bool = False,
    ) -> list[dict]:
        """Send the documented two-step mode transition once per request."""

        async with self._night_vision_lock:
            return await self._set_night_vision(mode, experimental=experimental)

    async def _set_night_vision(
        self,
        mode: NightVisionMode,
        *,
        experimental: bool,
    ) -> list[dict]:
        paths = build_night_vision_set_paths(mode)
        for definition_name in (
            "night_vision_color_set",
            "night_vision_low_light_set",
        ):
            self.catalog.get(definition_name).require_permission(
                experimental=experimental, confirm=False
            )
        await self._require_command_applicable("night_vision_color_set")

        responses = []
        try:
            for path in paths:
                payload = await self._send_service_change(
                    path,
                    f"night-vision transition to {mode}",
                    experimental=experimental,
                    confirm=False,
                )
                try:
                    responses.append(parse_night_vision_set_response(payload))
                except NightVisionConfigurationError as exc:
                    raise ServiceChangeUncertainError(
                        "a night-vision transition step was sent once, but its "
                        "acknowledgement was invalid; the outcome is unknown and must be "
                        "checked with the night-vision getter before another write"
                    ) from exc
        except asyncio.CancelledError as exc:
            if not responses or isinstance(exc, ServiceChangeCancelledError):
                raise
            raise ServiceChangeCancelledError(
                "night-vision transition was interrupted after one step completed; "
                "the resulting mode is unknown and must be verified before another write"
            ) from exc
        except _TRANSPORT_FAILURES as exc:
            if not responses or isinstance(exc, ServiceChangeUncertainError):
                raise
            raise ServiceChangeUncertainError(
                "night-vision transition failed after one step completed; "
                "the resulting mode is unknown and must be verified before another write"
            ) from exc
        return responses

    async def set_infrared_light(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
    ) -> dict[str, int]:
        payload = await self._send_service_change(
            build_infrared_light_set_path(enabled),
            "infrared-light configuration",
            experimental=experimental,
            confirm=False,
        )
        with _service_acknowledgement("infrared-light configuration", "infrared-light getter"):
            return parse_infrared_light_set_response(payload, enabled)

    async def set_alarm_led(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
    ) -> bool:
        path = build_alarm_led_set_path(enabled)
        payload = await self._send_service_change(
            path,
            "alarm-indicator configuration",
            experimental=experimental,
            confirm=False,
        )
        with _service_acknowledgement("alarm-indicator configuration", "alarm-indicator getter"):
            return parse_alarm_led_set_response(payload, enabled)

    async def set_motion_detection(
        self,
        enabled: bool,
        *,
        sensitivity: int | None = None,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        """Preserve adjacent alarm fields and send the candidate exactly once."""

        definition = self.catalog.get("motion_detection_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        params = await self.get_params()
        current = parse_motion_detection_status(params)
        if "enabled" not in current or "sensitivity" not in current:
            raise DetectionConfigurationError(
                "camera did not report the complete current motion state; refusing a "
                "write without a recoverable enabled/sensitivity pre-state"
            )
        if sensitivity is None:
            sensitivity = current["sensitivity"]
        path = build_motion_detection_set_path(
            enabled,
            sensitivity,
            params=params,
        )
        await self._send_service_change(
            path,
            "motion-detection configuration",
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "motion-detection configuration was sent once and a response arrived, but "
            "its exact acknowledgement contract is not proven; the outcome is unknown "
            "and must be checked with the motion getter before another write"
        )

    async def get_human_detection_settings(
        self,
        *,
        include_tracking: bool = False,
    ) -> dict:
        detection = await self._send_cgi("/trans_cmd_string.cgi?cmd=2126&command=1")
        tracking = None
        if include_tracking:
            tracking = await self._send_cgi(self.catalog.get("human_tracking_status").path)
        return parse_human_detection_status(detection, tracking_payload=tracking)

    async def set_human_detection(
        self,
        enabled: bool,
        *,
        sensitivity: int,
        distance: int,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        await self._send_detection_change(
            "human_detection_set",
            build_human_detection_set_path(enabled, sensitivity, distance),
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "main human-detection configuration was sent once and a response arrived, "
            "but its exact acknowledgement and inverse getter are not proven; the "
            "outcome is unknown"
        )

    async def set_human_sensitivity(self, sensitivity: int) -> dict:
        payload = await self._send_detection_change(
            "human_sensitivity_set",
            build_human_sensitivity_set_path(sensitivity),
            experimental=False,
            confirm=False,
        )
        with _service_acknowledgement("human sensitivity", "human getter"):
            return parse_human_sensitivity_set_response(payload, sensitivity)

    async def set_human_frame(self, enabled: bool) -> dict:
        payload = await self._send_detection_change(
            "human_frame_set",
            build_human_frame_set_path(enabled),
            experimental=False,
            confirm=False,
        )
        with _service_acknowledgement("human frame configuration", "human getter"):
            return parse_human_frame_set_response(payload, enabled)

    async def set_human_tracking(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        await self._send_detection_change(
            "human_tracking_set",
            build_human_tracking_set_path(enabled),
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "human-tracking configuration was sent once and a response arrived, but its "
            "exact acknowledgement contract is not proven; the outcome is unknown and "
            "must be checked with the tracking getter before another write"
        )

    async def set_human_zoom_tracking(
        self,
        enabled: bool,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> Never:
        definition = self.catalog.get("human_zoom_tracking_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        await self._require_command_applicable(definition.name)
        current = await self.get_human_detection_settings()
        if "zoom_tracking_setting_enabled" not in current:
            raise DetectionConfigurationError(
                "camera did not report humanoid_zoom; refusing a write without an inverse state"
            )
        await self._send_service_change(
            build_human_zoom_tracking_set_path(enabled),
            "human zoom tracking configuration",
            experimental=experimental,
            confirm=confirm,
        )
        raise ServiceChangeUncertainError(
            "human zoom-tracking configuration was sent once and a response arrived, but "
            "its exact acknowledgement contract is not proven; the outcome is unknown "
            "and must be checked with the human getter before another write"
        )

    async def _send_detection_change(
        self,
        definition_name: str,
        path: str,
        *,
        experimental: bool,
        confirm: bool,
    ) -> dict:
        definition = self.catalog.get(definition_name)
        definition.require_permission(experimental=experimental, confirm=confirm)
        await self._require_command_applicable(definition_name)
        return await self._send_service_change(
            path,
            definition_name.replace("_", " "),
            experimental=experimental,
            confirm=confirm,
        )

    async def get_wifi_status(self) -> dict:
        return extract_wifi_status(await self.get_params())

    async def scan_wifi(self) -> dict:
        definition = self.catalog.get("wifi_scan")
        response = await self._send_cgi(definition.path)
        return {"networks": parse_wifi_scan_response(response)}

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
    ) -> Never:
        """Send the unconfirmed Wi-Fi candidate once and never retry it."""

        async with self._wifi_lock:
            return await self._set_wifi(
                ssid,
                password,
                channel=channel,
                auth_type=auth_type,
                experimental=experimental,
                confirm=confirm,
                recovery_ready=recovery_ready,
            )

    async def _set_wifi(
        self,
        ssid: str,
        password: str,
        *,
        channel: int | None,
        auth_type: int | None,
        experimental: bool,
        confirm: bool,
        recovery_ready: bool,
    ) -> Never:
        definition = self.catalog.get("wifi_set")
        definition.require_permission(experimental=experimental, confirm=confirm)
        if not recovery_ready:
            raise ConfirmationRequiredError(
                "wifi_set: pass --recovery-ready only after verifying physical reset/recovery"
            )
        validate_wifi_credentials(ssid, password)
        append_auth("/set_wifi.cgi", self.config)

        try:
            scan = await self.scan_wifi()
            channel, auth_type = complete_wifi_metadata(scan["networks"], ssid, channel, auth_type)
            path = build_wifi_set_path(ssid, password, channel, auth_type)

            if not self.connected:
                await self.connect()
            try:
                await self._send_cgi(
                    path,
                    experimental=experimental,
                    confirm=confirm,
                    recovery_ready=True,
                    retry_requests=False,
                )
            except TransportCommandCancelledError as exc:
                raise WifiChangeCancelledError(
                    "Wi-Fi command cancellation occurred after its one-shot send may have "
                    "started; the outcome is unknown, so use the recovery plan instead "
                    "of resending"
                ) from exc
            except _TRANSPORT_FAILURES as exc:
                raise WifiChangeUncertainError(
                    "Wi-Fi command was sent once without retry, but no acknowledgement arrived; "
                    "its outcome is unknown, so use the recovery plan instead of resending it"
                ) from exc
            except ResponseParseError as exc:
                raise WifiChangeUncertainError(
                    "Wi-Fi command was sent once, but its response was malformed; the "
                    "outcome is unknown, so use the recovery plan instead of resending it"
                ) from exc
            raise WifiChangeUncertainError(
                "Wi-Fi command was sent once and a response arrived, but its exact "
                "acknowledgement contract is not proven; the outcome is unknown, so "
                "rediscover the camera or use the recovery plan instead of resending"
            )
        finally:
            await self.transport.close()

    async def move_ptz(
        self,
        direction: PTZDirection,
        duration: float,
        *,
        experimental: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Move in one direction for a caller-bounded duration, then stop."""

        async with self._ptz_move_lock:
            return await self._move_ptz(
                direction,
                duration,
                experimental=experimental,
                confirm=confirm,
            )

    async def _move_ptz(
        self,
        direction: PTZDirection,
        duration: float,
        *,
        experimental: bool,
        confirm: bool,
    ) -> dict:
        duration = validate_ptz_duration(duration)
        start_path = build_ptz_start_path(direction)
        stop_path = build_ptz_stop_path(direction)
        start_definition = self.catalog.get(f"ptz_{direction}_start")
        stop_definition = self.catalog.get(f"ptz_{direction}_stop")
        start_definition.require_permission(experimental=experimental, confirm=confirm)
        stop_definition.require_permission(experimental=experimental, confirm=False)

        await self._require_command_applicable(start_definition.name)
        start_attempted = False
        primary_error: BaseException | None = None
        start_response: dict | None = None
        stop_response: dict | None = None
        try:
            deadline = asyncio.get_running_loop().time() + duration
            start_attempted = True
            movement_timeout = asyncio.timeout_at(deadline)
            try:
                async with movement_timeout:
                    start_payload = await self._send_service_change(
                        start_path,
                        f"PTZ {direction} start",
                        experimental=experimental,
                        confirm=confirm,
                    )
                    try:
                        start_response = parse_ptz_response(start_payload)
                    except PTZConfigurationError as exc:
                        raise ServiceChangeUncertainError(
                            f"PTZ {direction} start was sent once, but its acknowledgement "
                            "was invalid; the outcome is unknown"
                        ) from exc
            except TimeoutError as exc:
                raise ServiceChangeUncertainError(
                    f"PTZ {direction} start was not acknowledged before the movement "
                    "deadline; the outcome is unknown"
                ) from exc
            except asyncio.CancelledError as exc:
                if not movement_timeout.expired():
                    raise
                raise ServiceChangeUncertainError(
                    f"PTZ {direction} start was not acknowledged before the movement "
                    "deadline; the outcome is unknown"
                ) from exc
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if start_attempted:
                try:
                    stop_response = await _await_cancellation_safe_cleanup(
                        self._send_ptz_stop_once(
                            direction,
                            stop_path,
                            experimental=experimental,
                        ),
                        operation="PTZ cleanup stop",
                    )
                except BaseException:
                    if primary_error is None:
                        raise
                    LOGGER.warning(
                        "PTZ cleanup stop failed after movement error",
                        exc_info=True,
                    )

        return {"start": start_response, "stop": stop_response}

    async def stop_ptz(
        self,
        direction: PTZDirection,
        *,
        experimental: bool = False,
    ) -> dict:
        """Send the direction-specific manual movement stop once."""

        path = build_ptz_stop_path(direction)
        definition = self.catalog.get(f"ptz_{direction}_stop")
        definition.require_permission(experimental=experimental, confirm=False)
        return await self._send_ptz_stop_once(direction, path, experimental=experimental)

    async def _send_ptz_stop_once(
        self,
        direction: PTZDirection,
        path: str,
        *,
        experimental: bool,
    ) -> dict:
        stop_timeout = asyncio.timeout(self.config.timeout)
        try:
            async with stop_timeout:
                payload = await self._send_service_change(
                    path,
                    f"PTZ {direction} stop",
                    experimental=experimental,
                    confirm=False,
                )
        except TimeoutError as exc:
            raise ServiceChangeUncertainError(
                f"PTZ {direction} stop did not complete within the request timeout; "
                "the outcome is unknown"
            ) from exc
        except asyncio.CancelledError as exc:
            if not stop_timeout.expired():
                raise
            raise ServiceChangeUncertainError(
                f"PTZ {direction} stop did not complete within the request timeout; "
                "the outcome is unknown"
            ) from exc
        try:
            return parse_ptz_response(payload)
        except PTZConfigurationError as exc:
            raise ServiceChangeUncertainError(
                f"PTZ {direction} stop was sent once, but its acknowledgement was "
                "invalid; the outcome is unknown"
            ) from exc

    async def set_siren(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ActuatorStateConfigurationError("siren state must be boolean")
        definition = self.catalog.get("siren_on" if enabled else "siren_off")
        payload = await self._send_service_change(
            definition.path,
            "siren configuration",
            experimental=False,
            confirm=False,
        )
        with _service_acknowledgement("siren configuration", "siren getter"):
            return parse_actuator_set_response(payload)

    async def set_light(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ActuatorStateConfigurationError("white-light state must be boolean")
        definition = self.catalog.get("white_light_on" if enabled else "white_light_off")
        await self._require_command_applicable(definition.name)
        payload = await self._send_service_change(
            definition.path,
            "white-light configuration",
            experimental=False,
            confirm=False,
        )
        with _service_acknowledgement("white-light configuration", "light getter"):
            return parse_actuator_set_response(payload)
