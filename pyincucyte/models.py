"""Typed records returned by the PyIncucyte API.

Everything a downstream pipeline needs to find its images - which well, which
channel, which timepoint, which file on disk - is on these objects, and every
one of them serialises to plain JSON via ``to_dict()``.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from . import channels as ch
from . import wells as wl
from .engine import format_elapsed, parse_scan_datetime, vessel_id_from_record

# ---------------------------------------------------------------------------
# Output layouts
# ---------------------------------------------------------------------------

#: layout name -> (writes an ImageJ channel axis, writes an ImageJ time axis)
LAYOUTS = {
    "separate": (False, False),
    "channel_stack": (True, False),
    "time_stack": (False, True),
    "time_channel_stack": (True, True),
}

LAYOUT_DESCRIPTIONS = {
    "separate": "One plain TIFF per well, channel and scan time",
    "channel_stack": "One ImageJ CYX stack per well and scan time",
    "time_stack": "One ImageJ TYX stack per well and channel",
    "time_channel_stack": "One ImageJ TCYX stack per well",
}

LAYOUT_LABELS = {
    "separate": "Separate TIFFs",
    "channel_stack": "Channel hyperstack",
    "time_stack": "Time stack",
    "time_channel_stack": "Time + channel hyperstack",
}

LAYOUT_AXES = {
    "separate": "YX",
    "channel_stack": "CYX",
    "time_stack": "TYX",
    "time_channel_stack": "TCYX",
}

#: Accepted aliases so old flags and friendly names both resolve.
LAYOUT_ALIASES = {
    "single": "separate", "images": "separate", "tiff": "separate",
    "hyperstack": "channel_stack", "channels": "channel_stack",
    "time": "time_stack", "timestack": "time_stack",
    "time_hyper": "time_channel_stack", "time+channel": "time_channel_stack",
    "time_hyperstack": "time_channel_stack",
}

#: Rough per-image payload sizes, used only for pre-flight size estimates.
DEFAULT_IMAGE_BYTES = {1: 1_500_000, 2: 2_950_000, 3: 2_950_000}


def resolve_layout(name):
    """Return a canonical layout name, accepting aliases. Raises ValueError."""
    if not name:
        return "separate"
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    key = LAYOUT_ALIASES.get(key, key)
    if key not in LAYOUTS:
        raise ValueError(
            f"Unknown layout {name!r}. Use one of: {', '.join(LAYOUTS)}")
    return key


def layout_flags(name):
    """Return ``(hyperstack, time_stack)`` for a layout name."""
    return LAYOUTS[resolve_layout(name)]


def layout_from_flags(hyperstack, time_stack):
    """Return the layout name matching the old boolean pair."""
    for name, flags in LAYOUTS.items():
        if flags == (bool(hyperstack), bool(time_stack)):
            return name
    return "separate"


def human_bytes(size):
    """Format a byte count for humans (1.4 GB, 812.0 MB, 96 B)."""
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def _plural(count, word):
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def derived(value, source):
    """A value paired with where it came from.

    The pipeline's shared manifest shape carries provenance on anything that
    was not read straight off an instrument, so a consumer - or Jamie in six
    months - can tell a measured 1800 apart from a typed one.  ``None`` still
    gets a reason: "the device does not report it" and "nobody has looked" are
    different answers to somebody deciding whether to trust a number.
    """
    return {"value": value, "source": source}


def microns_per_pixel(record):
    """The pixel size a raw vessel record states, in microns, or None.

    ``GetAllSearchVessels`` carries it on every vessel as
    ``ImageSize.MicronsPerPixel``.  Nothing else in this package knows it, and
    every micron-denominated measurement downstream needs it.
    """
    size = record.get("ImageSize") if isinstance(record, dict) else None
    if not isinstance(size, dict):
        return None
    try:
        value = float(size.get("MicronsPerPixel"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Vessel
# ---------------------------------------------------------------------------

@dataclass
class Vessel:
    """One plate/vessel on the device, with its plate geometry and channels."""

    id: int
    name: str = ""
    owner: str = ""
    type_name: str = ""
    scan_type: str = ""
    rows: int = 8
    cols: int = 12
    first_scan: datetime = None
    last_scan: datetime = None
    channel_labels: dict = field(default_factory=dict)
    active_channels: set = field(default_factory=set)
    #: Microns per pixel, read straight off the vessel record - not inferred
    #: from the objective.  None when the device did not state one.
    pixel_size_um: float = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_record(cls, record):
        """Build a Vessel from a raw ``GetAllSearchVessels`` entry."""
        if not isinstance(record, dict):
            return None
        vessel_id = vessel_id_from_record(record)
        if vessel_id is None:
            return None
        doc = record.get("VesselDocumentation") or {}
        type_name = record.get("VesselTypeName", "") or ""
        rows, cols = wl.guess_plate_size(type_name)
        vessel_channels = record.get("Channels") or {}

        def _dt(value):
            if not value:
                return None
            try:
                return parse_scan_datetime(value)
            except (ValueError, TypeError, AttributeError):
                return None

        return cls(
            id=vessel_id,
            name=doc.get("Label", "") or "",
            owner=doc.get("UserName", "") or "",
            type_name=type_name,
            scan_type=record.get("ScanTypeDisplayText", "") or "",
            rows=rows, cols=cols,
            first_scan=_dt(record.get("FirstScanDateTime")),
            last_scan=_dt(record.get("LastScanDateTime")),
            channel_labels=ch.labels_for_vessel(vessel_channels),
            active_channels=ch.active_channels(vessel_channels),
            pixel_size_um=microns_per_pixel(record),
            raw=record,
        )

    @property
    def well_count(self):
        return self.rows * self.cols

    @property
    def plate_format(self):
        return f"{self.well_count}-well"

    @property
    def channel_summary(self):
        """e.g. ``"Phase + GFP"`` for the channels this vessel actually uses."""
        if not self.active_channels:
            return ""
        return ch.format_channels(self.active_channels, self.channel_labels)

    @property
    def label(self):
        return f"{self.id} - {self.name}" if self.name else str(self.id)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "owner": self.owner,
            "type_name": self.type_name, "scan_type": self.scan_type,
            "rows": self.rows, "cols": self.cols,
            "first_scan": self.first_scan.isoformat() if self.first_scan else None,
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
            "channel_labels": {str(k): v for k, v in self.channel_labels.items()},
            "active_channels": sorted(self.active_channels),
            "pixel_size_um": self.pixel_size_um,
        }


# ---------------------------------------------------------------------------
# One vessel at one moment
# ---------------------------------------------------------------------------

@dataclass
class VesselScan:
    """One vessel at one scan time - the unit a preview is taken from.

    :meth:`~pyincucyte.client.IncucyteClient.find_scans` returns these, and
    each one knows how to show itself, so finding a plate and looking at it is
    two steps rather than a lookup table::

        scan = incucyte.find_scans(name="Cry1", most_recent=1)[0]
        scan.preview(wells="A1-B3").show()

    ``wells`` and ``channels`` describe what the device actually holds for this
    moment, which is not the same as what the plate is capable of - a scan can
    miss wells, and an experiment can be reconfigured mid-run.
    """

    vessel: Vessel
    scan_time: str                                  # exactly as the device says it
    wells: set = None                               # (row, col) holding images
    channels: set = field(default_factory=set)      # ImageType numbers present
    sites: set = field(default_factory=set)
    image_count: int = 0
    #: (row, col, site) -> {channel: {scale, bias, median}} for fluorescence.
    coefficients: dict = field(default_factory=dict, repr=False)
    #: What the Incucyte software has saved for this vessel, ready to adjust.
    unmixing: object = None
    #: Reduced viewer resolutions advertised by GetScanVessel.  Kept in their
    #: raw JSON shape because the private route is capability-detected later.
    pyramid_levels: list = field(default_factory=list, repr=False)
    client: object = field(default=None, repr=False)

    # -- identity ---------------------------------------------------------

    @property
    def vessel_id(self):
        return self.vessel.id

    @property
    def name(self):
        return self.vessel.name

    @property
    def when(self):
        """The scan time as a datetime, or None if the device sent nonsense."""
        try:
            return parse_scan_datetime(str(self.scan_time))
        except (ValueError, TypeError, AttributeError):
            return None

    @property
    def elapsed(self):
        """Time since the vessel's first scan, e.g. ``"01d06h30m"``."""
        when, first = self.when, self.vessel.first_scan
        if when is None or first is None:
            return ""
        return format_elapsed(when - first)

    @property
    def well_count(self):
        return len(self.wells) if self.wells else self.vessel.well_count

    @property
    def well_names(self):
        return [wl.well_name(r, c) for r, c in sorted(self.wells or ())]

    @property
    def channel_summary(self):
        active = self.channels or self.vessel.active_channels
        return ch.format_channels(active, self.vessel.channel_labels)

    @property
    def label(self):
        """e.g. ``"Vessel 38 - Cry1 plate - 2026-03-03 09:00"``."""
        when = self.when
        stamp = f"{when:%Y-%m-%d %H:%M}" if when else str(self.scan_time)
        return f"Vessel {self.vessel.label} - {stamp}"

    def summary(self):
        parts = [self.label, _plural(self.well_count, "well")]
        if self.channel_summary:
            parts.append(self.channel_summary)
        if self.elapsed:
            parts.append(f"+{self.elapsed}")
        return " - ".join(parts)

    # -- looking at it ----------------------------------------------------

    def preview(self, wells=None, **kwargs):
        """Fetch thumbnails of these wells. See ``IncucyteClient.preview``."""
        if self.client is None:
            raise ValueError(
                "This scan is not attached to a client - call "
                "client.preview(scan, ...) instead.")
        return self.client.preview(self, wells=wells, **kwargs)

    def to_dict(self):
        when = self.when
        return {
            "vessel_id": self.vessel_id,
            "vessel_name": self.vessel.name,
            "owner": self.vessel.owner,
            "plate": self.vessel.plate_format,
            "scan_time": self.scan_time,
            "when": when.isoformat() if when else None,
            "elapsed": self.elapsed,
            "well_count": self.well_count,
            "wells": self.well_names,
            "channels": sorted(self.channels),
            "channel_names": [
                self.vessel.channel_labels.get(c, ch.image_type_label(c))
                for c in sorted(self.channels, key=ch.image_type_sort_key)],
            "sites": sorted(self.sites),
            "image_count": self.image_count,
            "pyramid_levels": len(self.pyramid_levels),
            "unmixing": str(self.unmixing or ""),
        }

    def __repr__(self):
        return f"<VesselScan {self.summary()}>"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

