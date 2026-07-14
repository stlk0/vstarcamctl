"""Project-specific exceptions with user-facing messages."""


class VStarcamError(Exception):
    """Base error for expected VStarcamCtl failures."""


class ConfigError(VStarcamError):
    """Configuration is missing or invalid."""


class RawCommandError(VStarcamError):
    """A raw CGI path is invalid or unsafe."""


class ResponseParseError(VStarcamError):
    """A camera response is malformed or exceeds safe parser limits."""


class ExperimentalCommandError(VStarcamError):
    """A command is not confirmed and needs an explicit opt-in."""


class ConfirmationRequiredError(VStarcamError):
    """A risky command needs a second explicit confirmation."""


class TransportError(VStarcamError):
    """The selected camera transport failed."""


class TransportUnavailableError(TransportError):
    """An optional transport dependency or capability is unavailable."""


class TransportTimeoutError(TransportError):
    """The camera did not respond before the configured timeout."""


class WifiChangeUncertainError(TransportError):
    """A Wi-Fi write may have applied even though no acknowledgement arrived."""


class WifiConfigurationError(VStarcamError):
    """Wi-Fi input or scan metadata is incomplete or unsafe."""


class ServiceChangeUncertainError(TransportError):
    """A service-setting write may have applied without an acknowledgement."""


class DetectionConfigurationError(VStarcamError):
    """Motion or human-detection input/response data is invalid."""


class NightVisionConfigurationError(VStarcamError):
    """Night-vision or infrared-light input/response data is invalid."""


class TimeConfigurationError(VStarcamError):
    """Camera clock, timezone, or NTP input/response data is invalid."""


class MediaConfigurationError(VStarcamError):
    """RTSP, ONVIF, or audio settings are invalid or incomplete."""


class MediaStreamError(VStarcamError):
    """An RTSP media stream could not be read or written."""


class MediaToolUnavailableError(MediaStreamError):
    """A required local media executable is unavailable."""


class AccountConfigurationError(VStarcamError):
    """Camera-account password input is invalid or unsafe."""


class AccountChangeUncertainError(TransportError):
    """A camera-account password write may have applied without acknowledgement."""


class CatalogError(VStarcamError):
    """The structured command catalog is invalid."""
