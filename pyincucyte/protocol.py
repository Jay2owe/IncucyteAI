"""The acquisition protocol, drawn.

The Incucyte's own software shows a run's design as nested boxes: a time loop
around a well loop around a chain of channel nodes. The picture is genuinely the
clearest statement of what an experiment is - "every 30 minutes, at all 24
wells, take these three exposures in this order" - and it is locked inside the
vendor software, on a machine beside the instrument.

Every fact in it is already on the wire:

* the **vessel record** (``GetAllSearchVessels``), which the client has already
  cached - the channel names an experiment gave its colours, the plate type, the
  pixel size, and when the run began;
* the **scan payload** (``GetScanVessel``), which carries the acquisition plan
  the vendor software never shows over the network: ``AcquisitionTime`` and
  ``StareTime`` per colour, the optical module's wavelengths and units, the scan
  pattern's wells and sites per well, and the stop-after schedule;
* the **scan times** (``AllScanTimes``), which say how far the run has actually
  got and what cadence it achieved;
* the **device status**, which says whether the instrument is acquiring now.

So this draws that, and then says the things the vendor's own graph does not:
what the cycle time turned out to BE rather than what was asked for, how much of
the run has happened, which of the plate's wells the pattern actually images,
and where every value on the page came from. A requested cycle and an achieved
one are not the same kind of fact, and six months later the drawing is the only
thing that can still tell them apart.

Nothing here reads a pixel. The whole page costs the cached vessel list, one
scan payload and one sweep of scan times, and ``scan=False`` drops even the
sweep.

Three backends, one geometry. :func:`layout` turns a :class:`Protocol` into a
list of primitive shapes; the SVG writer, the matplotlib writer and the ASCII
drawing all consume that, so the terminal and the figure cannot start
disagreeing about what the run does. matplotlib is imported inside the function
that needs it, never at module import - an SVG costs no plotting stack at all,
and the frozen desktop app excludes matplotlib outright.

The instrument's address is never on the page. A drawing is a file somebody
sends to a collaborator, and a site-specific address that travelled that way
would be exactly the leak this package has already paid for once.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import channels as ch
from . import wells as wl
from .engine import parse_scan_datetime
from .errors import IncucyteError

#: A cycle the timestamps disagree with by more than this is reported. Enough
#: wells at a long enough exposure overrun the requested cadence, and the
#: derived number is the one that is true.
CYCLE_TOLERANCE = 0.02


# --------------------------------------------------------------------------
# palettes
#
# The hex values are `gui.theme`'s, copied rather than imported: that module
# imports Tk at the top, and a drawing must be producible on a headless machine.
# Copied so the app and the figure look like one product.
# --------------------------------------------------------------------------

LIGHT = {
    "page": "#FFFFFF",
    "surface": "#F1F4F8",         # the outer loop box
    "surface_alt": "#FFFFFF",     # the inner box, and a channel node
    "border": "#D5DCE5",
    "border_strong": "#B9C4D0",
    "text": "#16212E",
    "muted": "#657588",
    "accent": "#0E7C86",
    "track": "#DFE5EC",
    "live": "#C62828",
    "warning": "#B26A00",
}

DARK = {
    "page": "#171C24",
    "surface": "#1F262F",
    "surface_alt": "#252D38",
    "border": "#333D4A",
    "border_strong": "#46525F",
    "text": "#E8EDF3",
    "muted": "#94A3B3",
    "accent": "#2AA9B4",
    "track": "#333D4A",
    "live": "#EF6C6C",
    "warning": "#E0A34A",
}

THEMES = {"light": LIGHT, "dark": DARK}

#: A colour per channel, chosen from its NAME and meaning nothing about the
#: pixels. Phase is a transmitted-light image and the two Colors are single-band
#: detector channels; the colour here is wayfinding on a diagram, exactly as the
#: Incucyte's own green Color 1 icon is.
_CHANNEL_KEYS = (
    ("bf", ("phase", "bright", "trans", "dia", "dic")),
    ("green", ("gfp", "yfp", "fitc", "green", "venus", "citrine", "clover")),
    ("red", ("rfp", "mcherry", "tdtomato", "tritc", "red", "dsred", "scarlet",
             "cherry")),
    ("blue", ("dapi", "hoechst", "cfp", "blue")),
    ("far", ("cy5", "far", "apc", "alexa 6", "irfp")),
)

CHANNEL_COLOURS = {
    "light": {"bf": "#8A94A6", "green": "#2E9E4F", "red": "#C6413F",
              "blue": "#3B6FD4", "far": "#B0479B", "other": "#4A6072"},
    "dark": {"bf": "#9AA5B4", "green": "#5DBB63", "red": "#EF6C6C",
             "blue": "#6E9BE8", "far": "#D07FC0", "other": "#8FA3B8"},
}

#: When a name says nothing, the device's own channel numbering does. Image type
#: 1 is Phase, 2 is Color 1 (Green) and 3 is Color 2 (Red) - see `channels.py`.
_BY_IMAGE_TYPE = {ch.PHASE: "bf", ch.COLOR1: "green", ch.COLOR2: "red"}


def channel_colour(name, theme="light", image_type=None):
    """A display colour for a channel. Wayfinding only - see above.

    The name wins, because an experiment that renamed Color 1 to mCherry meant
    it; the device's channel number is the fallback when a name says nothing.
    """
    text = str(name or "").lower()
    table = CHANNEL_COLOURS.get(theme, CHANNEL_COLOURS["light"])
    for key, needles in _CHANNEL_KEYS:
        if any(n in text for n in needles):
            return table[key]
    return table.get(_BY_IMAGE_TYPE.get(image_type), table["other"])


def palette(theme="light"):
    return dict(THEMES.get(str(theme).lower(), LIGHT))


def format_exposure(seconds):
    """An exposure in the unit a person says it in.

    The device states acquisition times in milliseconds and they are typically a
    few hundred, so milliseconds is the ordinary unit here rather than the
    special case.
    """
    if seconds is None:
        return ""
    value = float(seconds)
    if value < 1.0:
        return "%g ms" % round(value * 1000.0, 3)
    return "%g s" % round(value, 3)


# --------------------------------------------------------------------------
# what a protocol is
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One channel node: an exposure the run takes at every well, in order.

    ``name_source`` and ``plan_source`` are separate on purpose. The name can
    come from the cached vessel record while the exposure beside it comes from
    the scan payload, and those are not the same strength of claim.

    Where PyLV200 puts an EM gain, an Incucyte puts its **stare time** - the
    settling time before the exposure. The role in the drawing is the same; the
    number is not, so it is not called a gain.
    """

    index: int                     # acquisition order, from zero
    image_type: int = 0            # the device's own 1/2/3
    name: str = ""
    name_source: str = ""          # vessel | scan | override | default
    exposure_s: float = None
    stare_s: float = None
    frame_count: int = None
    wavelength_nm: int = None
    units: str = ""                # GCU / RCU, the vendor's calibrated units
    collected: bool = True
    plan_name: str = ""            # what the scan payload called it
    plan_source: str = ""          # scan | none

    @property
    def label(self):
        return (self.name or self.plan_name
                or ch.image_type_label(self.image_type))

    @property
    def exposure(self):
        """The exposure in the unit a person says it in, or "" if unknown."""
        return format_exposure(self.exposure_s)

    @property
    def stare_text(self):
        """``stare 180 ms`` - the settling time before the exposure."""
        bits = []
        if self.stare_s:
            bits.append("stare %s" % format_exposure(self.stare_s))
        if self.frame_count and self.frame_count > 1:
            bits.append("x%d" % self.frame_count)
        return "  ".join(bits)

    @property
    def filter_text(self):
        """``524 nm GCU`` - the band this channel sits behind, and its unit."""
        bits = []
        if self.wavelength_nm:
            bits.append("%d nm" % self.wavelength_nm)
        if self.units:
            bits.append(self.units)
        return " ".join(bits)

    @property
    def rows(self):
        """The three lines under the name, by ROLE and possibly empty.

        Aligned by role rather than packed, so Phase - which has no exposure at
        all - does not print its wavelength where its neighbours print their
        stare time. A row no channel uses is dropped once, for the whole chain.
        """
        return (self.exposure, self.stare_text, self.filter_text)

    @property
    def detail(self):
        """The same lines with the gaps closed, for a tooltip or a list."""
        return tuple(x for x in self.rows if x)

    def to_dict(self):
        return {"index": self.index, "image_type": self.image_type,
                "name": self.name, "name_source": self.name_source,
                "exposure_s": self.exposure_s, "exposure": self.exposure,
                "stare_s": self.stare_s, "frame_count": self.frame_count,
                "wavelength_nm": self.wavelength_nm, "units": self.units,
                "collected": self.collected,
                "plan_name": self.plan_name, "plan_source": self.plan_source}


