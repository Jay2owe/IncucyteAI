"""The export recipe shared by the GUI, the CLI and the Python API.

One :class:`ExportOptions` object fully describes a download.  It round trips
through JSON, so the same file is a GUI preset, a ``--preset`` for the command
line, and the config an automated pipeline commits next to its analysis code.
"""

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import channels as ch
from . import wells as wl
from .models import layout_flags, layout_from_flags, resolve_layout

#: Bumped only when the on-disk preset shape changes incompatibly.
PRESET_VERSION = 1

START_FIRST = "first"
START_TODAY = "today"
END_NOW = "now"

#: Accepted written forms for a point in time.
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
)

#: ``-48h`` (before now) for a start, ``+72h`` (after the start) for an end.
_DURATION = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours",
                   "d": "days", "w": "weeks"}

#: ``-50f`` (the last 50 scans) or ``+100 frames`` (the first 100 from the
#: start).  A frame is one scan time - one point on a stack's T axis - not one
#: image, so a frame covers every selected well and channel at that moment.
_FRAMES = re.compile(r"^([+-])\s*(\d+)\s*(f|frames?|scans?)$", re.IGNORECASE)

#: How far back to look when counting frames backwards and the experiment's
#: own start time is unknown.
FRAME_LOOKBACK_DAYS = 120

MOMENT_HELP = ("YYYY-MM-DD, YYYY-MM-DD HH:MM, a relative offset like "
               "-48h / +3d, or a frame count like -50f / +100f")


def parse_duration(value):
    """Return a signed timedelta for ``-48h``/``+3d``, or None if not one."""
    match = _DURATION.match(str(value).strip())
    if not match:
        return None
    sign, amount, unit = match.groups()
    delta = timedelta(**{_DURATION_UNITS[unit.lower()]: float(amount)})
    return -delta if sign == "-" else delta


