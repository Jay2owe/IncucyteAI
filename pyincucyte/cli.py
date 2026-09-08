"""Command line front end.

Every flag the original ``incucyte_downloader.py`` accepted still works.  What
is new is aimed at scripting: ``--preset`` to load a saved recipe, ``--json``
so another program can read the output, and ``plan`` to see exactly what a
download would do before it runs.
"""

import argparse
import contextlib
import getpass
import io
import json
import logging
import re
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from . import engine
from .channels import CHANNEL_HELP
from .client import IncucyteClient
from .config import ConfigStore
from .errors import IncucyteError, NotLoggedInError
from .manifest import MANIFEST_FILENAME, load_manifest
from .models import (
    LAYOUT_DESCRIPTIONS, LAYOUTS, human_bytes, json_clean, layout_from_flags,
    resolve_layout,
)
from .preview import DEFAULT_MAX_IMAGES, DEFAULT_SIZE
from . import schedule
from .timeline import DEFAULT_TIMELINE_SIZE
from .processing import BACKGROUND_HELP, UNMIX_HELP, Unmixing
from .options import (
    END_NOW, ExportOptions, MOMENT_HELP, SPAN_HELP, START_FIRST, START_TODAY,
    parse_duration, parse_moment,
)

log = logging.getLogger("pyincucyte.cli")

#: What find/preview accept for --at, --since and --until. Narrower than
#: MOMENT_HELP: a frame count has no meaning without a scan list.
WHEN_HELP = ("YYYY-MM-DD, YYYY-MM-DD HH:MM, or a relative offset "
             "like -48h / +3d")

#: Exit code for a poll that wrote nothing.  Distinct from 0 ("a chunk landed")
#: and 2 ("it failed"), because a scheduled task treats all three differently
#: and only 0 should start whatever runs next.  The same three numbers pylv200
#: uses, so somebody who has scheduled one package does not have to relearn the
#: other.  Only ``watch --once`` returns it; every other command keeps this
#: package's existing convention, where 1 is argparse's and 2 is a failure.
NOTHING_WRITTEN = 1

# Mutable so tests and embedded callers can switch modes without rebinding a
# name captured by output helpers.
_JSON = [False]


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def literal(text):
    """Escape a help string for argparse, which %-formats what it is given.

    The unmix help contains a literal "8%", and argparse reads "%r" in it as a
    format spec - which turns `--help` into a traceback.
    """
    return str(text).replace("%", "%%")


def emit(text=""):
    print(text, file=sys.stderr if _JSON[0] else sys.stdout)


def emit_json(payload):
    print(json.dumps(json_clean(payload), indent=2, allow_nan=False))


def emit_error(kind, message, command=""):
    """Emit one stable machine error while preserving a human diagnostic."""
    message = str(message).strip()
    print(f"error: {message}", file=sys.stderr)
    if _JSON[0]:
        emit_json({"ok": False,
                   "error": {"type": str(kind), "message": message},
                   "command": str(command or "")})


