"""Talking *back* to the instrument: device state, and the two writes.

Everything else in PyIncucyte reads.  This module is the exception, and it is
deliberately small and deliberately awkward to fire by accident.

Two halves:

*Reading* turns ``Device/Status/GetDeviceStatusUpdate`` into a
:class:`DeviceState` - what the instrument is doing, how warm it is, when it
next scans, whether the drawer is shut.  No side effects; call it as often as
you like.

*Writing* is :func:`begin_scan` (scan one vessel now) and :func:`save_unmix`
(store unmixing coefficients on a vessel, so the Incucyte's own software shows
them).  Both refuse unless the caller passes ``confirm=True``.  The instrument
is shared: until proven otherwise, a write is somebody else's experiment.

Note what is deliberately **not** here.  There is no stop, pause or abort - the
device API has none.  Ending an experiment early means
``Scheduling/ApplySchedule``, which replaces the schedule for the *whole tray*,
every vessel on it, guarded by a staleness token.  That is a read-modify-write
on shared state, and it stays out of this package until it can be watched
against the real instrument.

Route and field names were read off the vendor client assemblies that ship with
the Incucyte software (``Essen.IncuCyte.Models.dll``: ``DeviceStatusState``,
``BeginScanReqParams``, ``SaveUnmixReqParams``), so they are transcribed rather
than guessed.  None of it has run against the instrument.  Every reader here
therefore tolerates a missing or renamed key and keeps the untouched payload in
``.raw``, so a surprise degrades to a thinner answer instead of an exception.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .engine import parse_scan_datetime
from .errors import ConfirmationRequiredError, DeviceBusyError

log = logging.getLogger("pyincucyte.device")

# -- routes -----------------------------------------------------------------

ROUTE_STATUS = "Device/Status/GetDeviceStatusUpdate"
ROUTE_TEMPERATURES = "Device/Temperature/GetDeviceTemperatures"
ROUTE_SCAN_PATTERN = "Vessels/GetScanPattern"
ROUTE_BEGIN_SCAN = "Automation/BeginScan"
ROUTE_SAVE_UNMIX = "Vessels/SaveUnmix"
ROUTE_VALIDATE_LOGIN = "Users/ValidateUserLogin"

#: ``DeviceActivityTypeCode`` in declaration order - a .NET enum serialises as
#: its index, so position matters here.
DEVICE_ACTIVITY = (
    "Idle", "Scanning", "GatheringMetrics", "DeviceError",
    "FluorescenceCalibration", "ActivityPaused", "WarmingUp", "OpticsTest",
    "Restarting", "DiskNearlyFull", "DiskFull", "SelfTest", "Updating",
    "DatabaseNearlyFull", "DatabaseFull", "OpticsMismatchError",
    "FrontPanelOpen", "BadControllerFan", "HighCpuTemp", "HighPchTemp",
    "RaidDegraded", "RaidOffline", "RaidRebuilding", "PrintingVesselMarks",
    "StitchingImages", "FrontSpatialCalibration", "MiddleSpatialCalibration",
    "RearSpatialCalibration", "FrontSpatialConfirmTest",
    "MiddleSpatialConfirmTest", "RearSpatialConfirmTest",
    "LampHouseMismatchError",
)

#: ``DrawerStatusTypeCode``, same rule.
DRAWER_STATUS = ("Unknown", "Open", "Closed", "Opening", "Closing")

#: Activities that mean something is *wrong*, not merely busy.  Asking for a
#: scan while one of these holds achieves nothing, so the writes refuse.
ACTIVITY_PROBLEMS = frozenset({
    "DeviceError", "DiskFull", "DiskNearlyFull", "DatabaseFull",
    "DatabaseNearlyFull", "OpticsMismatchError", "LampHouseMismatchError",
    "BadControllerFan", "HighCpuTemp", "HighPchTemp", "RaidDegraded",
    "RaidOffline",
})

#: Activities where the instrument is working normally and simply occupied.
ACTIVITY_WORKING = frozenset({
    "Scanning", "GatheringMetrics", "FluorescenceCalibration", "WarmingUp",
    "OpticsTest", "SelfTest", "Updating", "Restarting", "RaidRebuilding",
    "PrintingVesselMarks", "StitchingImages", "FrontSpatialCalibration",
    "MiddleSpatialCalibration", "RearSpatialCalibration",
    "FrontSpatialConfirmTest", "MiddleSpatialConfirmTest",
    "RearSpatialConfirmTest",
})

UNKNOWN = "Unknown"


# -- reading the payload ----------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def spell_out(name):
    """``"DiskNearlyFull"`` -> ``"Disk nearly full"``, for a human to read."""
    if not name:
        return UNKNOWN
    words = _CAMEL.sub(" ", str(name)).split()
    if not words:
        return UNKNOWN
    return " ".join([words[0].capitalize()] + [w.lower() for w in words[1:]])


def enum_name(value, names, default=UNKNOWN):
    """Name a .NET enum whether it arrived as its index or as its name."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        index = int(value)
        return names[index] if 0 <= index < len(names) else default
    text = str(value).strip()
    for name in names:
        if name.lower() == text.lower():
            return name
    return text or default