#: An unsigned length of time, as ``batch_after`` takes: ``7d``, ``48h``, ``90m``.
#: Unlike an offset there is no sign here - this is "how long", not "how long
#: before or after".
_SPAN = re.compile(r"^\+?\s*(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)

SPAN_HELP = "a length of time such as 90m, 12h, 7d or 2w"

#: Largest-first, so a span is written in the biggest unit that divides it.
#: Weeks are deliberately absent: nobody who typed ``7d`` wants to read ``1w``.
_SPAN_UNITS = (("d", "day", 86400), ("h", "hour", 3600), ("m", "minute", 60))

#: unit letter -> the word for it, for spans said out loud.
_SPAN_WORDS = {"s": "second", "m": "minute", "h": "hour", "d": "day",
               "w": "week"}


def parse_span(value):
    """Return a positive timedelta for ``7d``/``48h``, or None if not one."""
    if isinstance(value, timedelta):
        return value if value > timedelta(0) else None
    match = _SPAN.match(str(value).strip())
    if not match:
        return None
    amount, unit = match.groups()
    delta = timedelta(**{_DURATION_UNITS[unit.lower()]: float(amount)})
    return delta if delta > timedelta(0) else None


def format_span(delta):
    """Write a timedelta back in the biggest whole unit: ``7d``, ``90m``."""
    seconds = int(round(delta.total_seconds()))
    for short, _word, size in _SPAN_UNITS:
        if seconds and seconds % size == 0:
            return f"{seconds // size}{short}"
    return f"{seconds}s"


def describe_span(value):
    """Say a length of time in words, in the unit it was written in.

    ``"7d"`` reads back as "7 days", not "1 week" - the point is to echo the
    setting the user typed, not to find the tidiest way to say it.
    """
    if not isinstance(value, timedelta):
        match = _SPAN.match(str(value).strip())
        if match:
            amount, unit = match.groups()
            number = float(amount)
            count = int(number) if number == int(number) else number
            word = _SPAN_WORDS[unit.lower()]
            return f"{count} {word}{'' if count == 1 else 's'}"
        value = parse_span(value)
        if value is None:
            return str(value)
    seconds = int(round(value.total_seconds()))
    for _short, word, size in _SPAN_UNITS:
        if seconds and seconds % size == 0:
            count = seconds // size
            return f"{count} {word}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def parse_frame_count(value):
    """Return a signed frame count for ``-50f`` / ``+100 frames``, or None.

    Negative counts back from the end of the window, positive counts forward
    from its start - the same sign convention as the time offsets.
    """
    match = _FRAMES.match(str(value).strip())
    if not match:
        return None
    sign, amount, _unit = match.groups()
    count = int(amount)
    if count < 1:
        raise ValueError(f"A frame count must be at least 1, got {value!r}")
    return -count if sign == "-" else count


def parse_moment(value, field_name="time", end_of_day=False):
    """Turn any accepted written form into a datetime.

    A bare date means the start of that day, or the *end* of it when
    ``end_of_day`` is set — so ``end_at="2026-03-05"`` includes all of the 5th
    rather than stopping at midnight.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min)

    text = str(value).strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        day = datetime.strptime(text, DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be {MOMENT_HELP}; got {value!r}") from exc
    return datetime.combine(day, time.max if end_of_day else time.min)


def _as_text(value):
    """Store datetimes and dates as strings so options stay JSON-friendly."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _as_span_text(value):
    """Normalise a batch delay to its written form, or "" when there is none."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, timedelta):
        return format_span(value)
    text = str(value).strip()
    if not text:
        return ""
    if parse_span(text) is None:
        raise ValueError(f"batch_after must be {SPAN_HELP}; got {value!r}")
    return text


@dataclass
class ExportOptions:
    """A complete, serialisable description of one download."""

    output: str = ""
    vessels: list = field(default_factory=list)
    wells: str = "all"
    channels: str = "all"
    layout: str = "separate"
    start_from: str = START_TODAY
    end_at: str = None
    scan_filter: str = None
    green_lut: bool = False
    # Preprocessing, all off by default: the device sends raw pixels and that
    # is what a measurement pipeline should get unless it asks otherwise.
    calibrate: bool = False        # raw counts -> calibrated units, 32-bit float
    background: str = ""           # "", "device", or a number of raw counts
    unmix: str = ""                # "", "device", or "green<8%red"
    workers: int = 4
    interval_minutes: int = 10
    # Chunked watching. Watch mode normally downloads a frame the moment it
    # appears; these hold it back until the chunk is worth fetching - so an
    # experiment can be left to run and collected a week at a time.
    batch_frames: int = 0          # ...until this many new frames are waiting
    batch_after: str = ""          # ...or the oldest has waited this long: "7d"
    host: str = None
    wells_by_vessel: dict = field(default_factory=dict)
    state_scope: str = "auto"
    cache_payloads: str = "auto"
    write_manifest: bool = True
    name: str = ""
    notes: str = ""

    # -- normalisation ----------------------------------------------------

    def __post_init__(self):
        self.layout = resolve_layout(self.layout)
        self.vessels = [int(v) for v in (self.vessels or [])]
        self.workers = max(1, min(32, int(self.workers or 1)))
        self.interval_minutes = max(1, int(self.interval_minutes or 1))
        self.batch_frames = max(0, int(self.batch_frames or 0))
        self.batch_after = _as_span_text(self.batch_after)
        self.wells_by_vessel = {
            str(k): v for k, v in (self.wells_by_vessel or {}).items()}
        if self.layout != "separate":
            # Stacked outputs are written straight from the raw payloads; a
            # display LUT would corrupt the pixel values downstream analysis
            # depends on.
            self.green_lut = False
        # Validate the specs now so a bad preset fails at load, not mid-download.
        ch.parse_channels(self.channels)
        wl.parse_wells(self.wells)
        for spec in self.wells_by_vessel.values():
            wl.parse_wells(spec)
        # unmix may arrive as an Unmixing object or a list of terms; the
        # canonical form is the spec string, so a preset stays plain JSON.
        from .processing import normalise_unmix

        self.unmix = normalise_unmix(self.unmix)
        self.background = "" if self.background is None else str(self.background)
        self.start_from = _as_text(self.start_from)
        self.end_at = _as_text(self.end_at)

        start_frames = parse_frame_count(self.start_from)
        if start_frames is not None and start_frames > 0:
            raise ValueError(
                f"start_from={self.start_from!r} counts frames forwards, which "
                f"has nothing to count from. Use a negative count such as "
                f"'-{start_frames}f' for the last {start_frames} frames, or put "
                f"'+{start_frames}f' in end_at for the first {start_frames}.")
        end_frames = parse_frame_count(self.end_at) if self.end_at else None
        if end_frames is not None and end_frames < 0:
            raise ValueError(
                f"end_at={self.end_at!r} counts frames backwards. Use "
                f"'+{abs(end_frames)}f' for the first {abs(end_frames)} frames "
                f"after the start, or put it in start_from for the last ones.")
        if start_frames is not None and end_frames is not None:
            raise ValueError(
                "Count frames from one end or the other, not both: "
                f"start_from={self.start_from!r} and end_at={self.end_at!r}.")

        if (self.start_from not in (START_FIRST, START_TODAY, END_NOW)
                and start_frames is None
                and parse_duration(self.start_from) is None):
            parse_moment(self.start_from, "start_from")
        if (self.end_at and self.end_at != END_NOW and end_frames is None
                and parse_duration(self.end_at) is None):
            parse_moment(self.end_at, "end_at", end_of_day=True)

    # -- derived views ----------------------------------------------------

    @property
    def hyperstack(self):
        """Old-style flag: does this layout write an ImageJ channel axis?"""
        return layout_flags(self.layout)[0]

    @property
    def time_stack(self):
        """Old-style flag: does this layout write an ImageJ time axis?"""
        return layout_flags(self.layout)[1]

    @property
    def channel_set(self):
        """Channel numbers to fetch, or ``None`` for every channel."""
        return ch.parse_channels(self.channels)

    def wells_for(self, vessel_id):
        """Well selection for one vessel, or ``None`` for the whole plate."""
        spec = self.wells_by_vessel.get(str(vessel_id), self.wells)
        return wl.parse_wells(spec)

    def window_description(self, first_scan=None, now=None):
        """Say what the window means, in words a frame count can also satisfy."""
        if self.start_frames is not None:
            end = self.resolve_end(now=now)
            return f"last {self.start_frames} frames up to {end:%d %b %Y %H:%M}"
        start = self.resolve_start(first_scan=first_scan, now=now)
        if self.end_frames is not None:
            return f"first {self.end_frames} frames from {start:%d %b %Y %H:%M}"
        end = self.resolve_end(start=start, now=now)
        return f"{start:%d %b %Y %H:%M} to {end:%d %b %Y %H:%M}"

    @property
    def start_frames(self):
        """How many frames to keep from the *end*, or None."""
        count = parse_frame_count(self.start_from)
        return abs(count) if count else None

    @property
    def end_frames(self):
        """How many frames to keep from the *start*, or None."""
        count = parse_frame_count(self.end_at) if self.end_at else None
        return count if count else None

    @property
    def counts_frames(self):
        return self.start_frames is not None or self.end_frames is not None

    def resolve_start(self, first_scan=None, now=None):
        """Turn ``start_from`` into a datetime (inclusive lower bound)."""
        now = now or datetime.now()
        if self.start_from == START_TODAY:
            return datetime.combine(now.date(), time.min)
        if self.start_from == END_NOW:
            return now
        if self.start_from == START_FIRST:
            if isinstance(first_scan, datetime):
                return first_scan
            if isinstance(first_scan, date):
                return datetime.combine(first_scan, time.min)
            return datetime.combine(now.date(), time.min)
        offset = parse_duration(self.start_from)
        if offset is not None:
            return now + offset          # "-48h" means two days before now
        if self.start_frames is not None:
            # A frame count has no timestamp until the scans are known; this is
            # only the floor for the search, and the slice happens afterwards.
            if isinstance(first_scan, datetime):
                return first_scan
            if isinstance(first_scan, date):
                return datetime.combine(first_scan, time.min)
            return now - timedelta(days=FRAME_LOOKBACK_DAYS)
        return parse_moment(self.start_from, "start_from")

    def resolve_end(self, start=None, now=None):
        """Turn ``end_at`` into a datetime (inclusive upper bound)."""
        now = now or datetime.now()
        if not self.end_at or self.end_at == END_NOW:
            return now
        if self.end_frames is not None:
            return now                   # bounded by the slice, not the clock
        offset = parse_duration(self.end_at)
        if offset is not None:
            # "+72h" is measured from the start, which is what "the first three
            # days of the experiment" actually means.
            anchor = start if start is not None else self.resolve_start(now=now)
            return anchor + offset
        return parse_moment(self.end_at, "end_at", end_of_day=True)

    def resolve_window(self, first_scan=None, now=None):
        """Return the inclusive (start, end) datetimes for this recipe."""
        now = now or datetime.now()
        start = self.resolve_start(first_scan=first_scan, now=now)
        end = self.resolve_end(start=start, now=now)
        if end < start:
            raise ValueError(
                "The export window ends before it starts: "
                f"{start} to {end}. Check start_from={self.start_from!r} "
                f"and end_at={self.end_at!r}.")
        return start, end

    # Kept for callers that only need whole days: the device lists scans one
    # calendar day at a time, so the sweep itself still works in dates.
    def resolve_start_date(self, first_scan=None, today=None):
        now = datetime.combine(today, time.min) if today else None
        return self.resolve_start(first_scan=first_scan, now=now).date()

    def resolve_end_date(self, today=None):
        now = datetime.combine(today, time.max) if today else None
        return self.resolve_end(now=now).date()

    def covers(self, scan_time, start=None, end=None):
        """Is this scan time inside the window? Accepts a string or datetime."""
        from .engine import parse_scan_datetime

        if start is None or end is None:
            start, end = self.resolve_window()
        moment = (scan_time if isinstance(scan_time, datetime)
                  else parse_scan_datetime(str(scan_time)))
        return start <= moment <= end

    def apply_frame_limits(self, scan_times):
        """Cut a chronological scan list down to the requested frame count."""
        from .engine import parse_scan_datetime

        if not self.counts_frames or not scan_times:
            return list(scan_times)

        def key(scan_time):
            try:
                return parse_scan_datetime(str(scan_time))
            except (ValueError, TypeError):
                return datetime.max

        ordered = sorted(scan_times, key=key)
        if self.start_frames is not None:
            return ordered[-self.start_frames:]
        return ordered[:self.end_frames]

    def filter_scan_times(self, scan_times, start=None, end=None):
        """Trim a scan list to the window, ``scan_filter`` and frame count."""
        from .engine import parse_scan_datetime

        if start is None or end is None:
            start, end = self.resolve_window()
        kept = []
        for scan_time in scan_times:
            if self.scan_filter and self.scan_filter not in str(scan_time):
                continue
            try:
                moment = parse_scan_datetime(str(scan_time))
            except (ValueError, TypeError):
                kept.append(scan_time)     # unparseable: let the device decide
                continue
            if start <= moment <= end:
                kept.append(scan_time)
        return self.apply_frame_limits(kept)

    @property
    def output_path(self):
        return Path(self.output).expanduser() if self.output else None

    # -- editing ----------------------------------------------------------

    def replace(self, **changes):
        """Return a copy with fields changed (options are treated as values)."""
        return replace(self, **changes)

    @property
    def recipe(self):
        """The preprocessing these options ask for."""
        from .processing import Recipe

        return Recipe.from_options(self)

    @property
    def processing_description(self):
        return self.recipe.describe()

    # -- chunked watching -------------------------------------------------

    @property
    def batch_delay(self):
        """``batch_after`` as a timedelta, or None when it is not set."""
        return parse_span(self.batch_after) if self.batch_after else None

    @property
    def batches(self):
        """True when watch mode should hold new frames back into chunks."""
        return bool(self.batch_frames or self.batch_after)

    @property
    def batch_description(self):
        """The condition a held chunk is waiting for, or "" when there is none.

        e.g. ``"50 frames have accumulated or 7 days have passed, whichever
        comes first"`` - written to follow "hold new frames until ...".
        """
        parts = []
        if self.batch_frames:
            plural = "" if self.batch_frames == 1 else "s"
            parts.append(f"{self.batch_frames} frame{plural} "
                         f"{'has' if not plural else 'have'} accumulated")
        if self.batch_after:
            said = describe_span(self.batch_after)
            parts.append(f"{said} {'has' if said.startswith('1 ') else 'have'}"
                         f" passed")
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return f"{parts[0]} or {parts[1]}, whichever comes first"

    def validate(self):
        """Return a list of human-readable problems; empty means good to go."""
        problems = []
        problems += self.recipe.validate()
        if not self.output:
            problems.append("No output folder chosen.")
        if not self.vessels:
            problems.append("No vessel selected.")
        if self.channel_set is not None and not self.channel_set:
            problems.append("No channels selected.")
        for vessel_id in self.vessels:
            selection = self.wells_for(vessel_id)
            if selection is not None and not selection:
                problems.append(f"Vessel {vessel_id} has no wells selected.")
        return problems

    # -- serialisation ----------------------------------------------------

    def to_dict(self):
        return {
            "preset_version": PRESET_VERSION,
            "name": self.name,
            "notes": self.notes,
            "host": self.host,
            "output": self.output,
            "vessels": list(self.vessels),
            "wells": self.wells,
            "wells_by_vessel": dict(self.wells_by_vessel),
            "channels": self.channels,
            "layout": self.layout,
            "start_from": self.start_from,
            "end_at": self.end_at,
            "scan_filter": self.scan_filter,
            "green_lut": self.green_lut,
            "calibrate": self.calibrate,
            "background": self.background,
            "unmix": self.unmix,
            "processing": self.recipe.to_dict(),
            "workers": self.workers,
            "interval_minutes": self.interval_minutes,
            "batch_frames": self.batch_frames,
            "batch_after": self.batch_after,
            "state_scope": self.state_scope,
            "cache_payloads": self.cache_payloads,
            "write_manifest": self.write_manifest,
        }

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        data.pop("preset_version", None)
        # Accept the old boolean pair as well as a layout name.
        if "layout" not in data and ("hyperstack" in data or "time_stack" in data):
            data["layout"] = layout_from_flags(data.pop("hyperstack", False),
                                               data.pop("time_stack", False))
        data.pop("hyperstack", None)
        data.pop("time_stack", None)
        if "end_date" in data and "end_at" not in data:
            data["end_at"] = data.pop("end_date")
        data.pop("end_date", None)
        if "green_phase" in data and "green_lut" not in data:
            data["green_lut"] = data.pop("green_phase")
        data.pop("green_phase", None)
        if "max_workers" in data and "workers" not in data:
            data["workers"] = data.pop("max_workers")
        data.pop("max_workers", None)
        # `processing` is a description written for humans reading a manifest;
        # the three fields it describes are what actually round trips.
        data.pop("processing", None)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path):
        """Write this recipe as a JSON preset."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path):
        """Read a JSON preset written by :meth:`save`."""
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- command line equivalent -----------------------------------------

    def cli_args(self, command="download"):
        """Return the ``pyincucyte`` argv that reproduces this recipe.

        Used by the GUI's "Copy CLI command", which is how a settings screen
        becomes a line in a pipeline script.
        """
        args = ["pyincucyte"]
        if self.host:
            args += ["--host", self.host]
        args.append(command)
        for vessel_id in self.vessels:
            args += ["-v", str(vessel_id)]
        args += ["-o", _quote(self.output or "OUTPUT_FOLDER")]
        if self.wells and self.wells != "all":
            args += ["-w", _quote(self.wells)]
        for vessel_id, spec in sorted(self.wells_by_vessel.items()):
            if spec and spec != self.wells:
                args += ["--vessel-wells", f"{vessel_id}:{spec}"]
        if self.channels and self.channels != "all":
            args += ["-c", self.channels]
        if self.layout != "separate":
            args += ["--layout", self.layout]
        if self.start_from != START_TODAY:
            args += ["--start-from", _quote(self.start_from)]
        if self.end_at and self.end_at != END_NOW:
            args += ["--end-at", _quote(self.end_at)]
        if self.scan_filter:
            args += ["--scan-time", _quote(self.scan_filter)]
        if self.green_lut:
            args.append("--green-lut")
        if self.calibrate:
            args.append("--calibrate")
        if self.unmix:
            args += ["--unmix", _quote(self.unmix)]
        if self.background:
            args += ["--background", _quote(self.background)]
        if self.workers != 4:
            args += ["--workers", str(self.workers)]
        if command == "watch":
            args += ["-i", str(self.interval_minutes)]
            if self.batch_frames:
                args += ["--batch-frames", str(self.batch_frames)]
            if self.batch_after:
                args += ["--batch-after", self.batch_after]
        if not self.write_manifest:
            args.append("--no-manifest")
        if self.cache_payloads != "auto":
            args += ["--cache", self.cache_payloads]
        return args

    def cli_command(self, command="download"):
        """Return the CLI equivalent as one copy-pasteable string."""
        return " ".join(self.cli_args(command))

    def describe(self, vessel_labels=None, channel_labels=None):
        """Return a few short lines describing this recipe for a UI panel."""
        vessel_labels = vessel_labels or {}
        names = [vessel_labels.get(v, str(v)) for v in self.vessels] or ["none"]
        return [
            f"Vessels: {', '.join(names)}",
            f"Wells: {self.wells}",
            f"Channels: {ch.format_channels(self.channel_set, channel_labels)}",
            f"Layout: {self.layout}",
            f"From: {self.start_from} to {self.end_at or END_NOW}",
        ]


def _quote(value):
    value = str(value)
    return f'"{value}"' if " " in value else value


__all__ = ["ExportOptions", "PRESET_VERSION", "START_FIRST", "START_TODAY",
           "END_NOW", "parse_moment", "parse_duration", "parse_frame_count",
           "parse_span", "format_span", "describe_span",
           "MOMENT_HELP", "SPAN_HELP", "FRAME_LOOKBACK_DAYS"]