@dataclass(frozen=True)
class Protocol:
    """One run's design, and how much of it has happened.

    Requested and achieved are kept apart everywhere: ``cycle_s`` is what the
    schedule asks for and ``interval_s`` is what the scan times give. Enough
    wells at a long enough exposure overrun the cycle, so they routinely differ,
    and a drawing that showed one number would be showing the wrong one.
    """

    vessel_id: int = 0
    vessel_name: str = ""
    owner: str = ""
    plate: str = ""                # the vessel type, e.g. "24-well Sarstedt"
    plate_rows: int = 0
    plate_cols: int = 0
    steps: tuple = ()
    wells: tuple = ()              # the wells the scan pattern images, in order
    imaged_wells: tuple = ()       # the ones that actually held images
    sites_per_well: int = 0
    pattern_name: str = ""
    magnification: str = ""
    whole_well: bool = False
    objective: str = ""
    optics: str = ""               # the optical module - the camera's analogue
    pixel_size_um: float = None
    started: object = None         # datetime of the vessel's first scan
    last_scan: object = None       # datetime of its most recent
    scan_time: str = ""            # the moment the payload describes
    cycle_s: float = None          # requested, from the schedule
    repeat_times: int = None       # requested, from StopAfterScanCount
    stop_after_hours: float = None
    stop_after: object = None      # datetime, from StopAfterDateTime
    schedule_expired: bool = None
    interval_s: float = None       # achieved, derived from the scan times
    acquired: int = None           # scans on the device; None = nothing looked
    channels_on_disk: int = None   # channels the scan holds; None = not looked
    live: bool = None              # is the instrument acquiring? None = unknown
    activity: str = ""             # what it says it is doing
    tray_position: int = None
    tray_count: int = None
    notes: tuple = ()              # what disagrees with what
    sources: dict = field(default_factory=dict)

    # -- what follows from it ---------------------------------------------

    @property
    def n_wells(self):
        return len(self.wells)

    # PyLV200 calls a field of view a position; an Incucyte calls it a well.
    # These two aliases are here so a script ported between the packages still
    # reads, the same way `OutputFile.frame_count` survives as an alias of
    # `frames`.
    @property
    def positions(self):
        return self.wells

    @property
    def n_positions(self):
        return self.n_wells

    @property
    def plate_format(self):
        total = self.plate_rows * self.plate_cols
        return "%d-well" % total if total else ""

    @property
    def drift(self):
        """Achieved cycle over requested, as a fraction. None if either is."""
        if not self.cycle_s or not self.interval_s:
            return None
        return (self.interval_s - self.cycle_s) / self.cycle_s

    @property
    def progress(self):
        """Fraction of the planned repeats acquired, or None."""
        if self.acquired is None or not self.repeat_times:
            return None
        return min(self.acquired / float(self.repeat_times), 1.0)

    @property
    def frames_per_cycle(self):
        """Images taken per cycle: every channel at every site of every well."""
        return (len(self.steps) * max(self.n_wells, 1)
                * max(self.sites_per_well, 1))

    @property
    def planned_duration_s(self):
        if not self.repeat_times or not (self.interval_s or self.cycle_s):
            return None
        return self.repeat_times * (self.interval_s or self.cycle_s)

    @property
    def elapsed_s(self):
        """Wall clock from the first scan to the last, when both are known."""
        if self.started and self.last_scan:
            return max((self.last_scan - self.started).total_seconds(), 0.0)
        if not self.acquired or not self.interval_s:
            return None
        return (self.acquired - 1) * self.interval_s

    @property
    def channel_names(self):
        return tuple(step.label for step in self.steps)

    def title(self):
        if self.vessel_name:
            return "%d - %s" % (self.vessel_id, self.vessel_name)
        return "Vessel %d" % self.vessel_id if self.vessel_id else "run"

    def to_dict(self):
        return {
            "vessel_id": self.vessel_id,
            "vessel_name": self.vessel_name,
            "owner": self.owner,
            "plate": self.plate,
            "plate_format": self.plate_format,
            "rows": self.plate_rows, "cols": self.plate_cols,
            "started": self.started.isoformat() if self.started else None,
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "scan_time": self.scan_time,
            "objective": self.objective, "optics": self.optics,
            "pixel_size_um": self.pixel_size_um,
            "wells": list(self.wells),
            "imaged_wells": list(self.imaged_wells),
            "sites_per_well": self.sites_per_well,
            "pattern": self.pattern_name,
            "magnification": self.magnification,
            "whole_well": self.whole_well,
            "steps": [s.to_dict() for s in self.steps],
            "cycle_s": self.cycle_s, "repeat_times": self.repeat_times,
            "stop_after_hours": self.stop_after_hours,
            "stop_after": self.stop_after.isoformat() if self.stop_after else None,
            "schedule_expired": self.schedule_expired,
            "interval_s": self.interval_s, "drift": self.drift,
            "acquired": self.acquired, "progress": self.progress,
            "channels_on_disk": self.channels_on_disk,
            "frames_per_cycle": self.frames_per_cycle,
            "planned_duration_s": self.planned_duration_s,
            "elapsed_s": self.elapsed_s,
            "live": self.live, "activity": self.activity,
            "tray_position": self.tray_position, "tray_count": self.tray_count,
            "notes": list(self.notes),
            "sources": dict(self.sources),
        }

    # -- how it is shown --------------------------------------------------

    def lines(self, width=96):
        """The drawing as plain ASCII, for a terminal. Never Unicode.

        A box-drawing character raises ``UnicodeEncodeError`` on a Windows
        console still running a code page, which turns a diagram into a
        traceback on exactly the machine beside the instrument.
        """
        return ascii_drawing(self, width=width)

    def svg(self, theme="light"):
        """The drawing as an SVG document, as a string. Needs nothing."""
        return to_svg(self, theme=theme)

    def save(self, path, theme="light", dpi=200):
        """Write the drawing.

        ``.svg`` costs nothing; ``.png`` and ``.pdf`` need matplotlib, which is
        the ``figure`` extra and is deliberately excluded from the frozen app.
        """
        path = Path(path)
        # No suffix means a folder, whether or not it exists yet: `-o pictures`
        # that quietly wrote a file called `pictures` would be a surprise, and a
        # folder is what every other -o in this package takes.
        if path.is_dir() or not path.suffix:
            path = path / ("%s-protocol.svg" % _safe(self.title()))
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix == ".svg":
            path.write_text(self.svg(theme=theme), encoding="utf-8")
            return path
        if suffix in (".png", ".pdf"):
            return save_figure(self, path, theme=theme, dpi=dpi)
        raise IncucyteError(
            "a protocol drawing is .svg, .png or .pdf; got %r" % path.suffix)

    def __str__(self):
        return "\n".join(self.lines())

    def __repr__(self):
        return "<Protocol %s: %d channel(s), %d well(s)>" % (
            self.title(), len(self.steps), self.n_wells)


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_") or "protocol"


