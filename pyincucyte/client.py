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
from datetime import date, datetime, timedelta
from pathlib import Path

from . import channels as ch
from . import engine
from .cache import cache_for_output
from . import preview as preview_mod
from . import processing
from . import manifest as manifest_mod
from . import wells as wl
from .config import ConfigStore, Credentials
from .errors import NotLoggedInError, VesselNotFoundError
from .models import (
    DownloadResult, ExportPlan, OutputFile, ProgressEvent, Vessel, VesselScan,
    LAYOUT_AXES, layout_flags, resolve_layout,
)
from .options import (
    ExportOptions, START_FIRST, parse_duration, parse_frame_count, parse_moment,
)
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
        self._preview_cache = None
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

    # -- finding a vessel, and looking at it ------------------------------

    def scan_detail(self, vessel_id, scan_time):
        """What one scan actually holds for one vessel: wells, channels, sites.

        Returns ``None`` when the device has no images for that vessel at that
        moment.  Scan times are device-wide rather than per vessel, so this is
        the call that turns "a scan happened" into "this plate was in it".
        """
        try:
            data = engine.api_post(
                self.host, self.ensure_token(), "Vessels/GetScanVessel",
                {"VesselID": int(vessel_id), "DateTime": scan_time,
                 "IncludeDiagnosticMetrics": False})
        except RuntimeError as exc:
            if engine.is_missing_scan_vessel_error(exc):
                return None
            raise
        scan = engine.unpack_values(data.get("Data", {}))
        infos = scan.get("ImageInfos") or []
        if not isinstance(infos, list):
            infos = []
        wells, channels, sites = set(), set(), set()
        coefficients = {}
        for image in infos:
            swell = image.get("Swell") or {}
            site = image.get("SwellSite") or {}
            row = swell.get("RowZeroBased", 0)
            col = swell.get("ColumnZeroBased", 0)
            index = site.get("ValueZeroBased", 0)
            img_type = image.get("ImageType", 1)
            wells.add((row, col))
            channels.add(img_type)
            sites.add(index)
            found = processing.coefficients_from_image(image)
            if found:
                coefficients.setdefault((row, col, index), {})[img_type] = found
        if not wells:
            return None
        return {"wells": wells, "channels": channels, "sites": sites,
                "image_count": len(infos), "coefficients": coefficients,
                "unmixing": processing.Unmixing.from_scan(scan)}

    def find_vessels(self, name=None, *, vessel=None, owner=None, plate=None,
                     channel=None, scanned_only=False, refresh=False):
        """Search the vessel list. Every filter is optional and they all AND.

        ``name`` is a case-insensitive substring of the plate's label (a bare
        number is taken as a vessel id instead), ``plate`` is either a well
        count or part of the plate type, and ``channel`` matches either a
        channel name the experiment uses (``"GFP"``) or its device name
        (``"green"``).  Results come back with the most recently scanned
        vessel first, which is nearly always the one being looked for.
        """
        wanted_ids = None
        if vessel is not None:
            ids = vessel if isinstance(vessel, (list, tuple, set)) else [vessel]
            wanted_ids = {int(one) for one in ids}

        needle = str(name).strip() if name is not None else ""
        if needle.isdigit():
            # find_vessels("38") means the vessel with that id, not a plate
            # whose label happens to contain "38".
            wanted_ids = (wanted_ids or set()) | {int(needle)}
            needle = ""
        needle = needle.lower()
        owner_needle = str(owner).strip().lower() if owner else ""

        matches = []
        for candidate in self.vessels(refresh=refresh):
            if wanted_ids is not None and candidate.id not in wanted_ids:
                continue
            if needle and needle not in (candidate.name or "").lower():
                continue
            if owner_needle and owner_needle not in (candidate.owner or "").lower():
                continue
            if not _plate_matches(candidate, plate):
                continue
            if not _channel_matches(candidate, channel):
                continue
            if scanned_only and candidate.last_scan is None:
                continue
            matches.append(candidate)

        matches.sort(key=lambda v: (v.last_scan or datetime.min, v.id),
                     reverse=True)
        return matches

    def find_scans(self, name=None, *, vessel=None, owner=None, plate=None,
                   channel=None, at=None, since=None, until=None,
                   most_recent=1, scanned_only=True, resolve=True,
                   max_days=14, limit=None, progress=None, cancel=None):
        """Find scans to look at, as :class:`~pyincucyte.models.VesselScan`.

        The output feeds straight into :meth:`preview`, so "which plate is
        this?" is two lines::

            incucyte.find_scans(name="Cry1")[0].preview().show()
            incucyte.find_scans(most_recent=3, plate=24)      # newest three each
            incucyte.find_scans(vessel=38, at="-48h")         # nearest to then

        The vessel filters are those of :meth:`find_vessels`.  ``at`` picks the
        scan closest to a moment; ``since``/``until`` bound the search - each
        takes a date, a datetime, or a signed offset like ``-48h``.  Days are
        walked backwards from each vessel's own last scan (never from today, or
        an old experiment sweeps every day since) and no further back than
        ``max_days``.  With ``resolve`` left on, a scan is only returned once
        the device confirms it holds images for that vessel, which is what
        makes ``most_recent=1`` mean the newest *usable* scan.
        """
        cancel = _as_event(cancel)
        wanted = max(1, int(most_recent))
        since_dt = _moment(since, "since")
        until_dt = _moment(until, "until", end_of_day=True)
        at_dt = _moment(at, "at")

        found = []
        vessels = self.find_vessels(name, vessel=vessel, owner=owner,
                                    plate=plate, channel=channel,
                                    scanned_only=scanned_only)
        for candidate in vessels:
            if cancel.is_set():
                break
            _call(progress, ProgressEvent(
                stage="scanning", detail=f"Vessel {candidate.id}: looking for scans",
                vessel_id=candidate.id))
            found += self._scans_for_vessel(
                candidate, wanted=wanted, since=since_dt, until=until_dt,
                at=at_dt, resolve=resolve, max_days=max_days, cancel=cancel)
            if limit and len(found) >= int(limit):
                break
        return found[:int(limit)] if limit else found

    def _scans_for_vessel(self, vessel, *, wanted=1, since=None, until=None,
                          at=None, resolve=True, max_days=14, cancel=None):
        """Verified scans for one vessel, newest (or nearest to ``at``) first."""
        scans, seen = [], set()
        for scan_time in self._candidate_scan_times(
                vessel, since=since, until=until, at=at, max_days=max_days,
                cancel=cancel):
            if cancel is not None and cancel.is_set():
                break
            if scan_time in seen:
                continue
            seen.add(scan_time)
            detail = self.scan_detail(vessel.id, scan_time) if resolve else {}
            if detail is None:      # the scan happened, but not to this vessel
                continue
            scans.append(VesselScan(
                vessel=vessel, scan_time=scan_time,
                wells=detail.get("wells"), channels=detail.get("channels", set()),
                sites=detail.get("sites", set()),
                image_count=detail.get("image_count", 0),
                coefficients=detail.get("coefficients") or {},
                unmixing=detail.get("unmixing"), client=self))
            if len(scans) >= wanted:
                break
        return scans

    def _candidate_scan_times(self, vessel, *, since=None, until=None, at=None,
                              max_days=14, cancel=None):
        """Yield this vessel's plausible scan times, best guess first.

        Newest first normally; closest to ``at`` when a moment is given.  A day
        costs one API call, so the order is what keeps ``most_recent=1`` down to
        a single call.
        """
        floor = _later(since, vessel.first_scan)
        ceiling = _earlier(until, vessel.last_scan)

        def usable(scan_time):
            try:
                when = engine.parse_scan_datetime(str(scan_time))
            except (ValueError, TypeError):
                return None
            # A scan predating the plate cannot contain it.
            if vessel.first_scan and when < vessel.first_scan:
                return None
            if since and when < since:
                return None
            if until and when > until:
                return None
            return when

        if at is not None:
            for scan_time in self._scans_near(vessel, at, floor, ceiling,
                                              max_days, cancel, usable):
                yield scan_time
            return

        day = (ceiling or datetime.now()).date()
        first_day = floor.date() if floor else None
        for _ in range(max(1, int(max_days))):
            if cancel is not None and cancel.is_set():
                return
            if first_day and day < first_day:
                return
            times = [(usable(t), t) for t in self.scan_times(day)]
            for when, scan_time in sorted(
                    ((w, t) for w, t in times if w is not None), reverse=True):
                yield scan_time
            day -= timedelta(days=1)

    def _scans_near(self, vessel, moment, floor, ceiling, max_days, cancel,
                    usable):
        """Scan times sorted by distance from ``moment``, nearest first."""
        target = moment.date()
        first_day = floor.date() if floor else None
        last_day = ceiling.date() if ceiling else None
        collected = {}
        for offset in _outward(max(1, int(max_days))):
            if cancel is not None and cancel.is_set():
                break
            day = target + timedelta(days=offset)
            if first_day and day < first_day:
                continue
            if last_day and day > last_day:
                continue
            for scan_time in self.scan_times(day):
                when = usable(scan_time)
                if when is not None:
                    collected[scan_time] = abs((when - moment).total_seconds())
            # One day either side of the first hit is enough to be sure the
            # nearest scan is in hand.
            if collected and abs(offset) >= 1:
                break
        for scan_time in sorted(collected, key=collected.get):
            yield scan_time

    def unmixing(self, target=None, **find):
        """The unmixing the Incucyte has saved for a vessel, ready to adjust.

            mixing = incucyte.unmixing("Cry1")   # what is set on the instrument
            mixing["green"] = 0.12               # not enough was coming out
            incucyte.fetch(vessel=38, output="./run-01", unmix=mixing)

        Returns an empty :class:`~pyincucyte.processing.Unmixing` when nobody
        has configured any - which is the usual state, and a perfectly good
        starting point for setting your own.
        """
        scans = self._scans_to_preview(target, **find)
        if not scans:
            raise VesselNotFoundError(
                "No scan matched, so there is no saved unmixing to read.")
        return scans[0].unmixing or processing.Unmixing()

    def preview(self, target=None, *, wells=None, channels=None, site=0,
                size=None, contrast="auto", max_images=None, workers=4,
                cache=True, calibrate=False, background="", unmix="",
                progress=None, cancel=None, **find):
        """Fetch thumbnails of what is in the wells, ready to be shown.

            incucyte.preview("Cry1", wells="A1-B3").show()
            incucyte.preview(scan).save("./thumbs")

        ``target`` takes whatever names a plate: a
        :class:`~pyincucyte.models.VesselScan` (or several) from
        :meth:`find_scans`, a :class:`~pyincucyte.models.Vessel`, a vessel id,
        a name to search for, or nothing at all plus the ``find_scans``
        filters.  Wells and channels the scan does not hold are dropped rather
        than requested.

        ``calibrate``, ``background`` and ``unmix`` are the same options a
        download takes, so a preview can answer "is this the right ratio?"
        before anything is written::

            mixing = incucyte.unmixing(38)
            mixing["green"] = 0.12
            incucyte.preview(38, unmix=mixing).show()

        The device has no thumbnail route, so each tile is a full-size image
        off the wire: ``max_images`` (24 by default) is the brake, and the
        contrast stretch means these pixels are for recognition only.
        """
        cancel = _as_event(cancel)
        size = int(size or preview_mod.DEFAULT_SIZE)
        max_images = (preview_mod.DEFAULT_MAX_IMAGES if max_images is None
                      else max_images)
        recipe = processing.Recipe(
            calibrate=bool(calibrate),
            background="" if background is None else str(background),
            unmix=processing.normalise_unmix(unmix))
        problems = recipe.validate()
        if problems:
            raise ValueError("; ".join(problems))
        scans = self._scans_to_preview(target, progress=progress, cancel=cancel,
                                       **find)
        requests, skipped = preview_mod.build_requests(
            scans, wells=wells, channels=channels, site=site,
            max_images=max_images, recipe=recipe)
        if skipped:
            self.log.info("preview capped at %d images; %d not fetched",
                          max_images, skipped)
        images = preview_mod.fetch_previews(
            self, requests, size=size, contrast=contrast, workers=workers,
            progress=progress, cancel=cancel,
            cache=self.preview_cache if cache else None)
        result = preview_mod.PreviewSet(
            images=images, scans=scans, requested=len(requests), skipped=skipped,
            cancelled=cancel.is_set(), size=size, contrast=contrast,
            recipe=recipe)
        _call(progress, ProgressEvent(stage="done", detail=result.summary()))
        return result

    @property
    def preview_cache(self):
        """Rendered thumbnails held in memory, so a second look is instant."""
        if getattr(self, "_preview_cache", None) is None:
            self._preview_cache = preview_mod.ThumbCache()
        return self._preview_cache

    def _scans_to_preview(self, target, *, progress=None, cancel=None, **find):
        """Coerce anything that names a plate into a list of VesselScan."""
        if target is None:
            return self.find_scans(progress=progress, cancel=cancel, **find)
        if isinstance(target, VesselScan):
            return [target]
        if isinstance(target, Vessel):
            return self.find_scans(vessel=target.id, progress=progress,
                                   cancel=cancel, **find)
        if isinstance(target, (int, str)):
            return self.find_scans(target, progress=progress, cancel=cancel,
                                   **find)
        if isinstance(target, (list, tuple, set)):
            scans = []
            for item in target:
                scans += self._scans_to_preview(item, progress=progress,
                                                cancel=cancel, **find)
            return scans
        raise TypeError(f"Cannot preview {type(target).__name__}")

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
        recipe = options.recipe
        if recipe.is_active:
            _call(progress, ProgressEvent(
                stage="planning", detail=f"Preprocessing: {recipe.describe()}"))
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
                    max_workers=options.workers, recipe=recipe)
            else:
                items += engine.collect_scan_items_parallel(
                    self.host, token, vessel.id, vessel_scans, output_dir,
                    state=state_dict, wells=selection, channels=channel_set,
                    reference_time=reference, hyperstack=hyperstack,
                    max_workers=options.workers,
                    progress_callback=on_item, stop_event=cancel, recipe=recipe)

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
        # Unmixing reads the other channel for every image it corrects, so
        # without a cache a time stack pulls the contributor down once per
        # frame.  That is exactly what the cache exists to stop.
        cache_mode = options.cache_payloads
        if cache_mode == "auto" and options.recipe.wants_unmixing:
            cache_mode = "always"
        cache = cache_for_output(plan.output_dir, mode=cache_mode,
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

def _moment(value, field_name="time", end_of_day=False):
    """Resolve any written form of a moment, or None. Accepts ``-48h``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return parse_moment(value, field_name, end_of_day=end_of_day)
    text = str(value).strip().lower()
    if text in ("", "any", "all"):
        return None
    if text == "now":
        return datetime.now()
    if text == "today":
        return datetime.combine(date.today(), datetime.min.time())
    offset = parse_duration(text)
    if offset is not None:
        return datetime.now() + offset
    if parse_frame_count(text) is not None:
        # A frame has no timestamp until the scans are listed, so it cannot
        # name a moment to search around.
        raise ValueError(
            f"{field_name} needs a time, not a frame count; got {value!r}")
    return parse_moment(value, field_name, end_of_day=end_of_day)


def _later(*moments):
    """The latest of the moments given, ignoring None."""
    real = [m for m in moments if m is not None]
    return max(real) if real else None


def _earlier(*moments):
    real = [m for m in moments if m is not None]
    return min(real) if real else None


def _outward(days):
    """0, -1, +1, -2, +2 ... - day offsets, closest to the target first."""
    yield 0
    for step in range(1, max(1, int(days)) + 1):
        yield -step
        yield step


def _plate_matches(vessel, plate):
    """True when a vessel matches a plate filter: 24, '24-well', 'Sarstedt'."""
    if plate is None:
        return True
    text = str(plate).strip().lower()
    if not text:
        return True
    digits = text.replace("-well", "").replace(" well", "").replace(" ", "")
    if digits.isdigit():
        return vessel.well_count == int(digits)
    return text in (vessel.type_name or "").lower()


def _channel_matches(vessel, channel):
    """True when a vessel uses a channel, named either way round.

    ``"green"`` is the device's name for channel 2; ``"GFP"`` is what this
    experiment calls it.  Both should find the same plate.
    """
    if channel is None:
        return True
    active = vessel.active_channels or set(ch.ALL_CHANNELS)
    if isinstance(channel, int):
        return channel in active
    text = str(channel).strip().lower()
    if not text or text == "all":
        return True
    if text.isdigit():
        return int(text) in active
    try:
        wanted = ch.parse_channels(text)
    except ValueError:
        wanted = None
    if wanted:
        return bool(active & wanted)
    return any(text in str(label).lower()
               for number, label in vessel.channel_labels.items()
               if number in active)


def _item_processed(item):
    """True when this output file's pixels were altered from what was stored."""
    if item.get("processing"):
        return True
    for channel in item.get("channels") or ():
        if channel.get("processing"):
            return True
    for frame in item.get("frames") or ():
        if frame.get("processing"):
            return True
        for channel in frame.get("channels") or ():
            if channel.get("processing"):
                return True
    return False


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
        processed=_item_processed(item),
        processing=(plan.options.processing_description
                    if plan.options is not None else ""),
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
