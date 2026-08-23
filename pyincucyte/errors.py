"""Exception hierarchy for PyIncucyte.

Every error raised by the package derives from :class:`IncucyteError`, so a
pipeline can wrap a whole download in one ``except IncucyteError``.  The
classes also derive from ``RuntimeError`` because the original single-file
script raised bare ``RuntimeError`` everywhere — existing ``except
RuntimeError`` blocks keep working unchanged.
"""


class IncucyteError(RuntimeError):
    """Base class for every PyIncucyte failure."""


class DeviceUnreachableError(IncucyteError):
    """The Incucyte device could not be reached (network, DNS, firewall)."""


class ApiError(IncucyteError):
    """The device returned an HTTP or SOAP-level error."""

    def __init__(self, message, status_code=None, route=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.route = route
        self.body = body


class AuthenticationError(IncucyteError):
    """Credentials were rejected, missing, or expired."""


class NotLoggedInError(AuthenticationError):
    """No saved credentials — call ``login()`` first."""


class TokenExpiredError(AuthenticationError):
    """The bearer token is no longer valid and could not be refreshed."""


class EncryptionUnavailableError(IncucyteError):
    """The Incucyte .NET password encryption assembly could not be loaded."""


class VesselNotFoundError(IncucyteError):
    """The requested vessel id is not present on the device."""


class ExportError(IncucyteError):
    """An output file could not be written (bad dimensions, dtype, disk)."""


class ExportCancelled(IncucyteError):
    """A download was stopped by its cancel event before finishing."""


class ConfirmationRequiredError(IncucyteError):
    """A write to the instrument was attempted without confirming it.

    Everything else in PyIncucyte reads.  The handful of calls that change the
    instrument refuse until the caller says so in as many words, because the
    Incucyte is shared: ``confirm=True`` in Python, ``--yes`` on the command
    line, the dialog in the app.
    """


class DeviceBusyError(IncucyteError):
    """The instrument is in a state that makes this write pointless or unsafe."""


__all__ = [
    "IncucyteError",
    "DeviceUnreachableError",
    "ApiError",
    "AuthenticationError",
    "NotLoggedInError",
    "TokenExpiredError",
    "EncryptionUnavailableError",
    "VesselNotFoundError",
    "ExportError",
    "ExportCancelled",
    "ConfirmationRequiredError",
    "DeviceBusyError",
]