# --------------------------------------------------------------------------
# reading one
# --------------------------------------------------------------------------

def read_protocol(vessel, payload=None, *, scan_times=(), state=None,
                  imaged=None, names=None, name_source="", scan_time="",
                  scan=True):
    """Everything the device states about how a run was set up.

    Pure: it is handed what has already been fetched rather than fetching it,
    the way :mod:`pyincucyte.device` takes a ``call`` callable rather than a
    client.  :meth:`~pyincucyte.client.IncucyteClient.protocol` is the front
    door that does the fetching.

    ``vessel``   a :class:`~pyincucyte.models.Vessel`.
    ``payload``  one unpacked ``GetScanVessel`` response.
    ``scan_times``  every scan time the vessel has, for the achieved cadence.
    ``state``    a :class:`~pyincucyte.device.DeviceState`, for "acquiring now".
    ``imaged``   the wells that actually held images, as ``(row, col)`` pairs.
    ``names``    an override to prefer over the device's own channel names,
                 with ``name_source`` saying where it came from; that is how a
                 recipe's renamed channel reaches the drawing, and why the
                 drawing can say a name was overridden rather than read.
    ``scan=False``  the run's lifetime was not swept and the status was not
                 read. What is lost is the achieved interval, the progress and
                 whether the instrument is acquiring - the three things that
                 make this a picture of a run rather than of a plan.
    """
    data = payload if isinstance(payload, dict) else {}
    sources, notes = {}, []

    steps = _steps(vessel, data, names, name_source)
    if steps:
        sources["channel_names"] = (name_source or "override") if names else (
            "vessel" if getattr(vessel, "channel_labels", None) else "scan")
        if any(s.exposure_s is not None for s in steps):
            sources["exposure"] = "scan payload"

    # -- the well loop -----------------------------------------------------
    pattern = data.get("ScanPattern") or {}
    wells = _pattern_wells(pattern)
    if not wells and imaged:
        wells = tuple(wl.well_name(r, c) for r, c in sorted(imaged))
        notes.append("the scan payload states no scan pattern, so the wells "
                     "below are the ones that held images rather than the ones "
                     "the pattern asks for")
    if wells:
        sources["wells"] = "scan payload"
    sites = int(pattern.get("ImagesPerSwell") or 0)

    imaged_names = tuple(wl.well_name(r, c) for r, c in sorted(imaged or ()))
    if wells and imaged_names and len(imaged_names) < len(wells):
        notes.append(
            "the pattern asks for %d well(s) and this scan holds %d - the "
            "instrument skipped the rest, which a time stack will show as "
            "missing frames" % (len(wells), len(imaged_names)))

    # -- requested, from the schedule --------------------------------------
    cycle_s = _schedule_cycle(data)
    repeats = _int_or_none(data.get("StopAfterScanCount"))
    stop_hours = _float_or_none(data.get("StopAfterHours"))
    stop_at = _datetime_or_none(data.get("StopAfterDateTime"))
    if cycle_s is not None:
        sources["cycle"] = "scan payload"
    if repeats is not None or stop_hours is not None or stop_at is not None:
        sources["schedule"] = "scan payload"
    else:
        notes.append("the scan payload states no stop-after schedule, so how "
                     "many timepoints were asked for is not known - the "
                     "achieved count below is what there is")

    # -- achieved, from the scan times -------------------------------------
    interval_s, acquired = None, None
    if scan:
        stamps = _stamps(scan_times)
        acquired = len(stamps)
        interval_s = median_gap(stamps)
        sources["acquired"] = "scan times"
        if interval_s is not None:
            sources["interval_s"] = "derived from the scan times"
            # `AllScanTimes` takes a date, not a vessel, so what came back is
            # the tray's timetable bounded by this plate's own lifetime. It is
            # this plate's cadence whenever the tray runs one schedule, which
            # is the usual case and not a safe silent assumption.
            notes.append("scan times are device-wide, so this cadence is the "
                         "tray's - it is this plate's too unless the tray is "
                         "running more than one schedule")
        elif acquired < 2:
            notes.append("fewer than two scans exist, so no cadence can be "
                         "derived - a single timepoint has no interval")

    if cycle_s and interval_s:
        drift = (interval_s - cycle_s) / cycle_s
        if abs(drift) > CYCLE_TOLERANCE:
            notes.append(
                "the schedule asks for %s per cycle and the scan times give %s "
                "(%+.0f%%) - the achieved interval is the one that is true"
                % (_duration(cycle_s), _duration(interval_s), 100.0 * drift))

    if not steps:
        notes.append("no channel was read - the scan payload names none, and a "
                     "channel is never inferred here")

    # -- who is holding the instrument -------------------------------------
    live, activity = None, ""
    if state is not None:
        activity = getattr(state, "activity", "") or ""
        live = bool(getattr(state, "is_scanning", False))
        sources["live"] = "device status"
        # Scan times are device-wide and the status does not name a vessel, so
        # "acquiring now" is the instrument's, not this plate's.  Saying which
        # would be a guess, and a wrong one whenever a tray holds three plates.
        if live:
            notes.append("the instrument is scanning, but its status does not "
                         "say which vessel - this may be another plate on the "
                         "tray")

    on_disk = len({s.image_type for s in steps if s.collected}) or None
    optics = data.get("OpticsConfig") or {}

    return Protocol(
        vessel_id=getattr(vessel, "id", 0) or 0,
        vessel_name=getattr(vessel, "name", "") or "",
        owner=getattr(vessel, "owner", "") or "",
        plate=_plate_name(vessel, data),
        plate_rows=int(getattr(vessel, "rows", 0) or 0),
        plate_cols=int(getattr(vessel, "cols", 0) or 0),
        steps=steps, wells=wells, imaged_wells=imaged_names,
        sites_per_well=sites,
        pattern_name=str(pattern.get("Name") or ""),
        magnification=_magnification(pattern, optics),
        whole_well=bool(pattern.get("IsWholeWellSamplePattern") or False),
        objective=_objective(optics), optics=_optics_name(optics),
        pixel_size_um=getattr(vessel, "pixel_size_um", None),
        started=getattr(vessel, "first_scan", None),
        last_scan=getattr(vessel, "last_scan", None),
        scan_time=str(scan_time or data.get("ScanTime") or ""),
        cycle_s=cycle_s, repeat_times=repeats,
        stop_after_hours=stop_hours, stop_after=stop_at,
        schedule_expired=_bool_or_none(data.get("IsScheduleDurationExpired")),
        interval_s=interval_s, acquired=acquired, channels_on_disk=on_disk,
        live=live, activity=activity,
        tray_position=_int_or_none(data.get("TrayPositionIndex")),
        tray_count=_int_or_none(data.get("TotalTrayCount")),
        notes=tuple(notes), sources=sources)