def _get(data, *keys):
    """First present key from a dict, case-insensitively. None if absent."""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value
    return None


def _as_datetime(value):
    """A device timestamp, or None. Never raises - the field may be absent."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return parse_scan_datetime(str(value))
    except (ValueError, TypeError):
        return None


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_timespan(value):
    """A .NET TimeSpan (``"01:23:45"``, or seconds) as a timedelta, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    parts = str(value).split(":")
    try:
        if len(parts) == 3:
            days, hours = 0, parts[0]
            if "." in hours:                   # .NET writes "1.02:03:04"
                days, hours = hours.split(".", 1)
            return timedelta(days=int(days), hours=int(hours),
                             minutes=int(parts[1]), seconds=float(parts[2]))
        return timedelta(seconds=float(value))
    except (TypeError, ValueError):
        return None


def _hours_minutes(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


@dataclass
class DeviceState:
    """What the instrument is doing right now.

    Read it with :meth:`~pyincucyte.client.IncucyteClient.device_state`.  Any
    field can be ``None`` if this software version does not report it; ``raw``
    always holds the payload exactly as it arrived.
    """

    activity: str = UNKNOWN
    drawer: str = UNKNOWN
    automation_mode: bool = False
    percent_complete: float = None
    time_to_complete: timedelta = None
    last_scan: datetime = None
    next_scan: datetime = None
    next_scan_user: str = ""
    device_time: datetime = None
    gantry_c: float = None
    optics_c: float = None
    cube_c: float = None
    camera_c: float = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload):
        """Build from ``Device/Status/GetDeviceStatusUpdate``'s ``Data``."""
        data = payload if isinstance(payload, dict) else {}
        # The route answers with a DeviceStatusUpdateState wrapping the status,
        # but accept a bare DeviceStatusState too - older builds, and anyone
        # who passes the inner block straight in.
        status = _get(data, "DeviceStatus") or data
        if not isinstance(status, dict):
            status = {}
        next_info = _get(status, "NextScanInfo") or {}
        temperature = _get(status, "Temperature") or {}
        return cls(
            activity=enum_name(_get(status, "DeviceActivity", "Activity"),
                               DEVICE_ACTIVITY),
            drawer=enum_name(_get(status, "DrawerStatus", "Drawer"),
                             DRAWER_STATUS),
            automation_mode=bool(_get(status, "IsAutomationMode") or False),
            percent_complete=_as_number(_get(status, "PercentageComplete")),
            time_to_complete=_as_timespan(_get(status, "TimeToComplete")),
            last_scan=_as_datetime(_get(status, "LastScan")),
            next_scan=_as_datetime(_get(next_info, "NextScan")),
            next_scan_user=str(_get(next_info, "UserName") or ""),
            device_time=_as_datetime(_get(status, "DateTime")),
            gantry_c=_as_number(_get(temperature, "GantryBoardDegreesCelcius")),
            optics_c=_as_number(_get(temperature, "OpticsBoardDegreesCelcius")),
            cube_c=_as_number(_get(temperature, "CubeBoardDegreesCelcius")),
            camera_c=_as_number(_get(temperature, "CameraDegreesCelcius")),
            raw=data,
        )

    # -- what it means ----------------------------------------------------

    @property
    def is_idle(self):
        return self.activity == "Idle"

    @property
    def is_scanning(self):
        return self.activity == "Scanning"

    @property
    def is_working(self):
        """Busy, but in a healthy way - scanning, calibrating, warming up."""
        return self.activity in ACTIVITY_WORKING

    @property
    def has_problem(self):
        """The instrument is reporting a fault, a full disk, or a hot board."""
        return self.activity in ACTIVITY_PROBLEMS

    @property
    def temperature_c(self):
        """The one temperature worth quoting: the gantry board."""
        return self.gantry_c

    def summary(self):
        """One line: ``"Scanning, 42% done, ~1h 10m left"``."""
        parts = [spell_out(self.activity)]
        if self.percent_complete is not None and self.is_working:
            parts.append(f"{self.percent_complete:.0f}% done")
        if self.time_to_complete:
            parts.append(f"~{_hours_minutes(self.time_to_complete)} left")
        if self.automation_mode:
            parts.append("automation mode")
        return ", ".join(parts)

    def describe(self):
        """Lines for a status readout, in the order a person wants them."""
        lines = [f"Activity:     {self.summary()}"]
        if self.drawer and self.drawer != UNKNOWN:
            lines.append(f"Drawer:       {spell_out(self.drawer)}")
        if self.next_scan:
            who = (f" (scheduled by {self.next_scan_user})"
                   if self.next_scan_user else "")
            lines.append(f"Next scan:    {self.next_scan:%Y-%m-%d %H:%M}{who}")
        if self.last_scan:
            lines.append(f"Last scan:    {self.last_scan:%Y-%m-%d %H:%M}")
        temperatures = [(label, value) for label, value in (
            ("gantry", self.gantry_c), ("optics", self.optics_c),
            ("cube", self.cube_c), ("camera", self.camera_c))
            if value is not None]
        if temperatures:
            lines.append("Temperature:  " + ", ".join(
                f"{label} {value:.1f}C" for label, value in temperatures))
        if self.device_time:
            lines.append(f"Device clock: {self.device_time:%Y-%m-%d %H:%M:%S}")
        if self.has_problem:
            lines.append(f"WARNING:      the instrument is reporting "
                         f"{spell_out(self.activity).lower()}")
        return lines

    def to_dict(self):
        """A JSON-friendly view, for ``--json`` and for logging."""
        return {
            "summary": self.summary(),
            "activity": self.activity,
            "drawer": self.drawer,
            "automation_mode": self.automation_mode,
            "percent_complete": self.percent_complete,
            "seconds_to_complete": (self.time_to_complete.total_seconds()
                                    if self.time_to_complete else None),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "next_scan": self.next_scan.isoformat() if self.next_scan else None,
            "next_scan_user": self.next_scan_user,
            "device_time": (self.device_time.isoformat()
                            if self.device_time else None),
            "temperatures_c": {"gantry": self.gantry_c, "optics": self.optics_c,
                               "cube": self.cube_c, "camera": self.camera_c},
            "healthy": not self.has_problem,
        }

    def __str__(self):
        return self.summary()