@dataclass
class OutputFile:
    """One file written to disk, described in pipeline-ready terms.

    The field names are the SCN pipeline's shared handoff contract, defined in
    ``PySCNSlice/docs/scn-pipeline-plan.md`` and written the same way by
    PyLV200, so one reader serves all three.  ``to_dict()`` is that contract on
    the wire; ``well``/``row``/``col``/``site`` and the rest sit beside it as
    this package's own plate vocabulary.
    """

    path: Path
    vessel_id: int
    well: str
    row: int
    col: int
    site: int = 0
    layout: str = "separate"
    axes: str = "YX"
    #: One entry per plane on the channel axis, in stack order:
    #: ``{index, name, image_type, source}``.  ``index`` counts from **one**,
    #: the way ImageJ and PySCNSlice's ``scn_channel`` count, and describes
    #: *the written stack* rather than the vessel - a well that missed a
    #: channel would otherwise shift every name after the gap by one.  Use
    #: :attr:`channel_names` for just the names.
    channels: list = field(default_factory=list)
    image_types: list = field(default_factory=list)  # device channel numbers
    scan_times: list = field(default_factory=list)   # ISO strings, stack order
    elapsed: str = ""                                # e.g. "01d06h30m"
    bytes: int = 0
    vessel_name: str = ""
    processed: bool = False      # were these pixels altered after download?
    processing: str = ""         # ...and how, in one line

    # -- what the next stage of the pipeline reads -------------------------
    #: Will this file gain more frames?  None where nobody can honestly say.
    complete: bool = None
    #: How many frames it should hold once the run finishes, or None.
    frames_expected: int = None
    #: Planes allocated but not yet acquired.  Always 0 here: nothing is
    #: written until every plane has been downloaded and checked.
    blank_planes: int = 0
    #: ``{"value": seconds, "source": ...}`` - the acquisition cadence.
    interval_s: dict = None
    #: ``{"value": microns, "source": ...}`` - the pixel size.
    pixel_size_um: dict = None

    @property
    def channel_names(self):
        """Just the display names, in stack order - ``["Phase", "GFP"]``."""
        return [c.get("name") for c in self.channels]

    @property
    def frames(self):
        """Timepoints this file holds *now*.

        A time stack is extended in place on every poll, so this is true when
        it is read and stale a moment later.  :attr:`complete` is what says
        whether it will change again.
        """
        return max(1, len(self.scan_times))

    #: What :attr:`frames` was called before the pipeline's shared contract
    #: settled on one name for it.  Kept so existing scripts keep working.
    @property
    def frame_count(self):
        return self.frames

    @property
    def missing_frames(self):
        """Frames the run has that this file does not, or None if unknown.

        A time stack should hold every moment its vessel was scanned, so a
        stack holding fewer has wells or channels the device missed at those
        moments.  That is a different thing from a stack still filling, which
        is what :attr:`complete` says.
        """
        if self.frames_expected is None:
            return None
        return max(0, self.frames_expected - self.frames)

    def to_dict(self):
        data = asdict(self)
        data["path"] = str(self.path)
        # Two contract names that cannot be dataclass fields here: ``frames``
        # is derived from the scan times, and ``field`` would shadow
        # ``dataclasses.field`` inside the class body.
        data["frames"] = self.frames
        data["field"] = {"kind": "well", "name": self.well}
        return data