def _steps(vessel, data, names, name_source):
    """The channel chain, in the device's own acquisition order.

    Image type 1 is Phase and comes first because the instrument takes it first.
    A colour that the vessel switched off is left out entirely rather than drawn
    greyed: a node on the page is an exposure that happens.
    """
    channels = data.get("Channels") or {}
    colours = channels.get("Colors") or {}
    cube = ((data.get("OpticsConfig") or {}).get("Cube") or {}).get("Colors") or {}
    labels = getattr(vessel, "channel_labels", None) or {}
    override = list(names or ())

    steps, index = [], 0
    for image_type in ch.ALL_CHANNELS:
        if image_type == ch.PHASE:
            block = channels.get("Phase") or {}
            # Silence is not a yes. Every real payload states `Collected`, so a
            # missing one means no payload was read - and a channel invented
            # from nothing would put an exposure on the page that never
            # happened.
            if not block.get("Collected"):
                continue
            plan_name, entry, band = "", {}, {}
        else:
            key = "Color%d" % (image_type - ch.PHASE)
            entry = colours.get(key) or {}
            if not entry.get("Collected", False):
                continue
            plan_name = str(entry.get("ColorName") or "")
            band = cube.get(key) or {}

        if index < len(override):
            name, kind = str(override[index]), (name_source or "override")
        elif labels.get(image_type):
            name, kind = str(labels[image_type]), "vessel"
        elif plan_name:
            name, kind = plan_name, "scan"
        else:
            name, kind = ch.image_type_label(image_type), "default"

        steps.append(Step(
            index=index, image_type=image_type, name=name, name_source=kind,
            exposure_s=_milliseconds(entry.get("AcquisitionTime")),
            stare_s=_milliseconds(entry.get("StareTime")),
            frame_count=_int_or_none(entry.get("FrameCount")),
            wavelength_nm=_int_or_none(band.get("WaveLength")),
            units=str(band.get("Units") or ""),
            collected=True, plan_name=plan_name,
            plan_source="scan" if entry else ""))
        index += 1
    return tuple(steps)


def _pattern_wells(pattern):
    """The wells the scan pattern images, named and in row-major order."""
    swells = pattern.get("Swells")
    if not isinstance(swells, list):
        return ()
    found = set()
    for swell in swells:
        if not isinstance(swell, dict):
            continue
        row = _int_or_none(swell.get("RowZeroBased"))
        col = _int_or_none(swell.get("ColumnZeroBased"))
        if row is not None and col is not None:
            found.add((row, col))
    return tuple(wl.well_name(r, c) for r, c in sorted(found))


def _schedule_cycle(data):
    """The requested cadence, in seconds, if the payload states one.

    ``ScheduleJob`` is where a cadence would live and it is ``null`` on every
    payload captured so far, so this reads defensively and returns None rather
    than inventing a number.  A requested cycle that is absent is a different
    answer from one that disagrees with the timestamps, and the drawing says so.
    """
    job = data.get("ScheduleJob")
    if not isinstance(job, dict):
        return None
    for key in ("IntervalSeconds", "ScanIntervalSeconds", "PeriodSeconds"):
        value = _float_or_none(job.get(key))
        if value:
            return value
    for key in ("IntervalMinutes", "ScanIntervalMinutes"):
        value = _float_or_none(job.get(key))
        if value:
            return value * 60.0
    for key in ("IntervalHours", "ScanIntervalHours"):
        value = _float_or_none(job.get(key))
        if value:
            return value * 3600.0
    return None


def _plate_name(vessel, data):
    vessel_type = data.get("VesselType") or {}
    return (str(vessel_type.get("Name") or "")
            or str(getattr(vessel, "type_name", "") or ""))


def _objective(optics):
    objective = optics.get("Objective") or {}
    return str(objective.get("Name") or "")


def _optics_name(optics):
    cube = optics.get("Cube") or {}
    return str(cube.get("Name") or "")


def _magnification(pattern, optics):
    objective = optics.get("Objective") or {}
    name = str(objective.get("Name") or "")
    if name:
        return name
    value = pattern.get("Magnification")
    return str(value) if value not in (None, "") else ""