def print_table(rows, headers):
    """Print an aligned plain-text table."""
    if not rows:
        return
    columns = list(zip(*([headers] + [[str(c) for c in row] for row in rows])))
    widths = [max(len(cell) for cell in column) for column in columns]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    emit(line)
    emit("  ".join("-" * w for w in widths))
    for row in rows:
        emit("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


class ConsoleProgress:
    """A one-line, rewriting progress display for the terminal."""

    def __init__(self, enabled=True, stream=None):
        self.enabled = enabled and (stream or sys.stderr).isatty()
        self.stream = stream or sys.stderr
        self._last_stage = None

    def __call__(self, event):
        if not self.enabled:
            return
        if event.stage == "done":
            self._clear()
            return
        if event.total:
            bar_width = 24
            filled = int(bar_width * event.fraction)
            bar = "#" * filled + "." * (bar_width - filled)
            text = (f"{event.stage:<11} [{bar}] {event.done:>6,}/{event.total:<6,} "
                    f"{event.unit}")
        else:
            text = f"{event.stage:<11} {event.detail}"
        self.stream.write("\r" + text[:110].ljust(110))
        self.stream.flush()

    def _clear(self):
        if self.enabled:
            self.stream.write("\r" + " " * 110 + "\r")
            self.stream.flush()


# ---------------------------------------------------------------------------
# option assembly
# ---------------------------------------------------------------------------

def options_from_args(args, *, require_output=True):
    """Build ExportOptions from argv, layering CLI flags over any --preset."""
    options = (ExportOptions.load(args.preset) if getattr(args, "preset", None)
               else ExportOptions())
    changes = {}

    vessels = list(options.vessels)
    for vessel_id in (getattr(args, "vessel", None) or []):
        if vessel_id not in vessels:
            vessels.append(int(vessel_id))

    wells_by_vessel = dict(options.wells_by_vessel)
    for spec in (getattr(args, "filter", None) or []):
        vessel_id, well_spec = _split_filter(spec)
        if vessel_id not in vessels:
            vessels.append(vessel_id)
        if well_spec:
            wells_by_vessel[str(vessel_id)] = well_spec
    for spec in (getattr(args, "vessel_wells", None) or []):
        vessel_id, well_spec = _split_filter(spec)
        if vessel_id not in vessels:
            vessels.append(vessel_id)
        if well_spec:
            wells_by_vessel[str(vessel_id)] = well_spec

    legacy = getattr(args, "config", None)
    if legacy:
        for entry in json.loads(Path(legacy).read_text(encoding="utf-8")).get("vessels", []):
            vessel_id = int(entry["id"])
            if vessel_id not in vessels:
                vessels.append(vessel_id)
            if entry.get("wells"):
                wells_by_vessel[str(vessel_id)] = ",".join(entry["wells"])
            if entry.get("channels"):
                changes.setdefault("channels", entry["channels"])

    if vessels:
        changes["vessels"] = vessels
    if wells_by_vessel:
        changes["wells_by_vessel"] = wells_by_vessel
    if getattr(args, "output", None):
        changes["output"] = str(args.output)
    if getattr(args, "wells", None):
        changes["wells"] = args.wells
    if getattr(args, "channels", None):
        changes["channels"] = args.channels
    if getattr(args, "workers", None):
        changes["workers"] = args.workers
    if getattr(args, "interval", None):
        changes["interval_minutes"] = args.interval
    if getattr(args, "batch_frames", None):
        changes["batch_frames"] = args.batch_frames
    if getattr(args, "batch_after", None):
        changes["batch_after"] = args.batch_after
    if getattr(args, "scan_time", None):
        changes["scan_filter"] = args.scan_time
    if getattr(args, "state_scope", None):
        changes["state_scope"] = args.state_scope
    if getattr(args, "no_manifest", False):
        changes["write_manifest"] = False
    if getattr(args, "no_append", False):
        changes["append_stacks"] = False
    if getattr(args, "cache_payloads", None):
        changes["cache_payloads"] = args.cache_payloads
    if getattr(args, "host", None):
        changes["host"] = args.host

    layout = getattr(args, "layout", None)
    if layout:
        changes["layout"] = resolve_layout(layout)
    elif getattr(args, "hyperstack", False) or getattr(args, "time_stack", False):
        changes["layout"] = layout_from_flags(args.hyperstack, args.time_stack)

    if getattr(args, "calibrate", None) is not None:
        changes["calibrate"] = bool(args.calibrate)
    if getattr(args, "unmix", None):
        changes["unmix"] = args.unmix
    if getattr(args, "background", None):
        changes["background"] = args.background

    # --date is the old single-day shorthand.
    if getattr(args, "date", None):
        changes["start_from"] = args.date
        changes["end_at"] = args.date
    if getattr(args, "start_from", None):
        changes["start_from"] = _normalise_start(args.start_from)
    end_at = getattr(args, "end_at", None) or getattr(args, "end_date", None)
    if end_at:
        changes["end_at"] = _normalise_end(end_at)

    options = options.replace(**changes)

    if require_output and not options.output:
        raise SystemExit("error: an output folder is required (-o/--output)")
    if not options.vessels:
        raise SystemExit(
            "error: specify at least one vessel with -v, --filter or --preset")
    return options


def _split_filter(spec):
    """Parse ``38:A1,B3`` or plain ``38`` into (vessel_id, well spec or None)."""
    text = str(spec)
    if ":" in text:
        vessel_id, well_spec = text.split(":", 1)
        return int(vessel_id), well_spec.strip() or None
    return int(text), None


def _normalise_start(value):
    text = str(value).strip().lower()
    if text in ("first", "first scan", "firstscan"):
        return START_FIRST
    if text == "today":
        return START_TODAY
    if text == "now":
        return END_NOW
    return str(value).strip()


def _normalise_end(value):
    text = str(value).strip().lower()
    if text in ("now", "today", "last"):
        return END_NOW
    return str(value).strip()


def make_client(args):
    store = ConfigStore(getattr(args, "config_file", None))
    host = getattr(args, "host", None)
    try:
        return IncucyteClient.from_saved(host, store=store)
    except NotLoggedInError:
        if getattr(args, "command", "") in ("probe", "login"):
            return IncucyteClient(host or engine.DEFAULT_HOST, store=store)
        raise


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_probe(args):
    client = make_client(args)
    report = client.probe()
    if args.json:
        emit_json(report)
        return 0
    emit(f"\n=== Probing Incucyte at {report['host']} ===\n")
    names = {80: "HTTP", 443: "HTTPS", 808: "WCF net.tcp"}
    for port, open_ in report["ports"].items():
        emit(f"  Port {port} ({names.get(port, '?')}): "
             f"{'OPEN' if open_ else 'CLOSED'}")
    if report["api"]:
        emit("\n  API endpoint: OK")
        emit(f"  Device login: {'enabled' if report['device_login'] else 'disabled'}")
        emit(f"  Windows auth: {'enabled' if report['windows_login'] else 'disabled'}")
    else:
        emit(f"\n  API check failed: {report.get('error', 'unreachable')}")
        emit("  The device is only reachable from the site network.")
    emit()
    return 0 if report["api"] else 1


def cmd_login(args):
    client = make_client(args)
    username = args.username or input("Username: ")
    password = args.password or getpass.getpass("Password: ")
    emit("Encrypting password...")
    credentials = client.login(username, password, device_name=args.name)
    hours = credentials.token_seconds_left / 3600
    emit(f"Logged in as {username} on {client.host}.")
    emit(f"Token valid for {hours:.1f} hours. Saved to {client.store.path}")
    return 0


def cmd_logout(args):
    client = make_client(args)
    client.logout()
    emit(f"Saved login removed for {client.host}.")
    return 0


def cmd_vessels(args):
    client = make_client(args)
    vessels = client.vessels(refresh=True)
    if args.json:
        emit_json([v.to_dict() for v in vessels])
        return 0
    if not vessels:
        emit("No vessels found.")
        return 0
    rows = [[
        v.id, v.name or "-", v.owner or "-", v.plate_format,
        v.last_scan.strftime("%Y-%m-%d %H:%M") if v.last_scan else "-",
        v.channel_summary or "-",
    ] for v in vessels]
    emit()
    print_table(rows, ["ID", "Name", "Owner", "Plate", "Last scan", "Channels"])
    emit(f"\n{len(vessels)} vessels")
    return 0


def cmd_scans(args):
    client = make_client(args)
    end_arg = getattr(args, "end_at", None) or getattr(args, "end_date", None)
    if args.start_from or end_arg:
        options = ExportOptions(
            output=".", vessels=[0],
            start_from=_normalise_start(args.start_from or START_TODAY),
            end_at=_normalise_end(end_arg) if end_arg else None)
        first = (client.first_scan_time()
                 if options.start_from == START_FIRST else None)
        start, end = options.resolve_window(first_scan=first)
        scans = options.filter_scan_times(
            client.scan_times_between(start.date(), end.date()), start, end)
        label = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"
    else:
        day = _resolve_day(args.date, client) or date.today()
        scans = client.scan_times(day)
        label = str(day)
    if args.json:
        emit_json({"range": label, "scan_times": scans})
        return 0
    emit(f"\n=== Scans for {label} ===\n")
    for scan in scans:
        emit(f"  {scan}")
    emit(f"\n{len(scans)} scans")
    return 0


def _resolve_day(value, client):
    """Resolve one CLI date argument to a calendar day."""
    if not value:
        return None
    text = _normalise_start(value)
    if text == START_FIRST:
        first = client.first_scan_time()
        return first.date() if first else date.today()
    if text in (START_TODAY, END_NOW):
        return date.today()
    offset = parse_duration(text)
    if offset is not None:
        return (datetime.now() + offset).date()
    return parse_moment(text, "date").date()


def _find_scans(args, client):
    """Run the scan finder from the shared --name/--at/--most-recent flags."""
    return client.find_scans(
        getattr(args, "name", None),
        vessel=getattr(args, "vessel", None),
        owner=getattr(args, "owner", None),
        plate=getattr(args, "plate", None),
        channel=getattr(args, "channel", None),
        at=getattr(args, "at", None),
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        most_recent=getattr(args, "most_recent", 1),
        max_days=getattr(args, "max_days", 14),
        limit=getattr(args, "limit", None),
        progress=ConsoleProgress(not args.quiet))


def cmd_find(args):
    """Which plate is which: search vessels and report their newest scans."""
    client = make_client(args)
    scans = _find_scans(args, client)
    if args.json:
        emit_json([scan.to_dict() for scan in scans])
        return 0
    if not scans:
        emit("No scan matched. Try a wider --most-recent, --max-days or fewer "
             "filters.")
        return 0
    rows = [[
        scan.vessel_id, scan.vessel.name or "-",
        f"{scan.when:%Y-%m-%d %H:%M}" if scan.when else scan.scan_time,
        scan.elapsed or "-", scan.well_count, scan.channel_summary or "-",
        str(scan.unmixing or "") or "-",
    ] for scan in scans]
    emit()
    print_table(rows, ["ID", "Name", "Scan", "Elapsed", "Wells", "Channels",
                       "Unmixing"])
    emit(f"\n{len(scans)} scans. Look at one with: pyincucyte preview -v "
         f"{scans[0].vessel_id}")
    return 0


def cmd_preview(args):
    """Fetch thumbnails of the wells and show them."""
    client = make_client(args)
    scans = _find_scans(args, client)
    if not scans:
        emit("No scan matched - nothing to preview.")
        return 0
    result = client.preview(
        scans, wells=args.wells, channels=args.channels, site=args.site,
        size=args.size, contrast=args.contrast, max_images=args.max_images,
        workers=args.workers or 4, calibrate=bool(args.calibrate),
        background=args.background or "", unmix=args.unmix or "",
        progress=ConsoleProgress(not args.quiet))

    saved = result.save(args.save) if args.save else []
    if args.json:
        payload = result.to_dict()
        payload["saved"] = [str(path) for path in saved]
        emit_json(payload)
        return 0

    emit(f"\n=== {result.title} ===\n")
    for image in result.images:
        emit(f"  {image.label:<28} "
             f"{'x'.join(str(n) for n in image.size) if image.ok else image.error}")
    emit(f"\n{result.summary()}")
    if saved:
        emit(f"{len(saved)} PNGs written to {args.save}")
    if result.is_empty:
        return 0
    if not args.no_show:
        result.show()
    return 0


def cmd_protocol(args):
    """Draw how the run was set up, from the device's own metadata.

    The same picture the Incucyte software shows in its experiment view - a
    time loop around a well loop around the channel chain - except that it can
    be seen from another machine, and it says which of the numbers on it were
    requested and which actually happened.
    """
    client = make_client(args)
    scans = _find_scans(args, client)
    if not scans:
        emit("No scan matched - nothing to draw.")
        return 1

    protocol = client.protocol(
        scans[0], names=_split_names(args.channel_names),
        name_source="the command line" if args.channel_names else "",
        scan=not args.no_scan, progress=ConsoleProgress(not args.quiet))

    written = None
    if args.out:
        written = protocol.save(args.out,
                                theme="dark" if args.dark else "light")

    if args.json:
        payload = protocol.to_dict()
        payload["written"] = str(written) if written else ""
        emit_json(payload)
        return 0

    for line in protocol.lines(width=max(int(args.width), 48)):
        emit(line)
    if written:
        emit(f"\n  Wrote {written}")
    return 0


def _split_names(spec):
    """``--channel-names Phase,Cry1-GFP`` into a list, or None."""
    if not spec:
        return None
    return [part.strip() for part in str(spec).split(",") if part.strip()]


def cmd_timeline(args):
    """Open a bounded lazy scrubber over a vessel's scan times."""
    client = make_client(args)
    progress = ConsoleProgress(not args.quiet)
    if args.at:
        found = _find_scans(args, client)
        if not found:
            emit("No scan matched - nothing to show.")
            return 0
        target = found[:1]
        find = {}
    else:
        target = args.name
        find = {"vessel": args.vessel, "owner": args.owner,
                "plate": args.plate, "channel": args.channel}
    result = client.timeline(
        target, wells=args.wells, channels=args.channels, site=args.site,
        size=args.size, contrast=args.contrast, anchors=args.anchors,
        frame_cache=args.frame_cache, render_cache=args.render_cache,
        proxy_dir=args.proxy_dir, local_stack=args.local_stack,
        output=args.output,
        start_from=args.since or START_FIRST, end_at=args.until or END_NOW,
        max_frames=args.most_recent, calibrate=bool(args.calibrate),
        background=args.background or "", unmix=args.unmix or "",
        progress=progress, **find)
    saved = result.save(args.save) if args.save else []
    if args.json:
        payload = result.to_dict()
        payload["saved"] = [str(path) for path in saved]
        emit_json(payload)
    else:
        emit(f"\n=== {result.title} ===\n")
        emit(result.summary())
        if saved:
            emit(f"{len(saved)} PNGs written to {args.save}")
    if not args.no_show:
        result.show()
    else:
        result.close()
    return 0


def cmd_preview_probe(args):
    """Prove reduced viewer tile decoding without saving biological pixels."""
    client = make_client(args)
    scans = _find_scans(args, client)
    if not scans:
        emit("No scan matched - nothing to probe.")
        return 0
    report = client.probe_preview_tiles(
        scans[0], wells=args.wells, channels=args.channels, site=args.site,
        compare_full=not args.no_compare,
        progress=ConsoleProgress(not args.quiet))
    if args.json:
        emit_json(report)
        return 0
    emit(f"\nVessel {report['vessel_id']} - {report['scan_time']}")
    emit(f"Levels: {len(report['levels'])}; lowest resolution: "
         f"{report.get('lowest_resolution_level')}")
    for channel in report.get("channels", {}).values():
        emit(f"\n{channel['name']} - {channel['route']}")
        for level in channel["levels"]:
            if level.get("error"):
                emit(f"  level {level['level']}: {level['error']}")
            else:
                emit(f"  level {level['level']}: "
                     f"{level['decoded_shape']} - {level['bytes']:,} bytes - "
                     f"{level.get('orientation', 'not compared')}")
    return 0


def cmd_plan(args):
    client = make_client(args)
    options = options_from_args(args)
    progress = ConsoleProgress(not args.quiet)
    plan = client.plan(options, progress=progress)
    progress._clear()
    if args.save_preset:
        options.save(args.save_preset)
    if args.json:
        emit_json(plan.to_dict())
        return 0
    emit()
    emit(plan.summary())
    if plan.items:
        emit("\nFirst files:")
        for name in plan.preview(8):
            emit(f"  {name}")
        if plan.output_file_count > 8:
            emit(f"  ... and {plan.output_file_count - 8:,} more")
    emit(f"\nEquivalent command:\n  {options.cli_command()}")
    return 0


def cmd_download(args):
    client = make_client(args)
    options = options_from_args(args)
    if args.save_preset:
        options.save(args.save_preset)

    progress = ConsoleProgress(not args.quiet)
    plan = client.plan(options, progress=progress)
    progress._clear()

    if not args.quiet:
        emit(plan.summary())
    if args.dry_run:
        emit("\nDry run - nothing downloaded.")
        return 0
    if plan.is_empty:
        return 0

    result = client.download(plan, progress=progress)
    progress._clear()

    if args.json:
        emit_json(result.to_dict())
    else:
        emit(f"\n{result.summary()}")
        if result.manifest_path:
            emit(f"Manifest: {result.manifest_path}")
        for message in result.errors[:10]:
            emit(f"  ! {message}")
        if len(result.errors) > 10:
            emit(f"  ! ... and {len(result.errors) - 10} more failures")
    return 0 if result.ok else 1


def cmd_watch(args):
    client = make_client(args)
    options = options_from_args(args)
    if args.save_preset:
        options.save(args.save_preset)

    emit("\n=== Watch mode ===")
    emit(f"  Host:     {client.host}")
    emit(f"  Vessels:  {', '.join(str(v) for v in options.vessels)}")
    emit(f"  Wells:    {options.wells}")
    emit(f"  Output:   {options.output}")
    emit(f"  Layout:   {options.layout} - {LAYOUT_DESCRIPTIONS[options.layout]}")
    emit(f"  Interval: every {options.interval_minutes} minutes")
    once = getattr(args, "once", False)
    if options.batches:
        emit(f"  Batch:    hold new frames until {options.batch_description}")
        delay = options.batch_delay
        if (not once and delay and delay >= timedelta(days=1)
                and options.interval_minutes < 30):
            # Every poll re-checks the whole window, chunk due or not.  Under
            # --once the cadence belongs to whatever schedules this rather than
            # to --interval, so the advice is about a number nobody can act on.
            emit(f"            (a chunk this long is happier with -i 60 than "
                 f"-i {options.interval_minutes})")
    emit("  One poll, then stop\n" if once else "  Press Ctrl+C to stop\n")

    progress = ConsoleProgress(not args.quiet)
    last_held = [None]

    def on_result(result):
        progress._clear()
        last_held[0] = None
        emit(f"[{datetime.now():%H:%M:%S}] {result.summary()}")

    def on_hold(watcher):
        # One line per change, not one per poll: a week-long chunk polls
        # hundreds of times and most of them have nothing new to say.
        if watcher.pending_frames == last_held[0]:
            return
        last_held[0] = watcher.pending_frames
        progress._clear()
        emit(f"[{datetime.now():%H:%M:%S}] {watcher.hold_description}")

    def on_error(exc):
        progress._clear()
        emit(f"[{datetime.now():%H:%M:%S}] error: {exc}")

    watcher = client.watch(options, on_result=on_result, on_error=on_error,
                           on_hold=on_hold, progress=progress, start=False)
    if once:
        return _watch_once(watcher, progress, json_mode=args.json)

    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        watcher.stop()
        progress._clear()
        emit("\nStopped.")
        if watcher.pending_frames:
            emit(f"  {watcher.hold_description}.")
            emit("  Those frames are still on the instrument. Collect them "
                 "now with:")
            emit(f"    {options.cli_command('download')}")
    return 0


def _watch_once(watcher, progress, json_mode=False):
    """One poll, and an exit code a scheduled task can branch on.

    ``poll_once`` raises where pylv200's ``poll`` returns: it has no internal
    try/except because ``run_forever`` is what catches for it.  Catching here
    rather than changing that keeps ``run_forever``'s contract - and without it
    the first unreachable poll would be a traceback and Python's own exit 1,
    which is the code that means "nothing was written".
    """
    try:
        result = watcher.poll_once()
    except (IncucyteError, OSError, ValueError) as exc:
        progress._clear()
        emit_error(type(exc).__name__, exc, "watch")
        return 2
    progress._clear()
    if result is None:
        # A held chunk, and nothing was written for it - deliberately, because
        # a file or a ledger entry here would make the next poll treat those
        # frames as already collected.
        if json_mode:
            emit_json({"ok": True, "status": "held", "written": 0,
                       "waiting": watcher.pending_frames})
        else:
            emit(f"  {watcher.hold_description}.")
        return NOTHING_WRITTEN
    if not result.files:
        if json_mode:
            emit_json(result.to_dict())
        else:
            emit("  Nothing new on the instrument.")
        return NOTHING_WRITTEN
    if json_mode:
        emit_json(result.to_dict())
    else:
        emit(result.summary())
    return 0


def _schedulable(options):
    """The recipe as a scheduled task can safely carry it.

    One line, because the rule belongs to the schedule rather than to the
    command line: it used to live here, so a schedule made from Python skipped
    it and could register a task writing into system32.
    """
    return schedule.Job(options).recipe()


def _schedule_rows(found):
    """One row per scheduled download, with what Windows last made of it."""
    meanings = {"0": "wrote something", "1": "nothing was due",
                "2": "could not reach the instrument",
                # Windows' own two, in the decimal schtasks prints them in:
                # 0x41301 and 0x41303. They are most of what a listing shows,
                # so bare numbers make the column unreadable exactly when it is
                # being read.
                "267009": "running now", "267011": "has not run yet"}
    return [[one["task"], one.get("status", ""), one.get("next_run", ""),
             one.get("last_run", ""),
             meanings.get(str(one.get("last_result") or "").strip(),
                          one.get("last_result", ""))]
            for one in found]


def cmd_schedule(args):
    """Hand one `watch --once` to Windows, so nothing has to stay open.

    `watch` downloads while a process runs. This is the same job with none:
    Windows starts one poll, it decides whether a chunk is due, and it exits.
    Nothing is held between firings and nothing needs to be - `batch_after`
    runs from each frame's own acquisition time and `batch_frames` counts the
    instrument against the ledger, so the poll after a reboot decides exactly
    what the poll before it would have decided.
    """
    if args.list:
        found = schedule.tasks()
        if args.json:
            emit_json({"schedules": found})
        elif found:
            print_table(_schedule_rows(found),
                        ["schedule", "state", "next run", "last run",
                         "last result"])
        else:
            emit("  Nothing is scheduled.")
        return 0 if found else NOTHING_WRITTEN

    if args.remove:
        removed = schedule.remove(args.remove)
        if args.json:
            emit_json({"removed": removed})
        else:
            emit("  Removed " + removed + ".")
        return 0

    # The recipe, not a command line built here. `Job.argv` is the one place a
    # recipe becomes `pyincucyte watch ... --once`, so this verb and
    # `IncucyteClient.schedule` cannot schedule two different things.
    job = schedule.Job(options_from_args(args))
    every, modifier = schedule.cadence_of(args.every)
    logged_out = not args.at_logon
    argv = job.argv()
    name = args.name or job.label()
    settings = dict(every=every, modifier=modifier, logged_out=logged_out,
                    account=args.account, wake=bool(args.wake),
                    start_after=args.start_after, replace=bool(args.replace))

    if args.dry_run:
        # `plan` is the one place the settings are split between the two
        # builders, so a dry run cannot compose something a real run would
        # refuse - which is exactly what a dry run is for.
        intended = schedule.plan(job, name=name, **settings)
        if args.json:
            emit_json({"task": intended["task"],
                       "would_run": subprocess.list2cmdline(argv),
                       "runs": intended["runs"],
                       "command": intended["command"],
                       "definition": intended["definition"]})
        else:
            emit("  Would create " + intended["task"])
            emit("  Every %s: %s" % (args.every,
                                     subprocess.list2cmdline(argv)))
            emit("  " + schedule.render(intended["command"]))
        return 0

    if logged_out:
        # Said before Windows asks, or the prompt arrives with no explanation
        # of whose credential it wants or why a download needs one at all.
        emit("  Windows will ask for the credential of %s, so this can "
             "download while nobody is logged in."
             % (args.account or schedule.default_account()))
        emit("  It goes straight to Windows. PyIncucyte never sees it and "
             "never stores it.")

    made = schedule.register(job, name=name, **settings)
    if args.run_now:
        schedule.run_now(name)
    if args.json:
        emit_json({**made, "every": args.every,
                   "would_run": subprocess.list2cmdline(argv)})
    else:
        emit("  Created " + made["task"])
        emit("  Every %s: %s" % (args.every, subprocess.list2cmdline(argv)))
        emit("  Runs " + ("whether anyone is logged in or not" if logged_out
                          else "only while you are logged on"))
        emit("  Verified: " + ", ".join(
            "%s=%s" % (k, v) for k, v in sorted(made["settings"].items())))
        if args.run_now:
            emit("  Fired once now.")
    return 0


def cmd_status(args):
    """What the instrument is doing. Exits non-zero if it reports a fault."""
    client = make_client(args)
    state = client.device_state()
    patterns = {}
    for vessel_id in getattr(args, "vessel", None) or []:
        patterns[vessel_id] = client.scan_pattern(vessel_id)
    if args.json:
        payload = state.to_dict()
        if patterns:
            payload["vessels"] = {
                str(vessel_id): {"pattern": pattern.name,
                                 "wells": pattern.well_count,
                                 "images_per_well": pattern.images_per_well}
                for vessel_id, pattern in patterns.items()}
        if getattr(args, "raw", False):
            payload["raw"] = state.raw
        emit_json(payload)
        return 1 if state.has_problem else 0
    emit(f"\n=== {client.host} ===\n")
    for line in state.describe():
        emit(f"  {line}")
    for vessel_id, pattern in patterns.items():
        emit(f"  Vessel {vessel_id}:    {pattern.summary()}")
    if getattr(args, "raw", False):
        emit("\n" + json.dumps(state.raw, indent=2, default=str)[:4000])
    return 1 if state.has_problem else 0


def confirm_write(args, question):
    """``--yes``, or an interactive yes. A script without a tty gets neither.

    Returning False is not a refusal here - it lets the client raise
    ConfirmationRequiredError, which says how to confirm properly.
    """
    if getattr(args, "yes", False):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    emit(question)
    try:
        return input("  Type yes to go ahead: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_scan_now(args):
    """Ask the instrument to scan one vessel now. Writes; needs confirming."""
    client = make_client(args)
    vessel_id = args.vessel
    state = client.device_state()
    emit(f"\n=== {client.host} ===\n")
    for line in state.describe():
        emit(f"  {line}")
    emit("")
    agreed = confirm_write(
        args, f"  This asks the instrument to scan vessel {vessel_id} now.")
    client.begin_scan(vessel_id, confirm=agreed, force=args.force, state=state)
    emit(f"  Requested a scan of vessel {vessel_id}.")
    emit("  The instrument decides when it actually runs - watch it with "
         "'pyincucyte status'.")
    return 0


def cmd_unmix(args):
    """Show a vessel's unmixing on the instrument, and optionally change it."""
    client = make_client(args)
    current = client.unmixing(args.vessel)
    emit(f"\n=== Vessel {args.vessel} ===\n")
    emit(f"  On the instrument: {current.describe()}")
    emit(f"  As a spec:         {current.to_spec() or '-'}")
    if not args.set:
        emit("")
        emit("  Downloading with --unmix does this arithmetic locally and "
             "changes nothing on the device.")
        emit(f"  To change what the Incucyte software displays: "
             f"pyincucyte unmix -v {args.vessel} --set SPEC --yes")
        return 0
    wanted = Unmixing.coerce(args.set)
    emit(f"  Would become:      {wanted.describe()}")
    emit("")
    agreed = confirm_write(
        args, f"  This changes what the Incucyte software shows everyone for "
              f"vessel {args.vessel}.")
    client.save_unmix(args.vessel, wanted, confirm=agreed, force=args.force)
    emit(f"  Saved: {wanted.describe()}")
    return 0


def cmd_manifest(args):
    path = Path(args.path)
    if path.is_dir():
        path = path / MANIFEST_FILENAME
    manifest = load_manifest(path)
    if manifest is None:
        emit(f"No manifest at {path}")
        return 1
    if args.json:
        emit_json(manifest)
        return 0
    stats = manifest.get("stats", {})
    emit(f"\n=== {path} ===\n")
    emit(f"  Written by:  {manifest.get('generated_by')}")
    emit(f"  Output dir:  {manifest.get('output_dir')}")
    emit(f"  Layout:      {manifest.get('layout')} ({manifest.get('axes')})")
    emit(f"  Files:       {stats.get('file_count', 0):,}")
    emit(f"  Size:        {human_bytes(stats.get('bytes_total', 0))}")
    emit(f"  Wells:       {len(stats.get('wells', []))}")
    emit(f"  Channels:    {', '.join(stats.get('channels', [])) or '-'}")
    emit(f"  Scan times:  {len(manifest.get('scan_times', [])):,}")
    emit(f"  Runs:        {len(manifest.get('runs', []))}")
    emit(f"  Finished:    {stats.get('complete_files', 0):,} of "
         f"{stats.get('file_count', 0):,}")
    filling = stats.get("filling_files", 0)
    unstated = stats.get("unstated_files", 0)
    if filling:
        emit(f"  Still filling: {filling:,} - a time stack gains frames on "
             f"every poll, so these are not finished")
    if unstated:
        emit(f"  Not stated:  {unstated:,} - downloaded without anything "
             f"saying whether the experiment had ended")
    return 0


def cmd_preset(args):
    options = ExportOptions.load(args.path)
    if args.json:
        emit_json(options.to_dict())
        return 0
    emit(f"\n=== {args.path} ===\n")
    for line in options.describe():
        emit(f"  {line}")
    emit(f"\n  Command: {options.cli_command()}")
    return 0


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------

def add_finder_args(parser):
    """The filters that answer "which plate is this?" - shared by find/preview."""
    parser.add_argument("name", nargs="?",
                        help="Part of the plate's name (a bare number is a "
                             "vessel id)")
    parser.add_argument("--vessel", "-v", type=int, action="append",
                        help="Vessel ID (repeat for several)")
    parser.add_argument("--owner", help="Part of the owner's username")
    parser.add_argument("--plate", help="Well count or plate type (24, Sarstedt)")
    parser.add_argument("--channel",
                        help="A channel the experiment uses (GFP, green, 2)")
    parser.add_argument("--at", metavar="WHEN",
                        help=f"Take the scan nearest this moment ({WHEN_HELP})")
    parser.add_argument("--since", metavar="WHEN", help="Look no earlier than this")
    parser.add_argument("--until", metavar="WHEN", help="Look no later than this")
    parser.add_argument("--most-recent", "-n", dest="most_recent", type=int,
                        default=1, metavar="N",
                        help="Newest N scans per vessel (default 1)")
    parser.add_argument("--max-days", dest="max_days", type=int, default=14,
                        metavar="DAYS",
                        help="How far back to walk from each vessel's last "
                             "scan (default 14)")
    parser.add_argument("--limit", type=int, help="Stop after this many scans")


def add_write_args(parser):
    """The two flags every command that changes the instrument carries.

    The Incucyte is shared, so a write says so out loud or does not happen.
    """
    parser.add_argument("--yes", action="store_true",
                        help="Confirm this change to the instrument")
    parser.add_argument("--force", action="store_true",
                        help="Send it even if the instrument reports a fault")


def add_selection_args(parser, *, watch=False):
    parser.add_argument("--vessel", "-v", type=int, action="append",
                        help="Vessel ID (repeat for several)")
    parser.add_argument("--output", "-o", help="Output folder")
    parser.add_argument("--wells", "-w",
                        help="Well filter (A1, A1,B3, A1-D4, all)")
    parser.add_argument("--vessel-wells", action="append", metavar="ID:WELLS",
                        help="Per-vessel well filter, e.g. 38:A1-D4")
    parser.add_argument("--filter", "-f", action="append", metavar="ID[:WELLS]",
                        help="Alias for --vessel-wells (repeatable)")
    parser.add_argument("--channels", "-c", help=f"Channel filter ({CHANNEL_HELP})")
    parser.add_argument("--layout", choices=sorted(LAYOUTS),
                        help="Output layout (default: separate)")
    parser.add_argument("--hyperstack", action="store_true",
                        help="Shorthand for --layout channel_stack")
    parser.add_argument("--time-stack", action="store_true", dest="time_stack",
                        help="Shorthand for --layout time_stack")
    parser.add_argument("--date", "-d", help="Single day (YYYY-MM-DD)")
    parser.add_argument("--start-from", "-s", dest="start_from",
                        metavar="WHEN",
                        help=f"'first', 'today', 'now', {MOMENT_HELP}")
    parser.add_argument("--end-at", dest="end_at", metavar="WHEN",
                        help=f"'now' (default), {MOMENT_HELP}. A bare date "
                             f"includes the whole of that day; '+100f' takes "
                             f"the first 100 frames from the start.")
    parser.add_argument("--end-date", dest="end_date",
                        help=argparse.SUPPRESS)      # old spelling of --end-at
    parser.add_argument("--scan-time", "-t", dest="scan_time",
                        help="Only scan times containing this text")
    parser.add_argument("--workers", type=int, help="Parallel workers (default 4)")
    parser.add_argument("--calibrate", action="store_true", default=None,
                        help="Write fluorescence in calibrated units (GCU/RCU) "
                             "as 32-bit float, using the device's own Scale "
                             "and Bias")
    parser.add_argument("--no-calibrate", action="store_false", dest="calibrate",
                        help="Keep raw 16-bit counts (default)")
    parser.add_argument("--unmix", metavar="SPEC",
                        help=literal(f"Linear (spectral) unmixing: {UNMIX_HELP}"))
    parser.add_argument("--background", metavar="LEVEL",
                        help=literal(f"Subtract a background level: {BACKGROUND_HELP}"))
    parser.add_argument("--state-scope",
                        choices=["auto", "folder", "global", "none"],
                        help="Where the resume ledger lives (default: auto)")
    parser.add_argument("--no-append", action="store_true",
                        help="rewrite every time stack whole, instead of adding "
                             "new frames to the file already on disk")
    parser.add_argument("--no-manifest", action="store_true",
                        help="Do not write pyincucyte-manifest.json")
    parser.add_argument("--cache", dest="cache_payloads",
                        choices=["auto", "always", "never"],
                        help="Cache source payloads so rebuilt stacks do not "
                             "re-download (default: auto - on for time stacks)")
    parser.add_argument("--preset", help="Load an ExportOptions JSON preset")
    parser.add_argument("--save-preset", help="Write the resolved options to JSON")
    parser.add_argument("--config", help="Legacy JSON vessel/well config")
    if watch:
        parser.add_argument("--interval", "-i", type=int,
                            help="Poll interval in minutes (default 10)")
        parser.add_argument("--batch-frames", type=int, metavar="N",
                            help="Hold new frames back until N of them are "
                                 "waiting, then fetch the chunk in one go "
                                 "(default: download each frame on sight)")
        parser.add_argument("--batch-after", metavar="SPAN",
                            help=f"...or until the oldest waiting frame is "
                                 f"this old: {SPAN_HELP}. Given both, "
                                 f"whichever comes first wins")
        parser.add_argument("--once", action="store_true",
                            help="Poll once and stop, for a scheduled task. "
                                 "Exit 0 if a chunk landed, 1 if nothing was "
                                 "written (still holding, or nothing new), "
                                 "2 if the instrument could not be reached")


# argparse treats anything starting with "-" as an option, so
# ``--start-from -48h`` fails with a confusing "expected one argument".  These
# are the flags whose values can legitimately start with a minus, and this is
# the shape such a value takes: a signed number and a unit letter.
NEGATIVE_VALUE_FLAGS = {"--start-from", "-s", "--end-at", "--end-date",
                        "--date", "-d", "--at", "--since", "--until"}
_NEGATIVE_VALUE = re.compile(r"^-\d+(?:\.\d+)?\s*[smhdwf]", re.IGNORECASE)


def join_negative_values(argv):
    """Rewrite ``--start-from -48h`` as ``--start-from=-48h`` before parsing."""
    fixed, index = [], 0
    while index < len(argv):
        token = argv[index]
        following = argv[index + 1] if index + 1 < len(argv) else None
        if (token in NEGATIVE_VALUE_FLAGS and following
                and _NEGATIVE_VALUE.match(following)):
            fixed.append(f"{token}={following}")
            index += 2
            continue
        fixed.append(token)
        index += 1
    return fixed


def parse_args(argv=None):
    """Parse argv, tolerating values that begin with a minus sign."""
    argv = list(sys.argv[1:] if argv is None else argv)
    return build_parser().parse_args(join_negative_values(argv))


def cmd_gui(args):
    """Open the desktop app.

    Imported here rather than at the top of the module so the command line
    never needs Tk installed just to plan a download.
    """
    from .gui.app import main as gui_main
    return gui_main()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pyincucyte",
        description="Download Incucyte live-cell images: plan, fetch, watch.")
    parser.add_argument("--host", default=None,
                        help="Device address. No default - set PYINCUCYTE_HOST, "
                             "or `login --host <address>` saves it")
    parser.add_argument("--config-file", help="Path to the saved-credentials file")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="No progress display")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--version", action="version",
                        version=f"pyincucyte {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("probe", help="Check the device is reachable")

    p_login = sub.add_parser("login", help="Log in and save credentials")
    p_login.add_argument("--username", "-u")
    p_login.add_argument("--password", "-p")
    p_login.add_argument("--name", help="Friendly name for this device")

    sub.add_parser("logout", help="Forget the selected device's saved login")
    sub.add_parser("gui", help="Open the desktop app")
    sub.add_parser("vessels", help="List vessels")
    p_status = sub.add_parser(
        "status", help="Show what the instrument is doing")
    p_status.add_argument("--vessel", "-v", type=int, action="append",
                          help="Also show this vessel's scan pattern "
                               "(repeat for several)")
    p_status.add_argument("--raw", action="store_true",
                          help="Include the untouched payload")

    p_scan_now = sub.add_parser(
        "scan-now", help="Ask the instrument to scan one vessel now (writes)")
    p_scan_now.add_argument("--vessel", "-v", type=int, required=True,
                            help="Vessel ID to scan")
    add_write_args(p_scan_now)

    p_unmix = sub.add_parser(
        "unmix", help="Show, or change, the unmixing saved on a vessel")
    p_unmix.add_argument("--vessel", "-v", type=int, required=True,
                         help="Vessel ID")
    p_unmix.add_argument("--set", metavar="SPEC",
                         help=literal(f"Save these coefficients to the "
                                      f"instrument: {UNMIX_HELP}"))
    add_write_args(p_unmix)

    p_scans = sub.add_parser("scans", help="List scan times")
    p_scans.add_argument("--date", "-d")
    p_scans.add_argument("--start-from", "-s", dest="start_from", metavar="WHEN")
    p_scans.add_argument("--end-at", dest="end_at", metavar="WHEN")
    p_scans.add_argument("--end-date", dest="end_date", help=argparse.SUPPRESS)

    p_find = sub.add_parser(
        "find", help="Find a vessel and its most recent scans")
    add_finder_args(p_find)

    p_preview = sub.add_parser(
        "preview", help="Show thumbnails of the wells, to check the vessel")
    add_finder_args(p_preview)
    p_preview.add_argument("--wells", "-w",
                           help="Well filter (A1, A1,B3, A1-D4, all)")
    p_preview.add_argument("--channels", "-c",
                           help=f"Channel filter ({CHANNEL_HELP})")
    p_preview.add_argument("--site", type=int, default=0,
                           help="Which site within each well (default 0)")
    p_preview.add_argument("--size", type=int, default=DEFAULT_SIZE,
                           help=f"Thumbnail edge in pixels (default {DEFAULT_SIZE})")
    p_preview.add_argument("--contrast", default="auto",
                           choices=["auto", "minmax", "raw"],
                           help="Display stretch (default: auto)")
    p_preview.add_argument("--max-images", dest="max_images", type=int,
                           default=DEFAULT_MAX_IMAGES, metavar="N",
                           help=f"Stop the static well wall after N images "
                                f"(default {DEFAULT_MAX_IMAGES})")
    p_preview.add_argument("--save", metavar="DIR",
                           help="Also write the thumbnails there as PNGs")
    p_preview.add_argument("--no-show", dest="no_show", action="store_true",
                           help="Do not open a window")
    p_preview.add_argument("--workers", type=int, help="Parallel workers (default 4)")
    p_preview.add_argument("--calibrate", action="store_true",
                           help="Apply calibration (invisible here - the "
                                "contrast stretch undoes a linear rescale)")
    p_preview.add_argument("--unmix", metavar="SPEC",
                           help=literal(f"Preview an unmixing before "
                                        f"downloading with it: {UNMIX_HELP}"))
    p_preview.add_argument("--background", metavar="LEVEL",
                           help=literal(f"Preview a background subtraction: "
                                        f"{BACKGROUND_HELP}"))

    p_protocol = sub.add_parser(
        "protocol", help="How the run was set up: the time loop, the wells "
                         "and the channel chain, drawn")
    add_finder_args(p_protocol)
    p_protocol.add_argument("--out", "-o", metavar="FILE",
                            help="Write the drawing here: .svg costs nothing, "
                                 ".png and .pdf need matplotlib. A folder "
                                 "means <vessel>-protocol.svg inside it.")
    p_protocol.add_argument("--dark", action="store_true",
                            help="The dark palette, for a dark slide")
    p_protocol.add_argument("--no-scan", dest="no_scan", action="store_true",
                            help="The plan alone: do not sweep the run's whole "
                                 "lifetime. Much faster on a long experiment, "
                                 "and it costs the achieved cadence, the "
                                 "progress and whether the instrument is "
                                 "acquiring.")
    p_protocol.add_argument("--channel-names", dest="channel_names",
                            metavar="NAMES",
                            help="Override the device's channel names, in "
                                 "acquisition order (Phase,Cry1-GFP)")
    # Not through literal(): there is no literal % here, and escaping the one
    # in %(default)s stops argparse substituting it.
    p_protocol.add_argument("--width", type=int, default=96, metavar="COLUMNS",
                            help="Terminal width for the drawing "
                                 "(default: %(default)s)")

    p_timeline = sub.add_parser(
        "timeline", help="Scrub through a run without loading every image")
    add_finder_args(p_timeline)
    for action in p_timeline._actions:
        if action.dest == "most_recent":
            action.default = None
            action.help = "Keep only the newest N positions (default: all)"
    p_timeline.add_argument("--wells", "-w",
                            help="Well filter; the window starts at the first")
    p_timeline.add_argument("--channels", "-c",
                            help=f"Channel filter ({CHANNEL_HELP})")
    p_timeline.add_argument("--site", type=int, default=0,
                            help="Which site within each well (default 0)")
    p_timeline.add_argument(
        "--size", type=int, default=DEFAULT_TIMELINE_SIZE,
        help=f"Preview edge in pixels (default {DEFAULT_TIMELINE_SIZE})")
    p_timeline.add_argument("--contrast", default="auto",
                            choices=["auto", "minmax", "raw"],
                            help="Display stretch (default: auto)")
    p_timeline.add_argument("--anchors", type=int, default=100, metavar="N",
                            help="Evenly spaced initial frames (default 100)")
    p_timeline.add_argument("--frame-cache", type=int, default=128, metavar="N",
                            help="Native frames kept in memory (default 128)")
    p_timeline.add_argument("--render-cache", type=int, default=96, metavar="N",
                            help="Display frames kept in memory (default 96)")
    p_timeline.add_argument("--proxy-dir", metavar="DIR",
                            help="Reuse disposable native preview frames here")
    p_timeline.add_argument("--local-stack", metavar="TIFF",
                            help="Read an existing local time stack first")
    p_timeline.add_argument("--output", "-o", metavar="DIR",
                            help="Reuse exported stacks and preview proxies here")
    p_timeline.add_argument("--save", metavar="DIR",
                            help="Save the initial anchor previews as PNGs")
    p_timeline.add_argument("--no-show", dest="no_show", action="store_true",
                            help="Do not open the scrubber window")
    p_timeline.add_argument("--calibrate", action="store_true",
                            help="Apply the acquisition calibration")
    p_timeline.add_argument("--unmix", metavar="SPEC",
                            help=literal(f"Preview an unmixing: {UNMIX_HELP}"))
    p_timeline.add_argument("--background", metavar="LEVEL",
                            help=literal(f"Preview background subtraction: "
                                         f"{BACKGROUND_HELP}"))

    p_preview_probe = sub.add_parser(
        "preview-probe", help="Test the private reduced-tile route read-only")
    add_finder_args(p_preview_probe)
    p_preview_probe.add_argument("--wells", "-w",
                                 help="Probe the first matching acquired well")
    p_preview_probe.add_argument("--channels", "-c",
                                 help=f"Channel filter ({CHANNEL_HELP})")
    p_preview_probe.add_argument("--site", type=int, default=0)
    p_preview_probe.add_argument(
        "--no-compare", action="store_true",
        help="Do not fetch a full TIFF for the orientation comparison")

    p_plan = sub.add_parser("plan", help="Show what a download would fetch")
    add_selection_args(p_plan)

    p_download = sub.add_parser("download", help="Download images once")
    add_selection_args(p_download)
    p_download.add_argument("--dry-run", action="store_true",
                            help="Plan only, write nothing")

    p_watch = sub.add_parser("watch", help="Poll and download new scans forever")
    add_selection_args(p_watch, watch=True)

    p_schedule = sub.add_parser(
        "schedule",
        help="Ask Windows to keep downloading, with nothing left open")
    add_selection_args(p_schedule, watch=True)
    p_schedule.add_argument("--name", metavar="NAME",
                            help="What to call the schedule "
                                 "(default: the vessels)")
    p_schedule.add_argument("--every", metavar="PERIOD", default="1h",
                            help="How often Windows should check: 10m, 1h, "
                                 "6h, 1d (default: %(default)s)")
    p_schedule.add_argument("--at-logon", action="store_true",
                            help="Run only while somebody is logged on, and "
                                 "ask for no credential. The default keeps "
                                 "running on a rebooted, locked machine.")
    p_schedule.add_argument("--account", metavar="USER",
                            help="The Windows account to run as "
                                 "(default: this user)")
    p_schedule.add_argument("--start-after", metavar="PERIOD", default=None,
                            help="Put the first check off this long - '7d' to "
                                 "leave a week of acquisition alone before "
                                 "anything is downloaded")
    p_schedule.add_argument("--wake", action="store_true",
                            help="Wake a sleeping computer for each check")
    p_schedule.add_argument("--replace", action="store_true",
                            help="Overwrite a schedule with this name")
    p_schedule.add_argument("--run-now", action="store_true",
                            help="Fire it once as soon as it is created")
    p_schedule.add_argument("--dry-run", action="store_true",
                            help="Show the task that would be created, and "
                                 "create nothing")
    p_schedule.add_argument("--list", action="store_true",
                            help="What is already scheduled, and how each one "
                                 "is doing")
    p_schedule.add_argument("--remove", metavar="NAME",
                            help="Delete one scheduled download")

    p_manifest = sub.add_parser("manifest", help="Summarise a download manifest")
    p_manifest.add_argument("path", help="Manifest file or output folder")

    p_preset = sub.add_parser("preset", help="Show a saved preset")
    p_preset.add_argument("path")

    return parser


