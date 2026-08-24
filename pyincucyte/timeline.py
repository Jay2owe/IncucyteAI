"""Bounded, lazy timestack previews.

The timeline keeps scan metadata for every position but only a small set of
pixel arrays.  It prefers reduced viewer tiles, then an existing local stack or
disposable proxy, and finally the established full-TIFF route.  Full images are
shrunk immediately and their encoded bytes are released before entering the
cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import channels as ch
from . import engine
from . import processing as proc
from . import wells as wl
from .models import ProgressEvent
from .pyramid import (
    DEFAULT_MAX_EDGE, PyramidTransport, PyramidUnavailable, choose_level,
    parse_pyramid_levels,
)

log = logging.getLogger("pyincucyte.timeline")

DEFAULT_ANCHORS = 100
DEFAULT_TIMELINE_SIZE = DEFAULT_MAX_EDGE
DEFAULT_PREFETCH_RADIUS = 2
DEFAULT_FRAME_CACHE = 128
DEFAULT_RENDER_CACHE = 96
DEFAULT_FALLBACK_BYTE_BUDGET = 256 * 1024 * 1024


class FrameUnavailable(RuntimeError):
    """One timeline position cannot supply the requested image."""


def choose_frames(times, count, spread=True):
    """Choose evenly spread positions, always including first and newest."""
    times = sorted(times)
    count = max(0, int(count))
    if not times or count == 0:
        return ()
    if count >= len(times):
        return tuple(times)
    if not spread:
        return tuple(times[-count:])
    step = (len(times) - 1) / float(max(count - 1, 1))
    return tuple(sorted({times[int(round(index * step))]
                         for index in range(count)}))


def downsample_native(array, max_edge=DEFAULT_MAX_EDGE):
    """Block-mean an array to a bounded edge without contrast stretching."""
    import numpy as np

    source = np.asarray(array)
    if source.ndim > 2:
        source = source[..., 0]
    if source.ndim != 2:
        raise ValueError(f"preview source must be 2-D, got {source.shape}")
    if source.size == 0:
        return np.zeros((1, 1), dtype=source.dtype or np.uint8)
    edge = max(1, int(max_edge))
    factor = max(1, int((max(source.shape) + edge - 1) // edge))
    if factor == 1:
        return source.copy()

    rows = np.arange(0, source.shape[0], factor)
    cols = np.arange(0, source.shape[1], factor)
    work = source.astype("float64", copy=False)
    summed = np.add.reduceat(np.add.reduceat(work, rows, axis=0), cols, axis=1)
    row_counts = np.minimum(factor, source.shape[0] - rows)
    col_counts = np.minimum(factor, source.shape[1] - cols)
    averaged = summed / (row_counts[:, None] * col_counts[None, :])
    if np.issubdtype(source.dtype, np.integer):
        return np.rint(averaged).astype(source.dtype)
    return averaged.astype(source.dtype, copy=False)


class LRUCache:
    """Small thread-safe least-recently-used cache with observable bounds."""

    def __init__(self, maxsize):
        self.maxsize = max(0, int(maxsize))
        self.hits = 0
        self.misses = 0
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self.hits += 1
                return self._items[key]
            self.misses += 1
            return None

    def put(self, key, value):
        if self.maxsize <= 0:
            return
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        with self._lock:
            return len(self._items)


@dataclass
class FrameInfo:
    """Audit metadata for one well/channel/time position."""

    index: int
    scan_time: str
    label: str
    well: str
    site: int
    channel: int
    source: str = ""
    source_bytes: int = 0
    pyramid_level: int | None = None
    cached: bool = False
    error: str = ""

    def to_dict(self):
        return asdict(self)


def _source_key(vessel_id, scan_time, well, site, channel):
    row, col = _well_tuple(well)
    return (int(vessel_id), str(scan_time), int(row), int(col), int(site),
            int(channel))


def _well_tuple(well):
    if not isinstance(well, str):
        row, col = well
        return int(row), int(col)
    parsed = wl.parse_wells(well)
    if not parsed or len(parsed) != 1:
        raise ValueError(f"expected one well, got {well!r}")
    return next(iter(parsed))


def _proxy_token(key):
    payload = json.dumps(list(key), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ProxyStore:
    """Disposable, per-frame native arrays kept apart from scientific TIFFs."""

    def __init__(self, directory=None):
        self.directory = Path(directory).expanduser() if directory else None
        self._lock = threading.Lock()

    def _paths(self, key):
        token = _proxy_token(key)
        return (self.directory / f"{token}.npy",
                self.directory / f"{token}.json")

    def get(self, key):
        if self.directory is None:
            return None, None
        data_path, info_path = self._paths(key)
        if not data_path.is_file():
            return None, None
        try:
            import numpy as np

            array = np.load(data_path, allow_pickle=False)
            info = (json.loads(info_path.read_text(encoding="utf-8"))
                    if info_path.is_file() else {})
            return array, info
        except Exception:
            log.warning("ignoring damaged preview proxy %s", data_path)
            return None, None

    def put(self, key, array, info=None):
        if self.directory is None:
            return
        data_path, info_path = self._paths(key)
        with self._lock:
            if data_path.is_file():
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            import numpy as np

            handle, temp_name = tempfile.mkstemp(
                prefix=data_path.stem + "-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(handle, "wb") as stream:
                    np.save(stream, array, allow_pickle=False)
                os.replace(temp_name, data_path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            metadata = dict(info or {})
            metadata["key"] = list(key)
            metadata["shape"] = list(getattr(array, "shape", ()))
            metadata["dtype"] = str(getattr(array, "dtype", ""))
            meta_handle, meta_temp = tempfile.mkstemp(
                prefix=info_path.stem + "-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(meta_handle, "w", encoding="utf-8") as stream:
                    json.dump(metadata, stream, indent=2, sort_keys=True)
                os.replace(meta_temp, info_path)
            finally:
                if os.path.exists(meta_temp):
                    os.unlink(meta_temp)


class LocalStack:
    """Lazy pages from an already-exported TIFF stack."""

    def __init__(self, path, frame_count, channels, scan_times=None):
        import tifffile

        self.path = Path(path)
        self.frame_count = int(frame_count)
        self.channels = tuple(channels)
        self.local_frame_count = len(scan_times or ()) or self.frame_count
        self._time_index = {str(value): index
                            for index, value in enumerate(scan_times or ())}
        self._tiff = tifffile.TiffFile(str(self.path))
        self._lock = threading.Lock()

    def get(self, index, channel, scan_time=None):
        pages = self._tiff.pages
        if self._time_index:
            index = self._time_index.get(str(scan_time))
            if index is None:
                return None
        channel_count = max(1, len(self.channels))
        if (len(pages) >= self.local_frame_count * channel_count
                and channel in self.channels):
            page_index = int(index) * channel_count + self.channels.index(channel)
        else:
            page_index = int(index)
        if page_index < 0 or page_index >= len(pages):
            return None
        with self._lock:
            return pages[page_index].asarray()

    def close(self):
        self._tiff.close()


def discover_local_stacks(directory, vessel_id, wells, channels):
    """Map exported timestacks to well/channel keys without opening them."""
    directory = Path(directory).expanduser() if directory else None
    if directory is None or not directory.is_dir():
        return {}
    manifest_path = directory / "pyincucyte-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A device-wide scan list can include moments when this vessel was not
        # present.  Without manifest timestamps, positional reuse could attach
        # a perfectly good local frame to the wrong time and is therefore not
        # automatic.  An explicit local_stack remains available to callers
        # that know their file is positionally aligned.
        return {}
    entries = {}
    for entry in manifest.get("files", []):
        raw_path = Path(str(entry.get("path") or ""))
        entries[raw_path.name.lower()] = entry
    found = {}
    channels = tuple(sorted((int(value) for value in channels),
                            key=ch.image_type_sort_key))
    tokens = {number: ch.channel_tag([number]) for number in channels}
    for well in wells:
        pattern = f"VID{int(vessel_id)}_{well}_*_timestack*.tif"
        for path in sorted(directory.glob(pattern),
                           key=lambda candidate: candidate.stat().st_mtime,
                           reverse=True):
            prefix = f"VID{int(vessel_id)}_{well}_"
            tag = path.stem[len(prefix):].split("_timestack", 1)[0]
            in_file = tuple(number for number in channels
                            if tokens[number] in tag.split("-"))
            if not in_file:
                continue
            entry = entries.get(path.name.lower())
            if not entry or int(entry.get("vessel_id", -1)) != int(vessel_id):
                continue
            if str(entry.get("well") or "") != str(well):
                continue
            scan_times = list(entry.get("scan_times") or ())
            if not scan_times:
                continue
            for number in in_file:
                found.setdefault((well, number), {
                    "path": path, "channels": in_file,
                    "scan_times": scan_times})
    return found


class TimelineSource:
    """Transport-neutral contract used by the timeline window."""

    @property
    def frame_count(self):
        raise NotImplementedError

    def frame_label(self, index):
        raise NotImplementedError

    def get_frame(self, well, site, channel, index, *, cancel=None):
        raise NotImplementedError

    def prefetch(self, well, site, channel, indices, *, cancel=None):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class HybridTimelineSource(TimelineSource):
    """Lazy local/pyramid/full-image source with bounded caches."""

    def __init__(self, client, scans, *, wells=None, channels=None, sites=None,
                 recipe=None, max_edge=DEFAULT_MAX_EDGE,
                 frame_cache=DEFAULT_FRAME_CACHE,
                 render_cache=DEFAULT_RENDER_CACHE,
                 prefetch_radius=DEFAULT_PREFETCH_RADIUS, proxy_dir=None,
                 local_stack=None, transport=None,
                 fallback_byte_budget=DEFAULT_FALLBACK_BYTE_BUDGET,
                 progress=None):
        self.client = client
        self.scans = sorted(list(scans), key=lambda scan: engine.parse_scan_datetime(
            str(scan.scan_time)))
        if not self.scans:
            raise ValueError("a timeline needs at least one scan time")
        self.vessel = self.scans[0].vessel
        self.recipe = recipe or proc.Recipe()
        self.max_edge = max(1, int(max_edge))
        self.prefetch_radius = max(0, int(prefetch_radius))
        self.fallback_byte_budget = max(0, int(fallback_byte_budget))
        self.progress = progress
        self._native = LRUCache(frame_cache)
        self._rendered = LRUCache(render_cache)
        self._inflight = {}
        self._inflight_lock = threading.Lock()
        self._info = {}
        self._details = {}
        self._closed = False
        self._stats_lock = threading.Lock()
        self.network_fetches = 0
        self.bytes_fetched = 0
        self.initial_indices = ()
        self.transport = transport or PyramidTransport(client)
        self.proxy = ProxyStore(proxy_dir)

        selected_wells = (wl.normalise_wells(wells, self.vessel.rows,
                                             self.vessel.cols)
                          if not isinstance(wells, set) else wells)
        observed_wells = set().union(*(scan.wells or set() for scan in self.scans))
        if selected_wells is not None:
            observed_wells = selected_wells
        if not observed_wells:
            observed_wells = wl.all_wells(self.vessel.rows, self.vessel.cols)
        self.available_wells = tuple(wl.well_name(row, col)
                                     for row, col in sorted(observed_wells))

        selected_channels = (ch.parse_channels(channels)
                             if isinstance(channels, str) else
                             (set(channels) if channels else None))
        observed_channels = set().union(*(scan.channels or set()
                                          for scan in self.scans))
        if selected_channels is not None:
            observed_channels = selected_channels
        if not observed_channels:
            observed_channels = set(self.vessel.active_channels or {1})
        self.available_channels = tuple(sorted(observed_channels,
                                               key=ch.image_type_sort_key))
        self.channel_labels = {
            number: self.vessel.channel_labels.get(number,
                                                   ch.image_type_label(number))
            for number in self.available_channels
        }

        observed_sites = set(sites or ()) or set().union(
            *(scan.sites or set() for scan in self.scans)) or {0}
        self.available_sites = tuple(sorted(int(site) for site in observed_sites))
        self._local_by_key = {}
        if isinstance(local_stack, dict):
            opened = {}
            for key, spec in local_stack.items():
                path = Path(spec["path"] if isinstance(spec, dict) else spec)
                stack_channels = tuple(
                    spec.get("channels", (key[1],))
                    if isinstance(spec, dict) else (key[1],))
                stack_times = tuple(spec.get("scan_times", ())) if isinstance(
                    spec, dict) else ()
                signature = (path, stack_channels, stack_times)
                stack = opened.get(signature)
                if stack is None:
                    stack = opened[signature] = LocalStack(
                        path, self.frame_count, stack_channels, stack_times)
                self._local_by_key[(str(key[0]), int(key[1]))] = stack
        elif local_stack:
            stack = LocalStack(local_stack, self.frame_count,
                               self.available_channels)
            for name in self.available_wells:
                for number in self.available_channels:
                    self._local_by_key[(name, number)] = stack

    def _local_frame(self, index, well, channel):
        stack = self._local_by_key.get((str(well), int(channel)))
        return (stack.get(index, int(channel), self.scans[index].scan_time)
                if stack is not None else None)

    @property
    def frame_count(self):
        return len(self.scans)

    @property
    def cache_size(self):
        return len(self._native)

    @property
    def render_cache_size(self):
        return len(self._rendered)

    def frame_label(self, index):
        scan = self.scans[self._validate_index(index)]
        stamp = str(scan.scan_time)[:16].replace("T", " ")
        return f"{stamp}  +{scan.elapsed}".rstrip(" +")

    def _validate_index(self, index):
        index = int(index)
        if not 0 <= index < self.frame_count:
            raise IndexError(f"timeline frame {index} outside 0..{self.frame_count - 1}")
        return index

    def _normalise_well(self, well):
        row, col = _well_tuple(well)
        return wl.well_name(row, col), row, col

    def _detail(self, index):
        scan = self.scans[index]
        if scan.scan_time in self._details:
            detail = self._details[scan.scan_time]
        elif (scan.wells and (getattr(scan, "pyramid_levels", None)
                              or scan.coefficients or scan.image_count)):
            detail = {
                "wells": scan.wells, "channels": scan.channels,
                "sites": scan.sites, "coefficients": scan.coefficients,
                "unmixing": scan.unmixing,
                "pyramid_levels": getattr(scan, "pyramid_levels", ()),
            }
            self._details[scan.scan_time] = detail
        else:
            detail = self.client.scan_detail(scan.vessel_id, scan.scan_time)
            if detail is None:
                raise FrameUnavailable("this vessel was not imaged at that scan time")
            self._details[scan.scan_time] = detail
            scan.wells = detail.get("wells")
            scan.channels = detail.get("channels", set())
            scan.sites = detail.get("sites", set())
            scan.coefficients = detail.get("coefficients") or {}
            scan.unmixing = detail.get("unmixing")
            if hasattr(scan, "pyramid_levels"):
                scan.pyramid_levels = detail.get("pyramid_levels") or []
        return detail

    def _request(self, well, site, channel, index):
        index = self._validate_index(index)
        name, row, col = self._normalise_well(well)
        channel = int(channel)
        detail = self._detail(index)
        if detail.get("wells") and (row, col) not in detail["wells"]:
            raise FrameUnavailable(f"{name} was not acquired at this time")
        if detail.get("channels") and channel not in detail["channels"]:
            raise FrameUnavailable(
                f"{self.channel_labels.get(channel, channel)} was not acquired at this time")
        if detail.get("sites") and int(site) not in detail["sites"]:
            raise FrameUnavailable(f"site {int(site) + 1} was not acquired at this time")
        scan = self.scans[index]
        return {
            "vessel_id": scan.vessel_id,
            "vessel_name": scan.vessel.name,
            "scan_time": scan.scan_time,
            "row": row, "col": col, "well": name, "site": int(site),
            "img_type": channel,
            "channel": self.channel_labels.get(channel,
                                                ch.image_type_label(channel)),
            "elapsed": scan.elapsed,
            "fname": (f"timeline VID{scan.vessel_id} {name} "
                      f"type {channel} frame {index}"),
        }, detail

    def _record(self, key, index, request, **values):
        info = FrameInfo(
            index=index, scan_time=request["scan_time"],
            label=self.frame_label(index), well=request["well"],
            site=request["site"], channel=request["img_type"], **values)
        self._info[key] = info
        return info

    def frame_info(self, well, site, channel, index):
        scan = self.scans[self._validate_index(index)]
        key = _source_key(scan.vessel_id, scan.scan_time, well, site, channel)
        return self._info.get(key)

    def _network_account(self, source_bytes):
        with self._stats_lock:
            self.network_fetches += 1
            self.bytes_fetched += max(0, int(source_bytes))

    def _fetch_native(self, well, site, channel, index, cancel=None):
        if self._closed:
            raise FrameUnavailable("timeline source is closed")
        request, detail = self._request(well, site, channel, index)
        key = _source_key(request["vessel_id"], request["scan_time"],
                          request["well"], site, channel)

        proxy_array, proxy_info = self.proxy.get(key)
        if proxy_array is not None:
            self._record(key, index, request, source="proxy", source_bytes=0,
                         pyramid_level=(proxy_info or {}).get("pyramid_level"))
            return proxy_array

        local = self._local_frame(index, request["well"], int(channel))
        if local is not None:
            array = downsample_native(local, self.max_edge)
            self._record(key, index, request, source="local stack",
                         source_bytes=0)
            self.proxy.put(key, array, {"source": "local stack",
                                        "source_bytes": 0})
            return array

        levels = parse_pyramid_levels(detail.get("pyramid_levels") or detail)
        if levels:
            try:
                level = choose_level(levels, self.max_edge)
                frame = self.transport.fetch_frame(request, level, cancel=cancel)
                if frame.ok:
                    array = downsample_native(frame.array, self.max_edge)
                    self._network_account(frame.source_bytes)
                    self._record(key, index, request, source="pyramid tile",
                                 source_bytes=frame.source_bytes,
                                 pyramid_level=frame.level)
                    self.proxy.put(key, array, {
                        "source": "pyramid tile",
                        "source_bytes": frame.source_bytes,
                        "pyramid_level": frame.level,
                    })
                    return array
                log.debug("pyramid frame unavailable: %s", frame.error)
            except PyramidUnavailable as exc:
                log.debug("pyramid preview unavailable; using full TIFF: %s", exc)

        if cancel is not None and cancel.is_set():
            raise FrameUnavailable("frame request was cancelled")
        raw, error = engine._fetch_scan_vessel_image_bytes(
            self.client.host, self.client.ensure_token(), request)
        if error or not raw:
            self._record(key, index, request, source="full TIFF fallback",
                         error=str(error or "no image data"))
            raise FrameUnavailable(str(error or "no image data"))
        source_bytes = len(raw)
        try:
            array = downsample_native(engine._tiff_bytes_to_array(raw), self.max_edge)
        finally:
            del raw
        self._network_account(source_bytes)
        self._record(key, index, request, source="full TIFF fallback",
                     source_bytes=source_bytes)
        self.proxy.put(key, array, {"source": "full TIFF fallback",
                                    "source_bytes": source_bytes})
        return array

    def _native_frame(self, well, site, channel, index, cancel=None):
        scan = self.scans[self._validate_index(index)]
        key = _source_key(scan.vessel_id, scan.scan_time, well, site, channel)
        cached = self._native.get(key)
        if cached is not None:
            info = self._info.get(key)
            if info is not None:
                info.cached = True
            return cached

        with self._inflight_lock:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        if not owner:
            return future.result()

        try:
            array = self._fetch_native(well, site, channel, index, cancel=cancel)
            self._native.put(key, array)
            future.set_result(array)
            return array
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)

    def _processing_plan(self, detail, well, site, channel):
        _name, row, col = self._normalise_well(well)
        coefficients = (detail.get("coefficients") or {}).get(
            (row, col, int(site)), {})
        if self.recipe.uses_device_unmixing:
            pairs = list(detail.get("unmixing") or ())
        else:
            pairs = proc.parse_unmix(self.recipe.unmix)
        return proc.plan_for_image(self.recipe, int(channel), coefficients, pairs)

    def get_frame(self, well, site, channel, index, *, cancel=None):
        """Return native low-resolution values before display contrast."""
        index = self._validate_index(index)
        array = self._native_frame(well, site, channel, index, cancel=cancel)
        detail = self._detail(index)
        plan = self._processing_plan(detail, well, site, channel)
        if not plan:
            return array
        return proc.apply(
            array, plan,
            lambda contributor: self._native_frame(
                well, site, contributor, index, cancel=cancel))

    def render_frame(self, well, site, channel, index, *, size=DEFAULT_MAX_EDGE,
                     contrast="auto", cancel=None):
        """Return a cached uint8 display frame for the timeline window."""
        from .preview import _plan_signature, autoscale

        detail = self._detail(self._validate_index(index))
        plan = self._processing_plan(detail, well, site, channel)
        scan = self.scans[index]
        source = _source_key(scan.vessel_id, scan.scan_time, well, site, channel)
        key = source + (int(size), str(contrast), _plan_signature(plan))
        cached = self._rendered.get(key)
        if cached is not None:
            return cached
        scaled = autoscale(self.get_frame(well, site, channel, index,
                                          cancel=cancel), contrast)
        rendered = downsample_native(scaled, int(size))
        self._rendered.put(key, rendered)
        return rendered

    def prefetch(self, well, site, channel, indices, *, cancel=None):
        """Fetch uncached positions, stopping promptly when cancelled."""
        loaded = []
        for index in dict.fromkeys(int(value) for value in indices):
            if cancel is not None and cancel.is_set():
                break
            if not 0 <= index < self.frame_count:
                continue
            try:
                self._native_frame(well, site, channel, index, cancel=cancel)
                loaded.append(index)
            except FrameUnavailable:
                continue
        return tuple(loaded)

    def neighbours(self, index):
        index = self._validate_index(index)
        return tuple(value for distance in range(1, self.prefetch_radius + 1)
                     for value in (index - distance, index + distance)
                     if 0 <= value < self.frame_count)

    def prime(self, well=None, site=None, channel=None, *,
              count=DEFAULT_ANCHORS, cancel=None):
        """Load a bounded, evenly spread first view of the run."""
        well = well or self.available_wells[0]
        site = self.available_sites[0] if site is None else int(site)
        channel = self.available_channels[0] if channel is None else int(channel)
        anchors = choose_frames(range(self.frame_count), min(int(count),
                                                             self.frame_count))
        loaded = []
        initial_bytes = self.bytes_fetched

        def announce(index):
            loaded.append(index)
            info = self.frame_info(well, site, channel, index)
            if self.progress:
                try:
                    self.progress(ProgressEvent(
                        stage="previewing", detail=self.frame_label(index),
                        done=len(loaded), total=len(anchors),
                        unit="timeline frames", vessel_id=self.vessel.id))
                except Exception:
                    log.debug("timeline progress callback raised", exc_info=True)

        # Read native/proxy/local hits first.  Device misses with advertised
        # levels are then sent through the transport together, which enforces
        # its 32-spec batch boundary while preserving scan order.
        pending = []
        fallback = []
        for index in anchors:
            if cancel is not None and cancel.is_set():
                break
            scan = self.scans[index]
            key = _source_key(scan.vessel_id, scan.scan_time, well, site, channel)
            cached = self._native.get(key)
            if cached is not None:
                announce(index)
                continue
            proxy_array, proxy_info = self.proxy.get(key)
            if proxy_array is not None:
                request, _detail = self._request(well, site, channel, index)
                self._native.put(key, proxy_array)
                self._record(key, index, request, source="proxy", source_bytes=0,
                             pyramid_level=(proxy_info or {}).get(
                                 "pyramid_level"))
                announce(index)
                continue
            local = self._local_frame(index, well, channel)
            if local is not None:
                request, _detail = self._request(well, site, channel, index)
                array = downsample_native(local, self.max_edge)
                self._native.put(key, array)
                self._record(key, index, request, source="local stack",
                             source_bytes=0)
                self.proxy.put(key, array, {"source": "local stack",
                                            "source_bytes": 0})
                announce(index)
                continue
            try:
                request, detail = self._request(well, site, channel, index)
            except FrameUnavailable:
                continue
            levels = parse_pyramid_levels(detail.get("pyramid_levels") or detail)
            if levels:
                pending.append((index, key, request,
                                choose_level(levels, self.max_edge)))
            else:
                fallback.append(index)

        if pending and not (cancel is not None and cancel.is_set()):
            try:
                frames = self.transport.fetch_many(
                    [item[2] for item in pending], [item[3] for item in pending],
                    cancel=cancel)
            except PyramidUnavailable:
                fallback.extend(item[0] for item in pending)
            else:
                for (index, key, request, _level), frame in zip(pending, frames):
                    if not frame.ok:
                        fallback.append(index)
                        continue
                    array = downsample_native(frame.array, self.max_edge)
                    self._native.put(key, array)
                    self._network_account(frame.source_bytes)
                    self._record(key, index, request, source="pyramid tile",
                                 source_bytes=frame.source_bytes,
                                 pyramid_level=frame.level)
                    self.proxy.put(key, array, {
                        "source": "pyramid tile",
                        "source_bytes": frame.source_bytes,
                        "pyramid_level": frame.level,
                    })
                    announce(index)

        for index in sorted(set(fallback)):
            if cancel is not None and cancel.is_set():
                break
            before = self.bytes_fetched
            try:
                self._native_frame(well, site, channel, index, cancel=cancel)
            except FrameUnavailable:
                continue
            announce(index)
            info = self.frame_info(well, site, channel, index)
            # The reduced route is normally kilobytes.  Only the full-image
            # compatibility path is stopped by the stricter source-byte brake.
            if (info is not None and info.source == "full TIFF fallback"
                    and self.fallback_byte_budget
                    and self.bytes_fetched - initial_bytes
                    >= self.fallback_byte_budget
                    and self.bytes_fetched > before):
                break
        self.initial_indices = tuple(sorted(set(loaded)))
        return self.initial_indices

    def close(self):
        self._closed = True
        self._native.clear()
        self._rendered.clear()
        for stack in set(self._local_by_key.values()):
            stack.close()
        self._local_by_key.clear()


@dataclass
class TimelinePreview:
    """Public handle for a lazy timeline and its display defaults."""

    source: HybridTimelineSource
    well: str = ""
    site: int = 0
    channel: int = 1
    size: int = DEFAULT_MAX_EDGE
    contrast: str = "auto"
    errors: list = field(default_factory=list)

    @property
    def title(self):
        return f"Vessel {self.source.vessel.label} - time course"

    @property
    def frame_count(self):
        return self.source.frame_count

    @property
    def initial_indices(self):
        return self.source.initial_indices

    def summary(self):
        return (f"{self.frame_count:,} frames - {len(self.initial_indices):,} "
                f"initial previews - {self.source.bytes_fetched:,} bytes fetched")

    def show(self, parent=None, title=None, block=None, dark=None):
        from .gui.preview import show_timeline

        return show_timeline(self, parent=parent, title=title, block=block,
                             dark=dark)

    def save(self, directory, indices=None):
        """Save selected display frames; defaults to the initial anchors."""
        from PIL import Image

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        indices = tuple(self.initial_indices if indices is None else indices)
        paths = []
        for index in indices:
            array = self.source.render_frame(
                self.well, self.site, self.channel, index,
                size=self.size, contrast=self.contrast)
            stamp = re.sub(r"[^0-9]", "", self.source.scans[index].scan_time)[:14]
            path = directory / (
                f"VID{self.source.vessel.id}_{self.well}_"
                f"{ch.channel_token(self.channel)}_{stamp}.png")
            Image.fromarray(array).save(path, format="PNG")
            paths.append(path)
        return paths

    def to_dict(self):
        return {
            "title": self.title, "summary": self.summary(),
            "frame_count": self.frame_count,
            "initial_indices": list(self.initial_indices),
            "well": self.well, "site": self.site, "channel": self.channel,
            "channel_name": self.source.channel_labels.get(self.channel, ""),
            "bytes_fetched": self.source.bytes_fetched,
            "network_fetches": self.source.network_fetches,
            "frame_cache_size": self.source.cache_size,
            "render_cache_size": self.source.render_cache_size,
            "capabilities": self.source.transport.capabilities,
            "errors": list(self.errors),
        }

    def close(self):
        self.source.close()


__all__ = [
    "DEFAULT_ANCHORS", "DEFAULT_TIMELINE_SIZE", "DEFAULT_PREFETCH_RADIUS",
    "DEFAULT_FRAME_CACHE",
    "DEFAULT_RENDER_CACHE", "DEFAULT_FALLBACK_BYTE_BUDGET", "FrameUnavailable",
    "FrameInfo", "LRUCache", "ProxyStore", "TimelineSource",
    "HybridTimelineSource", "TimelinePreview", "choose_frames",
    "downsample_native", "discover_local_stacks",
]