@dataclass
class ScanPattern:
    """How one vessel is imaged: which wells, how many sites, what objective."""

    name: str = ""
    images_per_well: int = 0
    magnification: str = ""
    well_count: int = 0
    whole_well: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload):
        data = payload if isinstance(payload, dict) else {}
        wells = _get(data, "Swells")
        return cls(
            name=str(_get(data, "Name") or ""),
            images_per_well=int(_get(data, "ImagesPerSwell") or 0),
            magnification=str(_get(data, "Magnification") or ""),
            well_count=len(wells) if isinstance(wells, list) else 0,
            whole_well=bool(_get(data, "IsWholeWellSamplePattern") or False),
            raw=data,
        )

    def summary(self):
        bits = [self.name or "unnamed pattern"]
        if self.well_count:
            bits.append(f"{self.well_count} wells")
        if self.images_per_well:
            bits.append(f"{self.images_per_well} image"
                        f"{'' if self.images_per_well == 1 else 's'} per well")
        if self.whole_well:
            bits.append("whole well")
        return ", ".join(bits)

    def __str__(self):
        return self.summary()


# -- reads ------------------------------------------------------------------

def read_state(call):
    """Fetch and parse the instrument's current status."""
    return DeviceState.from_payload(call(ROUTE_STATUS))


def read_temperatures(call, start=None, end=None):
    """Temperature readings between two moments, oldest first."""
    end = end or datetime.now()
    start = start or (end - timedelta(hours=24))
    data = call(ROUTE_TEMPERATURES,
                {"StartTime": start.isoformat(), "EndTime": end.isoformat()})
    return data if isinstance(data, list) else []


def read_scan_pattern(call, vessel_id, when=None):
    """The imaging pattern a vessel was scanned with at ``when``."""
    moment = when or datetime.now()
    if isinstance(moment, datetime):
        moment = moment.isoformat()
    return ScanPattern.from_payload(
        call(ROUTE_SCAN_PATTERN, {"VesselID": int(vessel_id),
                                  "DateTime": moment}))


def read_user_id(call, username, encrypted_password):
    """The device's numeric id for a user, which the Automation routes want."""
    data = call(ROUTE_VALIDATE_LOGIN, {"UserName": username,
                                       "Password": encrypted_password})
    if isinstance(data, dict):
        for key in ("ID", "UserID", "Id"):
            value = _get(data, key)
            if value is not None:
                return int(value)
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return int(data)
    return None


# -- writes -----------------------------------------------------------------