COMMANDS = {
    "probe": cmd_probe, "login": cmd_login, "logout": cmd_logout, "gui": cmd_gui,
    "vessels": cmd_vessels, "scans": cmd_scans, "plan": cmd_plan,
    "find": cmd_find, "preview": cmd_preview, "timeline": cmd_timeline,
    "protocol": cmd_protocol,
    "preview-probe": cmd_preview_probe,
    "download": cmd_download, "watch": cmd_watch, "schedule": cmd_schedule,
    "status": cmd_status,
    "scan-now": cmd_scan_now, "unmix": cmd_unmix,
    "manifest": cmd_manifest, "preset": cmd_preset,
}


def configure_logging(args):
    level = logging.DEBUG if args.verbose else (
        logging.WARNING if args.quiet else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger("pyincucyte")
    root.handlers = [handler]
    root.setLevel(level)
    # Per-file chatter belongs at debug level once there is a progress bar.
    logging.getLogger("pyincucyte.engine").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _JSON[0] = "--json" in argv
    command = next((token for token in argv if token in COMMANDS), "")
    if _JSON[0]:
        parsed_error = io.StringIO()
        try:
            with contextlib.redirect_stderr(parsed_error):
                args = parse_args(argv)
        except SystemExit as exc:
            if exc.code:
                detail = parsed_error.getvalue().strip()
                message = detail.splitlines()[-1] if detail else "invalid command line"
                prefix = "pyincucyte: error: "
                if detail:
                    print(detail, file=sys.stderr)
                emit_json({
                    "ok": False,
                    "error": {
                        "type": "UsageError",
                        "message": (message[len(prefix):]
                                    if message.startswith(prefix) else message),
                    },
                    "command": command,
                })
            raise
    else:
        args = parse_args(argv)
    _JSON[0] = bool(getattr(args, "json", False))
    if not args.command:
        build_parser().print_help()
        return 0
    configure_logging(args)
    try:
        return COMMANDS[args.command](args) or 0
    except KeyboardInterrupt:
        emit_error("KeyboardInterrupt", "Stopped.", args.command)
        return 130
    except IncucyteError as exc:
        emit_error(type(exc).__name__, exc, args.command)
        return 2
    except (ValueError, OSError) as exc:
        emit_error(type(exc).__name__, exc, args.command)
        return 2
    except Exception as exc:                          # noqa: BLE001
        # An unexpected traceback would otherwise exit 1, which is
        # NOTHING_WRITTEN - a completely normal poll. A scheduled task records
        # only that number, so a schedule crashing on every firing and one
        # with nothing to download would read the same in the one column
        # anybody looks at. The traceback is still printed; only the code it
        # leaves behind changes.
        traceback.print_exc()
        emit_error(type(exc).__name__, exc, args.command)
        return 2


if __name__ == "__main__":
    sys.exit(main())