@dataclass
class ProgressEvent:
    """A single progress tick handed to a ``progress=`` callback."""

    stage: str                 # scanning | planning | downloading | writing | done
    detail: str = ""
    done: int = 0
    total: int = 0
    unit: str = "items"
    vessel_id: int = None

    @property
    def fraction(self):
        return (self.done / self.total) if self.total else 0.0

    @property
    def percent(self):
        return int(round(100 * self.fraction))

    def __str__(self):
        if self.total:
            return f"{self.stage}: {self.done}/{self.total} {self.unit} - {self.detail}"
        return f"{self.stage}: {self.detail}"


def _scan_sort_key(scan_time):
    """Sort scan times chronologically, tolerating anything unparseable."""
    try:
        return (0, parse_scan_datetime(str(scan_time)))
    except (ValueError, TypeError):
        return (1, datetime.max)


@dataclass
class ExportPlan:
    """Everything a download *would* do, computed before any image is fetched.

    A plan is inspectable (``summary()``, ``preview()``) and re-usable - this is
    what makes a dry run possible.
    """

    output_dir: Path
    layout: str
    items: list = field(default_factory=list, repr=False)   # raw engine work items
    vessels: list = field(default_factory=list)             # Vessel objects
    scan_times: list = field(default_factory=list)
    wells_by_vessel: dict = field(default_factory=dict)
    channels: set = None
    channel_labels: dict = field(default_factory=dict)
    options: object = None
    reference_times: dict = field(default_factory=dict)
    window: tuple = None          # inclusive (start, end) datetimes

    # -- sizes ------------------------------------------------------------

    @property
    def output_file_count(self):
        return len(self.items)

    @property
    def source_image_count(self):
        total = 0
        for item in self.items:
            total += len(self._item_image_types(item))
        return total

    @property
    def estimated_bytes(self):
        total = 0
        for item in self.items:
            for image_type in self._item_image_types(item):
                total += DEFAULT_IMAGE_BYTES.get(image_type, 2_000_000)
        return total

    @staticmethod
    def _item_image_types(item):
        if item.get("frames"):
            if item.get("channel_hyperstack"):
                return [c["img_type"] for f in item["frames"] for c in f["channels"]]
            return [f["img_type"] for f in item["frames"]]
        if item.get("channels"):
            return [c["img_type"] for c in item["channels"]]
        return [item.get("img_type", 1)]

    @property
    def new_scan_times(self):
        """The scan times this plan would add that the folder does not have.

        Not the same as the number of files: a frame is one moment on the time
        axis, covering every selected well and channel at once.  For the
        per-scan layouts every planned item is new, so this is simply the
        moments they cover.  A time stack is rewritten whole whenever one frame
        arrives, so its item lists *every* frame in the file - only the ones the
        resume ledger has not already recorded count as new.
        """
        recorded = {}
        state = getattr(self, "_state", None)
        if state is not None:
            recorded = getattr(state, "entries", None) or {}
        moments = set()
        for item in self.items:
            frame_times = item.get("scan_times")
            if frame_times:
                known = recorded.get(item.get("state_key")) or {}
                seen = set(known.get("scan_times") or [])
                moments.update(t for t in frame_times if t not in seen)
            elif item.get("scan_time"):
                moments.add(item["scan_time"])
        return sorted(moments, key=_scan_sort_key)

    @property
    def new_frame_count(self):
        """How many new moments this plan would add. See :attr:`new_scan_times`."""
        return len(self.new_scan_times)

    @property
    def is_empty(self):
        return not self.items

    @property
    def axes(self):
        return LAYOUT_AXES[self.layout]

    @property
    def well_count(self):
        total = 0
        for vessel_id, selection in self.wells_by_vessel.items():
            if selection is None:
                vessel = self.vessel(vessel_id)
                total += vessel.well_count if vessel else 0
            else:
                total += len(selection)
        return total

    def vessel(self, vessel_id):
        for vessel in self.vessels:
            if vessel.id == vessel_id:
                return vessel
        return None

    # -- description ------------------------------------------------------

    def preview(self, limit=5):
        """Return the first few filenames this plan would write."""
        return [item["fname"] for item in self.items[:limit]]

    @property
    def window_description(self):
        """e.g. ``"2026-03-01 14:30 to 2026-03-04 14:30"``."""
        if not self.window:
            return ""
        start, end = self.window
        return f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"

    def summary(self):
        """Return a short, human-readable description of the plan."""
        scope = (f"{_plural(len(self.vessels), 'vessel')} - "
                 f"{_plural(self.well_count, 'well')} - "
                 f"{_plural(len(self.scan_times), 'scan time')} - "
                 f"{LAYOUT_DESCRIPTIONS[self.layout]}")
        if self.window:
            scope += f"\n{self.window_description}"
        recipe = getattr(self.options, "recipe", None)
        if recipe is not None and recipe.is_active:
            scope += f"\nPixels: {recipe.describe()}"
        if self.is_empty:
            return scope + "\nNothing to download - every selected image is already on disk."
        work = " - ".join([
            _plural(self.output_file_count, "output file"),
            _plural(self.source_image_count, "source image"),
            f"~{human_bytes(self.estimated_bytes)}",
        ])
        return f"{scope}\n{work}"

    def to_dict(self):
        return {
            "output_dir": str(self.output_dir),
            "layout": self.layout,
            "axes": self.axes,
            "output_file_count": self.output_file_count,
            "source_image_count": self.source_image_count,
            "estimated_bytes": self.estimated_bytes,
            "vessels": [v.to_dict() for v in self.vessels],
            "window": [w.isoformat() for w in self.window] if self.window else None,
            "scan_time_count": len(self.scan_times),
            "first_scan_time": self.scan_times[0] if self.scan_times else None,
            "last_scan_time": self.scan_times[-1] if self.scan_times else None,
            "channels": sorted(self.channels) if self.channels else None,
            "channel_labels": {str(k): v for k, v in self.channel_labels.items()},
            "preview": self.preview(10),
        }


