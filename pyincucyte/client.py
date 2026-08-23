"""The one object a script or pipeline needs: :class:`IncucyteClient`.

    from pyincucyte import IncucyteClient

    with IncucyteClient.from_saved() as incucyte:
        plan = incucyte.plan(vessel=38, output="./images",
                             wells="A1-D6", channels="phase,green",
                             layout="time_channel_stack", start_from="first")
        print(plan.summary())              # dry run - nothing fetched yet
        result = incucyte.download(plan)   # writes files + a manifest
        for image in result.files:
            analyse(image.path, well=image.well, channels=image.channels)

The client owns the token (refreshing it silently), caches the vessel list, and
turns the engine's loose dicts into the typed records in
:mod:`pyincucyte.models`.
"""

import logging
import threading
from datetime import date, datetime
from pathlib import Path

from . import channels as ch
from . import engine
from .cache import cache_for_output
from . import manifest as manifest_mod
from . import wells as wl
from .config import ConfigStore, Credentials
from .errors import NotLoggedInError, VesselNotFoundError
from .models import (
    DownloadResult, ExportPlan, OutputFile, ProgressEvent, Vessel,
    LAYOUT_AXES, layout_flags, resolve_layout,
)
from .options import ExportOptions, START_FIRST
from .state import StateStore

log = logging.getLogger("pyincucyte.client")


def _as_event(callback):
    """Return a cancel Event, accepting None or an existing Event."""
    return callback if callback is not None else threading.Event()


def _call(callback, event):
    if callback:
        try:
            callback(event)
        except Exception:  # a broken progress hook must not kill a download
            log.debug("progress callback raised", exc_info=True)


