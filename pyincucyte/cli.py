"""Command line front end.

Every flag the original ``incucyte_downloader.py`` accepted still works.  What
is new is aimed at scripting: ``--preset`` to load a saved recipe, ``--json``
so another program can read the output, and ``plan`` to see exactly what a
download would do before it runs.
"""

import argparse
import getpass
import json
import logging
import re
import sys
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
    LAYOUT_DESCRIPTIONS, LAYOUTS, human_bytes, layout_from_flags, resolve_layout,
)
from .preview import DEFAULT_MAX_IMAGES, DEFAULT_SIZE
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
    print(text)


def emit_json(payload):
    print(json.dumps(payload, indent=2, default=str))


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

    if getattr(args, "green_phase", None) is not None:
        changes["green_lut"] = bool(args.green_phase)
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
    credentials = client.login(username, password)
    hours = credentials.token_seconds_left / 3600
    emit(f"Logged in as {username} on {client.host}.")
    emit(f"Token valid for {hours:.1f} hours. Saved to {client.store.path}")
    return 0


def cmd_logout(args):
    client = make_client(args)
    client.logout()
    emit("Saved login removed.")
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
    if options.batches:
        emit(f"  Batch:    hold new frames until {options.batch_description}")
        delay = options.batch_delay
        if delay and delay >= timedelta(days=1) and options.interval_minutes < 30:
            # Every poll re-checks the whole window, chunk due or not.
            emit(f"            (a chunk this long is happier with -i 60 than "
                 f"-i {options.interval_minutes})")
    emit("  Press Ctrl+C to stop\n")

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
    parser.add_argument("--green-lut", action="store_true", dest="green_phase",
                        default=None, help="Apply a green LUT to Phase images")
    parser.add_argument("--no-green-lut", action="store_false", dest="green_phase",
                        help="Keep Phase images as the device returns them")
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

    sub.add_parser("logout", help="Forget the saved login")
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
                           help=f"Stop after N images - each one is a full-size "
                                f"download (default {DEFAULT_MAX_IMAGES})")
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

    p_plan = sub.add_parser("plan", help="Show what a download would fetch")
    add_selection_args(p_plan)

    p_download = sub.add_parser("download", help="Download images once")
    add_selection_args(p_download)
    p_download.add_argument("--dry-run", action="store_true",
                            help="Plan only, write nothing")

    p_watch = sub.add_parser("watch", help="Poll and download new scans forever")
    add_selection_args(p_watch, watch=True)

    p_manifest = sub.add_parser("manifest", help="Summarise a download manifest")
    p_manifest.add_argument("path", help="Manifest file or output folder")

    p_preset = sub.add_parser("preset", help="Show a saved preset")
    p_preset.add_argument("path")

    return parser


COMMANDS = {
    "probe": cmd_probe, "login": cmd_login, "logout": cmd_logout, "gui": cmd_gui,
    "vessels": cmd_vessels, "scans": cmd_scans, "plan": cmd_plan,
    "find": cmd_find, "preview": cmd_preview,
    "download": cmd_download, "watch": cmd_watch, "status": cmd_status,
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
    args = parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    configure_logging(args)
    try:
        return COMMANDS[args.command](args) or 0
    except KeyboardInterrupt:
        emit("\nStopped.")
        return 130
    except IncucyteError as exc:
        emit(f"error: {exc}")
        return 2
    except (ValueError, OSError) as exc:
        emit(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