@dataclass
class DownloadResult:
    """What a download actually produced."""

    plan: ExportPlan = None
    files: list = field(default_factory=list)   # OutputFile
    errors: list = field(default_factory=list)  # str
    cancelled: bool = False
    started_at: datetime = None
    finished_at: datetime = None
    manifest_path: Path = None
    cache: object = field(default=None, repr=False)

    @property
    def file_count(self):
        return len(self.files)

    @property
    def bytes_total(self):
        return sum(f.bytes for f in self.files)

    @property
    def duration_seconds(self):
        if not (self.started_at and self.finished_at):
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def ok(self):
        return not self.errors and not self.cancelled

    @property
    def paths(self):
        """Just the written paths - the usual handoff to the next pipeline step."""
        return [f.path for f in self.files]

    def by_well(self):
        """Group written files by well name."""
        grouped = {}
        for item in self.files:
            grouped.setdefault(item.well, []).append(item)
        return grouped

    def summary(self):
        if self.cancelled:
            head = f"Cancelled after {self.file_count:,} files"
        elif self.errors:
            head = f"{self.file_count:,} files written, {len(self.errors)} failed"
        else:
            head = f"{self.file_count:,} files written"
        return (f"{head} - {human_bytes(self.bytes_total)} - "
                f"{self.duration_seconds:.0f}s")

    def to_dict(self):
        return {
            "file_count": self.file_count,
            "bytes_total": self.bytes_total,
            "cancelled": self.cancelled,
            "errors": list(self.errors),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 3),
            "files": [f.to_dict() for f in self.files],
        }

    def __repr__(self):
        return f"<DownloadResult {self.summary()}>"


__all__ = [
    "LAYOUTS", "LAYOUT_DESCRIPTIONS", "LAYOUT_LABELS", "LAYOUT_AXES",
    "LAYOUT_ALIASES", "resolve_layout", "layout_flags", "layout_from_flags",
    "human_bytes", "derived", "microns_per_pixel",
    "Vessel", "VesselScan", "OutputFile", "ExportPlan",
    "DownloadResult", "ProgressEvent",
]