def median_gap(stamps):
    """The median gap between scan times, in seconds, or None under two.

    The median rather than the mean, and for the same reason ``client`` uses it
    for the manifest's interval: an instrument that paused overnight leaves one
    enormous gap, and a mean over it describes no cadence the run ever used.
    Both call sites must agree, or the drawing and the manifest would state
    different cadences for the same plate.
    """
    ordered = sorted(stamps or ())
    if len(ordered) < 2:
        return None
    gaps = sorted((b - a).total_seconds() for a, b in zip(ordered, ordered[1:]))
    return round(gaps[len(gaps) // 2], 3)


def _stamps(scan_times):
    stamps = []
    for scan_time in scan_times or ():
        value = _datetime_or_none(scan_time)
        if value is not None:
            stamps.append(value)
    return stamps


def _milliseconds(value):
    """The device states acquisition and stare times in milliseconds."""
    number = _float_or_none(value)
    return None if not number else number / 1000.0


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value):
    return None if value is None else bool(value)


def _datetime_or_none(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return parse_scan_datetime(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


# --------------------------------------------------------------------------
# formatting the numbers
# --------------------------------------------------------------------------

def _duration(seconds):
    """A span in the unit a person would say it in."""
    if seconds is None:
        return "?"
    seconds = float(seconds)
    if seconds < 90:
        return "%.4g s" % seconds
    if seconds < 5400:
        return "%.1f min" % (seconds / 60.0)
    if seconds < 172800:
        return "%.1f h" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)


def _cycle_text(protocol):
    """The loop's own headline: what was asked for, and what happened."""
    bits = []
    if protocol.repeat_times and protocol.cycle_s:
        bits.append("%d x %s requested"
                    % (protocol.repeat_times, _duration(protocol.cycle_s)))
    elif protocol.repeat_times:
        bits.append("%d scans requested" % protocol.repeat_times)
    elif protocol.cycle_s:
        bits.append("%s per cycle requested" % _duration(protocol.cycle_s))
    elif protocol.stop_after_hours:
        bits.append("%s requested" % _duration(protocol.stop_after_hours * 3600.0))
    if protocol.interval_s:
        drift = protocol.drift
        bits.append("%s achieved%s"
                    % (_duration(protocol.interval_s),
                       " (%+.0f%%)" % (100.0 * drift)
                       if drift and abs(drift) > CYCLE_TOLERANCE else ""))
    return "   -   ".join(bits) or "cadence not known"


def _progress_text(protocol):
    if protocol.acquired is None:
        return ""
    if protocol.repeat_times:
        text = "%d of %d timepoints acquired" % (protocol.acquired,
                                                 protocol.repeat_times)
        if protocol.progress is not None:
            text += " (%.0f%%)" % (100.0 * protocol.progress)
    else:
        text = "%d timepoint(s) acquired" % protocol.acquired
    if protocol.elapsed_s:
        text += ",  %s so far" % _duration(protocol.elapsed_s)
    if protocol.planned_duration_s:
        text += " of %s" % _duration(protocol.planned_duration_s)
    return text


def _wells_text(protocol, limit=6):
    n = protocol.n_wells
    head = "All wells (%d)" % n if n != 1 else "One well"
    bits = []
    # The vessel type usually names its own format ("24-well Sarstedt"), so
    # prefixing the derived one gives "24-well 24-well Sarstedt".
    plate = protocol.plate or ""
    if plate and protocol.plate_format not in plate:
        plate = "%s %s" % (protocol.plate_format, plate)
    if plate or protocol.plate_format:
        bits.append(plate or "%s plate" % protocol.plate_format)
    if protocol.sites_per_well:
        bits.append("%d site%s each" % (protocol.sites_per_well,
                                        "" if protocol.sites_per_well == 1
                                        else "s"))
    shown = list(protocol.wells[:limit])
    if shown:
        tail = ",  ".join(shown)
        if n > limit:
            tail += ",  +%d more" % (n - limit)
        bits.append(tail)
    return head, "   ".join(bits)


#: What separates the parts of a footer line. The drawing takes a middle dot;
#: the terminal cannot - a Windows console still on a code page raises
#: `UnicodeEncodeError` on it, which turns a diagram into a traceback.
SEP, ASCII_SEP = " · ", " | "


def _provenance(protocol, sep=SEP):
    """One line saying where the page came from - in the picture rather than in
    a footnote nobody reads."""
    bits = []
    kind = protocol.sources.get("channel_names")
    if kind == "vessel":
        bits.append("channel names read from the vessel record")
    elif kind:
        bits.append("channel names %s" % kind)
    if protocol.sources.get("exposure"):
        bits.append("exposures and optics from GetScanVessel")
    if protocol.sources.get("wells"):
        bits.append("wells and sites from the scan pattern")
    if protocol.interval_s:
        bits.append("achieved interval is the median gap between scan times")
    if protocol.acquired is not None:
        bits.append("progress counted from the device's scan times")
    if protocol.sources.get("live"):
        bits.append("acquiring state read from the device status")
    return sep.join(bits)


def _instrument(protocol, sep=SEP):
    bits = []
    if protocol.optics:
        bits.append(protocol.optics)
    if protocol.objective:
        bits.append("%s objective" % protocol.objective)
    if protocol.pixel_size_um:
        bits.append("%.3f um/pixel" % protocol.pixel_size_um)
    if protocol.tray_position and protocol.tray_count:
        bits.append("tray %d of %d" % (protocol.tray_position,
                                       protocol.tray_count))
    if protocol.started:
        bits.append("started %s" % protocol.started.strftime("%Y-%m-%d %H:%M"))
    if protocol.activity:
        bits.append("instrument %s" % protocol.activity.lower())
    return sep.join(bits)


# --------------------------------------------------------------------------
# the geometry
#
# One layout, three renderers. The shapes are plain dicts in a top-left origin
# with y increasing downwards, which is SVG's own convention; the matplotlib
# writer inverts one axis and otherwise draws exactly the same list.
# --------------------------------------------------------------------------

PAD = 22                       # canvas margin
TITLE_SIZE = 16
HEAD_H = 32                    # a group box's header strip
BOX_PAD = 16
NODE_W, NODE_H = 152, 100
NODE_GAP = 34                  # the arrow lives in here
BAR_H = 9
PIP, PIP_GAP = 9, 4
MIN_W = 660
FONT_STACK = ("Segoe UI", "Inter", "Helvetica Neue", "Arial", "DejaVu Sans")
FONT = ", ".join(FONT_STACK) + ", sans-serif"


def _text_width(text, size, bold=False):
    """A good-enough advance width for a sans face, in points.

    Deliberately approximate: it decides whether a label is trimmed, and being a
    few percent generous costs nothing where being wrong costs a clipped name.
    """
    return len(str(text)) * size * (0.63 if bold else 0.56)


def _fit(text, size, limit, bold=False):
    """Trim to fit, with an ellipsis, rather than letting it run over a box."""
    text = str(text)
    if _text_width(text, size, bold) <= limit:
        return text
    keep = max(int(limit / (size * (0.63 if bold else 0.56))) - 1, 1)
    return text[:keep].rstrip() + "..."


def _wrap(text, size, limit):
    """Break a footer line into as many as it needs. Words are never split."""
    words, lines, current = str(text).split(), [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if current and _text_width(trial, size) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _rect(x, y, w, h, fill, stroke=None, radius=8, width=1.0, dash=None,
          title=""):
    return {"kind": "rect", "x": x, "y": y, "w": w, "h": h, "r": radius,
            "fill": fill, "stroke": stroke, "width": width, "dash": dash,
            "title": title}


def _text(x, y, text, size, fill, anchor="start", weight="normal"):
    return {"kind": "text", "x": x, "y": y, "text": str(text), "size": size,
            "fill": fill, "anchor": anchor, "weight": weight}


def _line(x1, y1, x2, y2, stroke, width=1.0, dash=None):
    return {"kind": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "stroke": stroke, "width": width, "dash": dash}


def _poly(points, fill):
    return {"kind": "poly", "points": [(float(x), float(y)) for x, y in points],
            "fill": fill}


def layout(protocol, theme="light"):
    """``(width, height, shapes)`` - the drawing, before any file format.

    Laid out from the channel chain outwards, so the page is exactly as wide as
    the run needs and a two-channel protocol does not sit in the corner of a
    four-channel canvas.
    """
    c = palette(theme)
    steps = list(protocol.steps)
    n = max(len(steps), 1)

    # The node is sized to its contents, not the contents to the node. A run
    # with a channel called BioLuminescence and one with three called Phase,
    # Green, Red are both drawn tightly, and neither has a name spilling over an
    # edge.
    node_w = NODE_W
    rows = [i for i in range(3) if any(s.rows[i] for s in steps)]
    if steps:
        node_w = max([NODE_W]
                     + [_text_width(s.label, 12, True) + 56 for s in steps]
                     + [_text_width(s.rows[i], 11) + 34
                        for s in steps for i in rows])
        node_w = min(node_w, 3 * NODE_W)

    chain_w = n * node_w + (n - 1) * NODE_GAP
    width = max(MIN_W, chain_w + 2 * (BOX_PAD + BOX_PAD + PAD))
    inner_x = PAD + BOX_PAD
    inner_w = width - 2 * inner_x
    outer_w = width - 2 * PAD

    shapes = []
    y = PAD

    # -- the title band ----------------------------------------------------
    shapes.append(_text(PAD, y + TITLE_SIZE,
                        _fit(protocol.title(), TITLE_SIZE, outer_w * 0.62,
                             bold=True),
                        TITLE_SIZE, c["text"], weight="bold"))
    right = PAD + outer_w
    if protocol.live:
        label = "instrument acquiring"
        shapes.append(_text(right, y + TITLE_SIZE - 1, label, 10.5, c["live"],
                            anchor="end"))
        shapes.append({"kind": "circle",
                       "cx": right - _text_width(label, 10.5) - 12,
                       "cy": y + TITLE_SIZE - 5, "r": 4.5, "fill": c["live"],
                       "title": "the instrument reports %s - its status does "
                                "not say which vessel"
                                % (protocol.activity or "scanning")})
    elif protocol.live is False:
        shapes.append(_text(right, y + TITLE_SIZE - 1,
                            protocol.activity.lower() or "not acquiring",
                            10.5, c["muted"], anchor="end"))
    y += TITLE_SIZE + 14

    # -- the outer box: the time loop --------------------------------------
    outer_y = y
    shapes.append(None)                       # placeholder, sized below
    outer_index = len(shapes) - 1

    # The circular arrow is the whole reason the box reads as a loop rather than
    # as an outline round a list.
    icon_x = PAD + BOX_PAD + 8
    shapes.extend(_loop_icon(icon_x, outer_y + 16, 7.5, c["accent"]))
    head_x = icon_x + 18
    shapes.append(_text(head_x, outer_y + 21, "Time loop", 12, c["text"],
                        weight="bold"))
    used = head_x + _text_width("Time loop", 12, True) + 14
    shapes.append(_text(used, outer_y + 21,
                        _fit(_cycle_text(protocol), 11,
                             PAD + outer_w - BOX_PAD - used),
                        11, c["muted"]))
    y = outer_y + HEAD_H

    # -- progress ----------------------------------------------------------
    progress_text = _progress_text(protocol)
    if progress_text:
        bar_x, bar_w = PAD + BOX_PAD, outer_w - 2 * BOX_PAD
        shapes.append(_rect(bar_x, y, bar_w, BAR_H, c["track"],
                            radius=BAR_H / 2))
        fraction = protocol.progress
        if fraction:
            shapes.append(_rect(bar_x, y, max(bar_w * fraction, BAR_H), BAR_H,
                                c["accent"], radius=BAR_H / 2,
                                title=progress_text))
        y += BAR_H + 15
        shapes.append(_text(bar_x, y, _fit(progress_text, 10.5, bar_w), 10.5,
                            c["muted"]))
        y += 10

    y += BOX_PAD

    # -- the inner box: every well -----------------------------------------
    inner_y = y
    shapes.append(None)
    inner_index = len(shapes) - 1

    head, tail = _wells_text(protocol)
    shapes.append(_text(inner_x + BOX_PAD, inner_y + 21, head, 12, c["text"],
                        weight="bold"))
    used = _text_width(head, 12, True) + 16
    if tail:
        shapes.append(_text(inner_x + BOX_PAD + used, inner_y + 21,
                            _fit(tail, 10.5, inner_w - 2 * BOX_PAD - used - 8),
                            10.5, c["muted"]))
    y = inner_y + HEAD_H

    # One pip per well the pattern images, filled where this scan actually held
    # images. The vendor's own graph says "All wells" and stops there; which of
    # the twenty-four the instrument skipped is the question somebody actually
    # has when a time stack comes back short.
    if protocol.wells:
        imaged = set(protocol.imaged_wells)
        pip_x = inner_x + BOX_PAD
        limit = int((inner_w - 2 * BOX_PAD) // (PIP + PIP_GAP))
        for name in protocol.wells[:limit]:
            got = (not imaged) or name in imaged
            shapes.append(_rect(pip_x, y, PIP, PIP,
                                c["accent"] if got else c["border_strong"],
                                radius=2,
                                title="%s%s" % (name, "" if got
                                                else " - no image in this scan")))
            pip_x += PIP + PIP_GAP
        if protocol.n_wells > limit:
            shapes.append(_text(pip_x + 2, y + PIP,
                                "+%d" % (protocol.n_wells - limit), 9,
                                c["muted"]))
        y += PIP + 12

    # -- the chain ---------------------------------------------------------
    nodes_y = y
    start_x = PAD + (outer_w - chain_w) / 2.0
    node_h = NODE_H - 16 * (3 - len(rows)) if steps else NODE_H
    if steps:
        for i, step in enumerate(steps):
            x = start_x + i * (node_w + NODE_GAP)
            shapes.extend(_node(step, x, nodes_y, node_w, node_h, rows, c,
                                theme))
            if i:
                shapes.extend(_arrow(x - NODE_GAP, x, nodes_y + node_h / 2.0,
                                     c["border_strong"]))
    else:
        shapes.append(_rect(start_x, nodes_y, node_w, node_h, c["surface_alt"],
                            c["border"], dash="4 3"))
        shapes.append(_text(start_x + node_w / 2.0, nodes_y + node_h / 2.0,
                            "channels", 11, c["muted"], anchor="middle"))
        shapes.append(_text(start_x + node_w / 2.0, nodes_y + node_h / 2.0 + 15,
                            "not known", 11, c["muted"], anchor="middle"))
    y = nodes_y + node_h + BOX_PAD

    shapes[inner_index] = _rect(inner_x, inner_y, inner_w, y - inner_y,
                                c["surface_alt"], c["border"], radius=10)
    y += BOX_PAD
    shapes[outer_index] = _rect(PAD, outer_y, outer_w, y - outer_y,
                                c["surface"], c["border_strong"], radius=12)

    # -- the footer --------------------------------------------------------
    y += 22
    for text, colour, size in ((_instrument(protocol), c["muted"], 10.5),
                               (_provenance(protocol), c["muted"], 9.5)):
        for line in _wrap(text, size, outer_w) if text else ():
            shapes.append(_text(PAD, y, line, size, colour))
            y += size + 5
    for note in protocol.notes:
        # The prefix is built before the loop, not inside the expression being
        # enumerated: `"** " + note if not i else note` reads `i` from the
        # enclosing scope, where the chain loop above left it, so the marker
        # silently vanished - and raised NameError on a run with no channels
        # at all, which is exactly the run that most needs its notes read.
        wrapped = _wrap("** " + note, 9.5, outer_w)
        for line_no, line in enumerate(wrapped):
            shapes.append(_text(PAD + (0 if not line_no else 14), y, line, 9.5,
                                c["warning"]))
            y += 14

    height = y + PAD - 8
    return width, height, [s for s in shapes if s]


def _node(step, x, y, w, h, rows, c, theme):
    """One channel: a coloured tab, the name, and what the plan asks of it.

    ``rows`` is which of the three roles the chain as a whole uses, so every
    node in a chain has its exposure, its stare time and its band on the same
    line as its neighbours' - and a role nobody fills costs no space at all.
    """
    colour = channel_colour(step.label, theme, image_type=step.image_type)
    tip = "  |  ".join([step.label] + list(step.detail)
                       + ([("name %s" % step.name_source)]
                          if step.name_source else []))
    out = [_rect(x, y, w, h, c["surface_alt"], c["border"], radius=9,
                 title=tip),
           # A tab, not a fill: a colour chosen from a NAME must not read as a
           # property of the pixels, which are single-band detector images.
           _rect(x, y, 5, h, colour, radius=2.5),
           {"kind": "circle", "cx": x + 20, "cy": y + 24, "r": 6.5,
            "fill": colour}]
    out.append(_text(x + 34, y + 28, _fit(step.label, 12, w - 44, True),
                     12, c["text"], weight="bold"))
    top = y + 50
    for line, i in zip((step.rows[i] for i in rows), range(len(rows))):
        if not line:
            continue
        out.append(_text(x + 16, top + i * 16, _fit(line, 11, w - 28),
                         11 if not i else 10,
                         c["text"] if not i else c["muted"]))
    if not step.detail:
        out.append(_text(x + 16, top, "transmitted light", 10, c["muted"]))
    return out


def _arrow(x1, x2, y, colour):
    return [_line(x1, y, x2 - 7, y, colour, 1.4),
            _poly([(x2 - 1, y), (x2 - 9, y - 4.5), (x2 - 9, y + 4.5)], colour)]


def _loop_icon(cx, cy, r, colour):
    """A circular arrow: three-quarters of a circle, with a head on the end."""
    points = [(cx + r * math.cos(math.radians(a)),
               cy - r * math.sin(math.radians(a)))
              for a in range(130, 401, 10)]
    end = math.radians(400)
    tip = (cx + r * math.cos(end + 0.34), cy - r * math.sin(end + 0.34))
    return [{"kind": "polyline", "points": points, "stroke": colour,
             "width": 1.8},
            _poly([tip,
                   (cx + (r + 3.4) * math.cos(end),
                    cy - (r + 3.4) * math.sin(end)),
                   (cx + (r - 3.4) * math.cos(end),
                    cy - (r - 3.4) * math.sin(end))], colour)]


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------

def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_svg(protocol, theme="light"):
    """The drawing as one self-contained SVG document.

    No external font, no script, no image: it opens in a browser, in Illustrator
    and in a slide, and it costs no plotting stack to produce - which matters
    because matplotlib is excluded from the frozen desktop app outright.
    """
    c = palette(theme)
    width, height, shapes = layout(protocol, theme=theme)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="%s">'
           % (round(width), round(height), round(width), round(height),
              _esc(FONT)),
           "<title>%s - acquisition protocol</title>" % _esc(protocol.title()),
           '<rect width="100%%" height="100%%" fill="%s"/>' % c["page"]]
    for shape in shapes:
        out.append(_svg_shape(shape))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _svg_shape(s):
    kind = s["kind"]
    if kind == "rect":
        bits = ['<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" '
                'fill="%s"'
                % (s["x"], s["y"], s["w"], s["h"], s["r"], s["fill"])]
        if s.get("stroke"):
            bits.append(' stroke="%s" stroke-width="%.2f"'
                        % (s["stroke"], s.get("width") or 1.0))
        if s.get("dash"):
            bits.append(' stroke-dasharray="%s"' % s["dash"])
        return _wrapped("".join(bits) + "/>", s.get("title"))
    if kind == "circle":
        return _wrapped('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>'
                        % (s["cx"], s["cy"], s["r"], s["fill"]),
                        s.get("title"))
    if kind == "text":
        anchor = {"start": "start", "middle": "middle",
                  "end": "end"}[s["anchor"]]
        weight = ' font-weight="600"' if s["weight"] == "bold" else ""
        return ('<text x="%.1f" y="%.1f" font-size="%.1f" fill="%s" '
                'text-anchor="%s"%s>%s</text>'
                % (s["x"], s["y"], s["size"], s["fill"], anchor, weight,
                   _esc(s["text"])))
    if kind == "line":
        dash = ' stroke-dasharray="%s"' % s["dash"] if s.get("dash") else ""
        return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                'stroke-width="%.2f" stroke-linecap="round"%s/>'
                % (s["x1"], s["y1"], s["x2"], s["y2"], s["stroke"],
                   s["width"], dash))
    if kind == "poly":
        pts = " ".join("%.1f,%.1f" % p for p in s["points"])
        return '<polygon points="%s" fill="%s"/>' % (pts, s["fill"])
    if kind == "polyline":
        pts = " ".join("%.1f,%.1f" % p for p in s["points"])
        return ('<polyline points="%s" fill="none" stroke="%s" '
                'stroke-width="%.2f" stroke-linecap="round"/>'
                % (pts, s["stroke"], s["width"]))
    raise IncucyteError("no SVG for shape %r" % kind)


def _wrapped(markup, title):
    """A shape with a tooltip. ``<title>`` is the whole mechanism - a browser
    shows it on hover with no script, which is how twenty-four pips can each
    name their own well without a legend."""
    if not title:
        return markup
    return "<g>%s<title>%s</title></g>" % (markup, _esc(title))


# --------------------------------------------------------------------------
# matplotlib, for a raster or a PDF
# --------------------------------------------------------------------------

def save_figure(protocol, path, theme="light", dpi=200):
    """The same shapes, through matplotlib, for a ``.png`` or a ``.pdf``.

    matplotlib is imported here and nowhere else in this module. It is the
    ``figure`` extra, it is not a dependency of the package, and the frozen
    desktop app excludes it - so this path is allowed to be the one that is
    missing, and the SVG is the one that always works.
    """
    try:
        from matplotlib import font_manager
        from matplotlib.patches import FancyBboxPatch, Polygon
        import matplotlib.pyplot as plt
    except ImportError as exc:                       # pragma: no cover - env
        raise IncucyteError(
            "a .png or .pdf protocol drawing needs matplotlib: "
            "pip install PyIncucyte[figure]. An .svg needs nothing.") from exc

    # Only families this machine actually has. Handing matplotlib the whole CSS
    # stack makes it warn once per string for every name it cannot find, and a
    # hundred findfont warnings is how a working command reads as a broken one.
    have = {f.name for f in font_manager.fontManager.ttflist}
    family = [name for name in FONT_STACK if name in have] or ["DejaVu Sans"]
    c = palette(theme)
    width, height, shapes = layout(protocol, theme=theme)

    fig = plt.figure(figsize=(width / 72.0, height / 72.0))
    fig.patch.set_facecolor(c["page"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)                 # y downwards, as the layout has it
    ax.set_axis_off()
    ax.set_facecolor(c["page"])

    for s in shapes:
        kind = s["kind"]
        if kind == "rect":
            ax.add_patch(FancyBboxPatch(
                (s["x"], s["y"]), s["w"], s["h"],
                boxstyle="round,pad=0,rounding_size=%.2f" % s["r"],
                linewidth=(s.get("width") or 1.0) if s.get("stroke") else 0,
                edgecolor=s.get("stroke") or "none", facecolor=s["fill"],
                linestyle=(0, (4, 3)) if s.get("dash") else "solid",
                mutation_aspect=1))
        elif kind == "circle":
            ax.add_patch(plt.Circle((s["cx"], s["cy"]), s["r"], color=s["fill"],
                                    linewidth=0))
        elif kind == "text":
            ax.text(s["x"], s["y"], s["text"], fontsize=s["size"],
                    color=s["fill"], va="baseline", fontfamily=family,
                    ha={"start": "left", "middle": "center",
                        "end": "right"}[s["anchor"]],
                    fontweight="semibold" if s["weight"] == "bold"
                    else "normal")
        elif kind == "line":
            ax.plot([s["x1"], s["x2"]], [s["y1"], s["y2"]], color=s["stroke"],
                    linewidth=s["width"], solid_capstyle="round",
                    linestyle=(0, (4, 3)) if s.get("dash") else "solid")
        elif kind == "poly":
            ax.add_patch(Polygon(s["points"], closed=True, color=s["fill"],
                                 linewidth=0))
        elif kind == "polyline":
            ax.plot([p[0] for p in s["points"]], [p[1] for p in s["points"]],
                    color=s["stroke"], linewidth=s["width"],
                    solid_capstyle="round")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path), dpi=dpi, facecolor=c["page"])
    plt.close(fig)
    return path


# --------------------------------------------------------------------------
# the terminal
# --------------------------------------------------------------------------

def _frame(lines, title="", width=96):
    """An ASCII box around a block, with a title cut into its top edge.

    ``width`` is a ceiling, not a target: the box is as wide as it needs to be
    and never wider than the terminal, because a box that wraps is not a box.
    """
    body_w = min(max([len(x) for x in lines] + [len(title) + 4, 8]),
                 max(width - 4, 8))
    title = title[:body_w - 4] if title else ""
    head = "+- %s " % title if title else "+"
    head = head + "-" * max(body_w + 3 - len(head), 1) + "+"
    out = [head]
    out += ["| %s |" % x[:body_w].ljust(body_w) for x in lines]
    out.append("+" + "-" * (body_w + 2) + "+")
    return out


def _cells(steps, cap=20):
    """One ASCII box per channel, all the same height, joined by arrows.

    The bodies are padded to a common height BEFORE the boxes are drawn, so
    every box is the same size and the arrows meet their sides. Padding
    afterwards leaves a chain that steps down the page as the detail runs out.
    """
    rows = [i for i in range(3) if any(s.rows[i] for s in steps)]
    bodies = []
    for step in steps:
        lines = [step.rows[i] for i in rows]
        # Phase is transmitted light: it has no exposure, no stare time and no
        # band, so its box would otherwise be a name over blank lines.
        if not step.detail and lines:
            lines[0] = "transmitted light"
        bodies.append([step.label] + lines)
    blocks = []
    for body in bodies:
        inner = min(max(len(x) for x in body), cap)
        block = ["+" + "-" * (inner + 2) + "+"]
        block += ["| %s |" % x[:inner].ljust(inner) for x in body]
        block.append("+" + "-" * (inner + 2) + "+")
        blocks.append(block)

    middle = len(blocks[0]) // 2
    return [(" -> " if r == middle else "    ").join(b[r] for b in blocks)
            for r in range(len(blocks[0]))]


def ascii_drawing(protocol, width=96):
    """The whole protocol as ASCII, for a terminal. Pure ASCII, deliberately.

    A Windows console still running a code page raises ``UnicodeEncodeError`` on
    a box-drawing character, which turns a diagram into a traceback on exactly
    the machine beside the instrument.
    """
    steps = list(protocol.steps)
    inner_width = width - 8            # two frames of "| " and " |" around it
    chain = _cells(steps) if steps else ["channels not known"]
    if steps and max(len(x) for x in chain) > inner_width - 4:
        # Too many channels for one row. A list says the same thing and a chain
        # trimmed to fit says something else entirely.
        chain = ["%d. %-22s %s" % (s.index + 1, s.label,
                                   "   ".join(s.detail) or "transmitted light")
                 for s in steps]

    head, tail = _wells_text(protocol, limit=6)
    inner = _frame(chain, title="%s%s" % (head, "   " + tail if tail else ""),
                   width=inner_width)

    body = []
    progress = _progress_text(protocol)
    if progress:
        bar = _bar(protocol, min(inner_width - 8, 40))
        body.extend(_wrap_chars(("%s  %s" % (bar, progress)).strip(),
                                inner_width - 2))
        body.append("")
    body.extend(inner)
    lines = _frame(body, title="Time loop:  %s" % _cycle_text(protocol),
                   width=width - 4)

    out = ["", "=== %s ===" % protocol.title(), ""]
    out.extend(lines)
    out.append("")
    for text in (_instrument(protocol, ASCII_SEP),
                 _provenance(protocol, ASCII_SEP)):
        if text:
            out.extend("  " + x for x in _wrap_chars(text, width - 2))
    for note in protocol.notes:
        out.extend(("  ** " if not i else "     ") + x
                   for i, x in enumerate(_wrap_chars(note, width - 6)))
    return out


def _wrap_chars(text, limit):
    """Wrap on words at a column count, for the terminal rather than the page."""
    words, lines, current = str(text).split(), [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if current and len(trial) > max(int(limit), 8):
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _bar(protocol, width):
    """A text progress bar. ``#`` and ``.``, because a block character is not
    ASCII."""
    fraction = protocol.progress
    if fraction is None:
        return ""
    done = int(round(width * fraction))
    return "[" + "#" * done + "." * (width - done) + "]"


__all__ = ["Protocol", "Step", "read_protocol", "layout", "to_svg",
           "save_figure", "ascii_drawing", "channel_colour", "palette",
           "format_exposure", "median_gap", "LIGHT", "DARK", "THEMES",
           "CYCLE_TOLERANCE"]