#: How a caller says yes, in each of the three places this is reachable from.
HOW_TO_CONFIRM = ("Pass confirm=True in Python, --yes on the command line, "
                  "or use the dialog in the app.")


def require_confirmation(confirm, what):
    """Refuse a write nobody has explicitly asked for."""
    if not confirm:
        raise ConfirmationRequiredError(
            f"{what} would change the instrument, and the Incucyte is shared, "
            f"so this needs confirming. {HOW_TO_CONFIRM}")


def refuse_if_unwell(state, what):
    """Refuse a write while the instrument is reporting a fault."""
    if state is not None and state.has_problem:
        raise DeviceBusyError(
            f"{what} was not sent: the instrument is reporting "
            f"{spell_out(state.activity).lower()}. Clear that first, or send "
            f"it anyway with --force (force=True in Python).")


def begin_scan(call, vessel_id, user_id, *, confirm=False, force=False,
               state=None, label=""):
    """Ask the instrument to scan one vessel now (``Automation/BeginScan``).

    Scoped to a single vessel: it adds work, it does not rewrite anybody's
    schedule.  There is no matching stop - see the module docstring.

    ``state`` is a :class:`DeviceState` already in hand, so a caller that has
    just shown somebody the status does not fetch it twice.

    Unverified against the instrument, and the Automation controller may want
    the device in automation mode - if it refuses, the device's own words come
    back in the :class:`~pyincucyte.errors.ApiError`.
    """
    vessel_id = int(vessel_id)
    named = f"vessel {vessel_id}" + (f" ({label})" if label else "")
    what = f"Starting a scan of {named}"
    require_confirmation(confirm, what)
    if not force:
        refuse_if_unwell(state, what)
    log.info("requesting a scan of %s", named)
    return call(ROUTE_BEGIN_SCAN, {"VesselID": vessel_id,
                                   "AutomationUserID": int(user_id)})


def unmix_payload(vessel_id, unmixing):
    """The ``SaveUnmix`` body for an :class:`~pyincucyte.processing.Unmixing`.

    Device channels are ``C1``/``C2``, numbered 1 and 2; ours are image types 2
    and 3, hence the same offset :mod:`~pyincucyte.processing` adds when it
    reads them back.  Returns the body and the coerced Unmixing.
    """
    from .processing import COLOR_INDEX_OFFSET, Unmixing

    unmixing = Unmixing.coerce(unmixing)
    pairs = [{"Recipient": int(term["recipient"]) - COLOR_INDEX_OFFSET,
              "Contributor": int(term["contributor"]) - COLOR_INDEX_OFFSET,
              "ValueRatio": float(term["ratio"]),
              "BlurringSigma": float(term["sigma"]) or None}
             for term in unmixing.terms()]
    return {"VesselID": int(vessel_id), "UnmixPairs": pairs}, unmixing


def save_unmix(call, vessel_id, unmixing, *, confirm=False, force=False,
               state=None, label=""):
    """Store unmixing coefficients on a vessel (``Vessels/SaveUnmix``).

    This is the one write that changes what other people *see*: the Incucyte's
    own viewer applies these when it draws the vessel.  It changes no pixels
    already on disk, and PyIncucyte does not need it - ``fetch(..., unmix=...)``
    does the same arithmetic locally without touching the device.
    """
    vessel_id = int(vessel_id)
    payload, unmixing = unmix_payload(vessel_id, unmixing)
    named = f"vessel {vessel_id}" + (f" ({label})" if label else "")
    what = (f"Saving unmixing ({unmixing.describe()}) onto {named}, which "
            f"changes what the Incucyte software shows everybody,")
    require_confirmation(confirm, what)
    if not force:
        refuse_if_unwell(state, what)
    log.info("saving unmixing on %s: %s", named, unmixing.to_spec() or "none")
    call(ROUTE_SAVE_UNMIX, payload)
    return unmixing


__all__ = [
    "DeviceState", "ScanPattern",
    "read_state", "read_temperatures", "read_scan_pattern", "read_user_id",
    "begin_scan", "save_unmix", "unmix_payload",
    "require_confirmation", "refuse_if_unwell", "enum_name", "spell_out",
    "DEVICE_ACTIVITY", "DRAWER_STATUS", "ACTIVITY_PROBLEMS", "ACTIVITY_WORKING",
    "HOW_TO_CONFIRM",
    "ROUTE_STATUS", "ROUTE_TEMPERATURES", "ROUTE_SCAN_PATTERN",
    "ROUTE_BEGIN_SCAN", "ROUTE_SAVE_UNMIX", "ROUTE_VALIDATE_LOGIN",
]