class IncucyteClient:
    """A connected session with one Incucyte device."""

    def __init__(self, host=None, *, store=None, credentials=None,
                 timeout=None, logger=None):
        self.store = store or ConfigStore()
        self._credentials = credentials or self.store.load()
        if host:
            self._credentials.host = host
        self.host = self._credentials.host or engine.DEFAULT_HOST
        self.log = logger or log
        self._vessels = None
        self._auth_lock = threading.Lock()
        if timeout:
            engine.API_TIMEOUT = int(timeout)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_saved(cls, host=None, *, store=None):
        """Open a client from the saved login. Raises if there isn't one."""
        store = store or ConfigStore()
        credentials = store.require()
        return cls(host or credentials.host, store=store, credentials=credentials)

    @classmethod
    def connect(cls, host, username, password, *, store=None, save=True):
        """Log in with a plaintext password and return a ready client."""
        client = cls(host, store=store)
        client.login(username, password, save=save)
        return client

    # -- authentication ---------------------------------------------------

    @property
    def credentials(self):
        return self._credentials

    @property
    def token(self):
        return self._credentials.token

    @property
    def is_connected(self):
        return self._credentials.token_valid

    @property
    def username(self):
        return self._credentials.username

    def login(self, username, password, *, save=True):
        """Encrypt the password with the vendor assembly, then authenticate."""
        encrypted = engine.encrypt_password(password)
        return self.login_encrypted(username, encrypted, save=save)

    def login_encrypted(self, username, encrypted_password, *, save=True):
        """Authenticate with an already-encrypted password."""
        token, expires_in = engine.get_token(self.host, username, encrypted_password)
        credentials = Credentials(
            host=self.host, username=username,
            encrypted_password=encrypted_password,
            login_time=datetime.now().isoformat(),
        ).with_token(token, expires_in)
        self._credentials = credentials
        self._vessels = None
        if save:
            self.store.save(credentials)
        return credentials

    def ensure_token(self):
        """Return a valid bearer token, refreshing it if it has expired."""
        with self._auth_lock:
            if self._credentials.token_valid:
                return self._credentials.token
            if not self._credentials.can_refresh:
                raise NotLoggedInError(
                    "No saved credentials to refresh - log in first.")
            token, expires_in = engine.get_token(
                self.host, self._credentials.username,
                self._credentials.encrypted_password)
            self._credentials = self._credentials.with_token(token, expires_in)
            self.store.save(self._credentials)
            self.log.debug("refreshed Incucyte token")
            return self._credentials.token

    def logout(self):
        """Forget the saved login."""
        self.store.clear()
        self._credentials = Credentials(host=self.host)
        self._vessels = None

    # -- raw calls --------------------------------------------------------

    def call(self, route, payload=None, *, unpack=True):
        """POST to an arbitrary API route and return its ``Data`` payload."""
        data = engine.api_post(self.host, self.ensure_token(), route, payload)
        if not unpack:
            return data
        return engine.unpack_values(data.get("Data", data))

    def probe(self):
        """Check reachability and report which login modes the device allows."""
        import socket

        report = {"host": self.host, "ports": {}, "api": False,
                  "device_login": None, "windows_login": None}
        for port in (80, 443, 808):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            try:
                report["ports"][port] = sock.connect_ex((self.host, port)) == 0
            finally:
                sock.close()
        try:
            url = (f"{engine.API_BASE_TEMPLATE.format(host=self.host)}"
                   f"/api/Connections/GetDeviceLoginModes")
            response = engine.session_for(self.host).post(url, json={}, timeout=10)
            modes = engine.unpack_values(response.json().get("Data", {}))
            report["api"] = True
            report["device_login"] = bool(modes.get("IsDeviceLoginAllowed"))
            report["windows_login"] = bool(modes.get("IsWindowsLoginAllowed"))
        except Exception as exc:  # probe must never raise
            report["error"] = str(exc)
        return report

    def device_status(self):
        """Return the device's current status block."""
        return self.call("Device/Status/GetDeviceStatusUpdate")

    # -- vessels ----------------------------------------------------------

    def vessels(self, refresh=False):
        """Return every vessel on the device as :class:`Vessel` objects."""
        if self._vessels is None or refresh:
            data = engine.api_post(self.host, self.ensure_token(),
                                   "Vessels/GetAllSearchVessels")
            records = engine.extract_search_vessels(data)
            seen, vessels = set(), []
            for record in records:
                vessel = Vessel.from_record(record)
                if vessel is None or vessel.id in seen:
                    continue
                seen.add(vessel.id)
                vessels.append(vessel)
            self._vessels = sorted(vessels, key=lambda v: v.id)
        return list(self._vessels)

    def vessel(self, vessel_id):
        """Return one vessel by id. Raises :class:`VesselNotFoundError`."""
        vessel_id = int(vessel_id)
        for vessel in self.vessels():
            if vessel.id == vessel_id:
                return vessel
        for vessel in self.vessels(refresh=True):
            if vessel.id == vessel_id:
                return vessel
        raise VesselNotFoundError(f"Vessel {vessel_id} is not on {self.host}")

    # -- scan times -------------------------------------------------------

    def scan_times(self, day=None):
        """Return every scan time on one calendar day."""
        day = day or date.today()
        if isinstance(day, datetime):
            day = day.date()
        data = self.call("Scans/AllScanTimes",
                         {"Year": day.year, "Month": day.month, "Day": day.day})
        return data if isinstance(data, list) else []

    def scan_times_between(self, start, end=None, *, progress=None, cancel=None,
                           reverse=False, enough=None):
        """Return every scan time from ``start`` to ``end`` (inclusive).

        ``reverse`` walks backwards from ``end``, and ``enough`` stops the walk
        early - together they are how a frame count is satisfied without
        querying every day of a long experiment.
        """
        cancel = _as_event(cancel)

        def on_day(day, done, total):
            _call(progress, ProgressEvent(
                stage="scanning", detail=f"Checking {day.isoformat()}",
                done=done, total=total, unit="days"))

        return engine.collect_scans_in_range(
            self.host, self.ensure_token(), start, end,
            progress_callback=on_day if progress else None,
            stop_event=cancel, reverse=reverse, enough=enough)

    def first_scan_time(self, vessel_id=None, max_days_back=90):
        """Return the experiment's first scan time.

        Uses the vessel's own metadata when a vessel is given (one call);
        otherwise walks days backwards across the whole device.
        """
        if vessel_id is not None:
            try:
                vessel = self.vessel(vessel_id)
            except VesselNotFoundError:
                vessel = None
            if vessel and vessel.first_scan:
                return vessel.first_scan
        return engine.find_first_scan_time(self.host, self.ensure_token(),
                                           max_days_back=max_days_back)

    # -- planning ---------------------------------------------------------

    def make_options(self, options=None, **kwargs):
        """Coerce keyword arguments or a dict into :class:`ExportOptions`."""
        if isinstance(options, ExportOptions):
            base = options
        elif isinstance(options, dict):
            base = ExportOptions.from_dict(options)
        elif options is None:
            base = ExportOptions()
        else:
            raise TypeError(f"Cannot use {type(options).__name__} as export options")

        if "vessel" in kwargs:
            single = kwargs.pop("vessel")
            kwargs.setdefault("vessels", [single] if single is not None else [])
        if kwargs.get("output") is not None:
            kwargs["output"] = str(kwargs["output"])
        if kwargs:
            unknown = set(kwargs) - set(ExportOptions.__dataclass_fields__)
            if unknown:
                raise TypeError(
                    f"Unknown export option(s): {', '.join(sorted(unknown))}")
            base = base.replace(**kwargs)
        if not base.host:
            base = base.replace(host=self.host)
        return base

    def plan(self, options=None, *, progress=None, cancel=None, **kwargs):
        """Work out exactly what a download would fetch, without fetching it."""
        options = self.make_options(options, **kwargs)
        problems = options.validate()
        if problems:
            raise ValueError("; ".join(problems))

        cancel = _as_event(cancel)
        token = self.ensure_token()
        output_dir = options.output_path
        output_dir.mkdir(parents=True, exist_ok=True)
        layout = resolve_layout(options.layout)
        hyperstack, time_stack = layout_flags(layout)

        vessels = [self.vessel(vessel_id) for vessel_id in options.vessels]
        reference_times = {}
        for vessel in vessels:
            reference = vessel.first_scan
            if reference is None and options.start_from == START_FIRST:
                reference = self.first_scan_time(vessel.id)
            if reference is not None:
                reference_times[vessel.id] = reference

        earliest = min(reference_times.values()) if reference_times else None
        latest = max((v.last_scan for v in vessels if v.last_scan), default=None)
        window_start, window_end = options.resolve_window(first_scan=earliest)

        _call(progress, ProgressEvent(
            stage="scanning",
            detail=f"Looking for scans: {options.window_description(earliest)}"))
        # The device lists scans one calendar day at a time, so sweep whole days
        # and then trim to the exact window - that is what makes a time of day
        # in start_from or end_at mean anything.
        scan_times = self._sweep_scan_times(
            options, window_start, window_end, latest=latest,
            progress=progress, cancel=cancel)
        scan_times = options.filter_scan_times(scan_times, window_start, window_end)

        channel_set = options.channel_set
        state = StateStore.for_output(output_dir, scope=options.state_scope)
        state_dict = state.as_dict()

        items = []
        wells_by_vessel = {}
        for vessel in vessels:
            if cancel.is_set():
                break
            selection = options.wells_for(vessel.id)
            selection = wl.normalise_wells(selection, vessel.rows, vessel.cols)
            wells_by_vessel[vessel.id] = selection
            reference = reference_times.get(vessel.id)
            vessel_scans = _scans_from(scan_times, reference)

            def on_item(vessel_id, scan_time, done, total, _v=vessel):
                _call(progress, ProgressEvent(
                    stage="planning",
                    detail=f"Vessel {_v.id}: {done}/{total} scan times checked",
                    done=done, total=total, unit="scan times", vessel_id=_v.id))

            if time_stack:
                items += engine.collect_time_stacks(
                    self.host, token, vessel.id, vessel_scans, output_dir,
                    state=state_dict, wells=selection, channels=channel_set,
                    reference_time=reference, channel_hyperstack=hyperstack,
                    progress_callback=on_item, stop_event=cancel,
                    max_workers=options.workers)
            else:
                items += engine.collect_scan_items_parallel(
                    self.host, token, vessel.id, vessel_scans, output_dir,
                    state=state_dict, wells=selection, channels=channel_set,
                    reference_time=reference, hyperstack=hyperstack,
                    max_workers=options.workers,
                    progress_callback=on_item, stop_event=cancel)

        labels = dict(ch.IMAGE_TYPE_LABELS)
        for vessel in vessels:
            labels.update(vessel.channel_labels)

        plan = ExportPlan(
            output_dir=output_dir, layout=layout, items=items, vessels=vessels,
            scan_times=sorted(scan_times, key=engine.parse_scan_datetime),
            wells_by_vessel=wells_by_vessel, channels=channel_set,
            channel_labels=labels, options=options,
            reference_times=reference_times,
            window=(window_start, window_end))
        plan._state = state
        return plan

    def _sweep_scan_times(self, options, window_start, window_end, latest=None,
                          progress=None, cancel=None):
        """Collect scan times, stopping as soon as a frame count is satisfied."""
        first_day = window_start.date()
        # No scan can exist after the vessel's last one, so do not query the
        # days between then and today - "the last 50 frames" of an experiment
        # that finished in March would otherwise walk back through every day
        # since.
        last_day = window_end.date()
        if latest is not None:
            last_day = min(last_day, latest.date())
        if last_day < first_day:
            last_day = first_day

        wanted = options.start_frames or options.end_frames
        if not wanted:
            return self.scan_times_between(
                first_day, last_day, progress=progress, cancel=cancel)

        def in_window(scan_time):
            try:
                return window_start <= engine.parse_scan_datetime(
                    str(scan_time)) <= window_end
            except (ValueError, TypeError):
                return True

        def enough(scans):
            return sum(1 for t in scans if in_window(t)) >= wanted

        # Counting back from the end means walking the days backwards, because
        # the start date is not known until enough frames have been found.
        return self.scan_times_between(
            first_day, last_day, progress=progress, cancel=cancel,
            reverse=bool(options.start_frames), enough=enough)

    # -- downloading ------------------------------------------------------

    def download(self, plan=None, *, progress=None, cancel=None,
                 write_manifest=None, **kwargs):
        """Run a plan (or build one from options) and write the files."""
        if plan is None or not isinstance(plan, ExportPlan):
            plan = self.plan(plan, progress=progress, cancel=cancel, **kwargs)

        cancel = _as_event(cancel)
        options = plan.options or ExportOptions()
        result = DownloadResult(plan=plan, started_at=datetime.now())

        if plan.is_empty:
            # An empty plan means either "already up to date" or "the user
            # stopped us while we were still listing files" - never conflate
            # the two, a caller retries one and not the other.
            result.cancelled = cancel.is_set()
            result.finished_at = datetime.now()
            _call(progress, ProgressEvent(
                stage="done",
                detail="Cancelled" if result.cancelled else "Nothing new to download"))
            return result

        state = getattr(plan, "_state", None) or StateStore.for_output(
            plan.output_dir, scope=options.state_scope)
        state_dict = state.as_dict()
        token = self.ensure_token()
        by_name = {item["fname"]: item for item in plan.items}
        hyperstack, time_stack = layout_flags(plan.layout)

        # A time stack must hold every frame, so one new scan rebuilds the whole
        # file.  Caching the source payloads keeps that a disk read rather than
        # a fresh download of the entire experiment on every poll.
        cache = cache_for_output(plan.output_dir, mode=options.cache_payloads,
                                 layout=plan.layout)

        def record(fname, size):
            item = by_name.get(fname)
            if item is not None:
                result.files.append(_output_file(item, plan, size))

        def on_error(message):
            result.errors.append(str(message))

        try:
            if time_stack:
                def on_unit(fname, size, done, total):
                    _call(progress, ProgressEvent(
                        stage="downloading", detail=fname, done=done,
                        total=total, unit="source images"))

                def on_file(fname, size, done, total):
                    record(fname, size)
                    _call(progress, ProgressEvent(
                        stage="writing", detail=fname, done=done, total=total,
                        unit="output files"))

                engine.download_collected_time_stack_items(
                    self.host, token, plan.items, state=state_dict,
                    max_workers=options.workers,
                    progress_callback=on_file,
                    unit_progress_callback=on_unit,
                    error_callback=on_error, stop_event=cancel, cache=cache)
            else:
                def on_file(fname, size, done, total):
                    record(fname, size)
                    _call(progress, ProgressEvent(
                        stage="downloading", detail=fname, done=done,
                        total=total, unit="files"))

                engine.download_collected_scan_items(
                    self.host, token, plan.items, state=state_dict,
                    max_workers=options.workers,
                    green_phase=options.green_lut, hyperstack=hyperstack,
                    progress_callback=on_file, error_callback=on_error,
                    stop_event=cancel, cache=cache)
        finally:
            state.flush(force=True)
            result.cancelled = cancel.is_set()
            result.finished_at = datetime.now()
            if cache is not None:
                result.cache = cache
                self.log.debug("payload cache: %s", cache.summary())
                cache.sweep()

        should_write = (options.write_manifest if write_manifest is None
                        else write_manifest)
        if should_write and result.files:
            try:
                result.manifest_path = manifest_mod.write_manifest(
                    result, output_dir=plan.output_dir, host=self.host,
                    options=options)
            except OSError as exc:
                result.errors.append(f"Could not write manifest: {exc}")

        _call(progress, ProgressEvent(stage="done", detail=result.summary()))
        return result

    def fetch(self, options=None, *, progress=None, cancel=None, **kwargs):
        """Plan and download in one call - the shortest path to files on disk."""
        plan = self.plan(options, progress=progress, cancel=cancel, **kwargs)
        return self.download(plan, progress=progress, cancel=cancel)

    # -- watching ---------------------------------------------------------

    def watch(self, options=None, *, on_result=None, on_error=None,
              on_poll=None, progress=None, interval=None, start=True, **kwargs):
        """Poll for new scans and download them as they appear.

        Returns a :class:`~pyincucyte.watch.Watcher` running in its own thread,
        so a pipeline can carry on and call ``stop()`` when it is done.
        """
        from .watch import Watcher

        options = self.make_options(options, **kwargs)
        if interval is not None:
            options = options.replace(interval_minutes=int(interval))
        watcher = Watcher(self, options, on_result=on_result,
                          on_error=on_error, on_poll=on_poll, progress=progress)
        if start:
            watcher.start()
        return watcher

    # -- lifecycle --------------------------------------------------------

    def close(self):
        engine.close_sessions()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __repr__(self):
        state = "connected" if self.is_connected else "not connected"
        return f"<IncucyteClient {self.username or '?'}@{self.host} ({state})>"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _scans_from(scan_times, reference):
    """Drop scan times that predate a vessel's own first scan."""
    if reference is None:
        return list(scan_times)
    kept = []
    for scan_time in scan_times:
        try:
            if engine.parse_scan_datetime(scan_time) >= reference:
                kept.append(scan_time)
        except (ValueError, TypeError):
            kept.append(scan_time)
    return kept


def _output_file(item, plan, size):
    """Turn one finished engine work item into an :class:`OutputFile`."""
    labels = plan.channel_labels or ch.IMAGE_TYPE_LABELS
    vessel = plan.vessel(item.get("vessel_id"))
    row, col = item.get("row", 0), item.get("col", 0)
    site = item.get("site", 0)

    if item.get("frames"):
        scan_times = list(item.get("scan_times", []))
        if item.get("channel_hyperstack"):
            image_types = [c["img_type"] for c in item["frames"][0]["channels"]]
        else:
            image_types = [item["frames"][0]["img_type"]]
    elif item.get("channels"):
        scan_times = [item.get("scan_time")]
        image_types = list(item.get("channel_types")
                           or [c["img_type"] for c in item["channels"]])
    else:
        scan_times = [item.get("scan_time")]
        image_types = [item.get("img_type", 1)]

    elapsed = ""
    reference = plan.reference_times.get(item.get("vessel_id"))
    if reference is not None and scan_times and scan_times[0]:
        try:
            elapsed = engine.format_elapsed(
                engine.parse_scan_datetime(scan_times[0]) - reference)
        except (ValueError, TypeError):
            elapsed = ""

    return OutputFile(
        path=Path(item["fpath"]),
        vessel_id=item.get("vessel_id"),
        vessel_name=vessel.name if vessel else "",
        well=wl.well_name(row, col), row=row, col=col, site=site,
        layout=plan.layout, axes=LAYOUT_AXES[plan.layout],
        channels=[labels.get(t, ch.image_type_label(t)) for t in image_types],
        image_types=list(image_types),
        scan_times=[t for t in scan_times if t],
        elapsed=elapsed, bytes=int(size or 0),
    )


__all__ = ["IncucyteClient"]
