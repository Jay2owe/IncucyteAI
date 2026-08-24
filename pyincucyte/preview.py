"""Look before you download: thumbnails of what is really in the wells.

Picking the right plate out of a list of forty is a question about pixels, not
metadata - "is this the one with the confluent monolayer, or the one that
lifted?".  This module answers it without downloading an experiment: it pulls
one image per selected well, shrinks it, stretches the contrast so a 16-bit
fluorescence frame is actually visible, and shows the lot in a small scrollable
window.

    scan = incucyte.find_scans(name="Cry1", most_recent=1)[0]
    scan.preview(wells="A1-B3").show()

Two warnings that matter.  Compatible devices expose undocumented reduced
viewer tiles; older devices fall back to full-size images, so
:data:`DEFAULT_MAX_IMAGES` still bounds this wall.  Both shrinking and contrast
stretching throw information away, so nothing shown here is quantitative:
measure the downloaded TIFF, never the preview.
"""

import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import channels as ch
from . import engine
from . import processing as proc
from . import wells as wl
from .models import ProgressEvent

log = logging.getLogger("pyincucyte.preview")

#: Longest edge of one thumbnail, in pixels.
DEFAULT_SIZE = 240

#: How many images one static preview fetches before it starts refusing.  The
#: cap still protects devices that need the full-size compatibility route.
DEFAULT_MAX_IMAGES = 24

#: Percentile pair used by the automatic contrast stretch.
CONTRAST_PERCENTILES = (0.5, 99.5)

#: How many rendered thumbnails one client keeps in memory.
CACHE_SIZE = 192


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def autoscale(array, contrast="auto", percentiles=CONTRAST_PERCENTILES):
    """Return a uint8 copy of ``array``, stretched so the cells are visible.

    A 16-bit fluorescence frame usually occupies the bottom few percent of its
    range, so shown raw it is a black square.  ``"auto"`` clips at the
    0.5/99.5 percentiles, ``"minmax"`` uses the frame's own range, and ``None``
    only rescales the bit depth.  All three are display transforms.
    """
    import numpy as np

    arr = np.asarray(array)
    if arr.ndim > 2:
        arr = arr[..., 0]
    if arr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    mode = "raw" if contrast in (None, False) else str(contrast).lower()
    if mode == "raw" and arr.dtype == np.uint8:
        return arr.copy()

    data = arr.astype("float32")
    if mode == "raw":
        if np.issubdtype(arr.dtype, np.integer):
            low, high = 0.0, float(np.iinfo(arr.dtype).max)
        else:
            low, high = float(data.min()), float(data.max())
    elif mode == "minmax":
        low, high = float(data.min()), float(data.max())
    else:
        first, last = percentiles
        low, high = (float(v) for v in np.percentile(data, [first, last]))
        if high <= low:                    # a flat or nearly flat frame
            low, high = float(data.min()), float(data.max())

    if high <= low:
        return np.zeros(arr.shape, dtype=np.uint8)
    return np.clip((data - low) * (255.0 / (high - low)), 0, 255).astype(np.uint8)


def _resample():
    """BILINEAR, wherever this Pillow keeps it."""
    from PIL import Image

    return getattr(Image, "Resampling", Image).BILINEAR


def thumbnail(tif_bytes, size=DEFAULT_SIZE, contrast="auto", plan=None,
              fetch_contributor=None):
    """Decode one payload and return a small uint8 array of it.

    With a ``plan`` the same preprocessing a download would apply happens first.
    Calibration is invisible here - it is a linear rescale and the contrast
    stretch undoes it - but unmixing and background removal are exactly what a
    preview is for.
    """
    array = engine._tiff_bytes_to_array(tif_bytes)
    return thumbnail_array(array, size=size, contrast=contrast, plan=plan,
                           fetch_contributor=fetch_contributor)


def thumbnail_array(array, size=DEFAULT_SIZE, contrast="auto", plan=None,
                    fetch_contributor=None):
    """Render a native array as a bounded uint8 recognition thumbnail."""
    from PIL import Image
    import numpy as np

    if plan:
        array = proc.apply(array, plan, fetch_contributor)
    scaled = autoscale(array, contrast)
    image = Image.fromarray(scaled)
    image.thumbnail((int(size), int(size)), _resample())
    return np.asarray(image)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _plan_signature(plan):
    """A hashable summary of a processing plan, for the thumbnail cache key.

    Without it, nudging an unmix ratio would keep handing back the picture made
    with the old one - which is exactly the comparison being made.
    """
    if not plan:
        return ()
    return (bool(plan.get("calibrate")), round(float(plan.get("background") or 0), 6),
            tuple(sorted((term["contributor"], round(float(term["ratio"]), 6),
                          round(float(term.get("sigma") or 0), 6))
                         for term in plan.get("unmix") or ())))


class ThumbCache:
    """A small in-memory store of rendered thumbnails, oldest evicted first.

    Reopening the preview window on the same plate should not pull the images
    down a second time.  The payloads themselves are far too big to hold, so
    what is kept is the finished thumbnail - about 60 KB each.
    """

    def __init__(self, maxsize=CACHE_SIZE):
        self.maxsize = int(maxsize)
        self.hits = 0
        self.misses = 0
        self._items = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(host, request, size, contrast):
        return (host, request["vessel_id"], request["scan_time"],
                request["row"], request["col"], request["site"],
                request["img_type"], int(size), str(contrast),
                _plan_signature(request.get("processing")))

    def get(self, key):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self.hits += 1
                return self._items[key]
        self.misses += 1
        return None

    def put(self, key, array):
        with self._lock:
            self._items[key] = array
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        return len(self._items)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class PreviewImage:
    """One thumbnail: which well and channel it came from, and its pixels."""

    vessel_id: int = 0
    vessel_name: str = ""
    scan_time: str = ""
    well: str = ""
    row: int = 0
    col: int = 0
    site: int = 0
    img_type: int = 1
    channel: str = ""
    elapsed: str = ""
    source_bytes: int = 0
    cached: bool = False
    error: str = ""
    array: object = field(default=None, repr=False)

    @property
    def ok(self):
        return self.array is not None and not self.error

    @property
    def size(self):
        """``(width, height)`` of the thumbnail, or ``(0, 0)``."""
        if self.array is None:
            return (0, 0)
        shape = self.array.shape
        return (int(shape[1]), int(shape[0]))

    @property
    def label(self):
        """e.g. ``"A1 - Phase"`` - what goes under the tile."""
        return f"{self.well} - {self.channel}" if self.channel else self.well

    def image(self):
        """Return the thumbnail as a PIL image."""
        from PIL import Image

        if self.array is None:
            raise ValueError(f"{self.label}: no image was fetched ({self.error})")
        return Image.fromarray(self.array)

    def png(self):
        """Return the thumbnail encoded as PNG bytes."""
        import io

        buffer = io.BytesIO()
        self.image().save(buffer, format="PNG")
        return buffer.getvalue()

    def filename(self, extension=".png"):
        """A stable, sortable filename for this tile."""
        stamp = re.sub(r"[^0-9]", "", str(self.scan_time))[:14]
        token = ch.channel_token(self.channel or self.img_type)
        return f"VID{self.vessel_id}_{self.well}_{token}_{stamp}{extension}"

    def save(self, path):
        """Write the thumbnail to ``path`` as a PNG. Returns the path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.png())
        return path

    def to_dict(self):
        width, height = self.size
        return {
            "vessel_id": self.vessel_id, "vessel_name": self.vessel_name,
            "scan_time": self.scan_time, "well": self.well,
            "row": self.row, "col": self.col, "site": self.site,
            "img_type": self.img_type, "channel": self.channel,
            "elapsed": self.elapsed, "width": width, "height": height,
            "source_bytes": self.source_bytes, "cached": self.cached,
            "error": self.error, "ok": self.ok,
        }


@dataclass
class PreviewSet:
    """The tiles for one look at a plate, plus how to show or save them."""

    images: list = field(default_factory=list)
    scans: list = field(default_factory=list)      # VesselScan objects
    requested: int = 0                             # tiles asked for
    skipped: int = 0                               # dropped by max_images
    cancelled: bool = False
    size: int = DEFAULT_SIZE
    contrast: str = "auto"
    recipe: object = None          # the processing these tiles went through

    # -- shape ------------------------------------------------------------

    @property
    def ok(self):
        return [image for image in self.images if image.ok]

    @property
    def count(self):
        return len(self.ok)

    @property
    def errors(self):
        return [f"{i.label}: {i.error}" for i in self.images if i.error]

    @property
    def is_empty(self):
        return not self.ok

    @property
    def vessels(self):
        seen, vessels = set(), []
        for scan in self.scans:
            if scan.vessel_id not in seen:
                seen.add(scan.vessel_id)
                vessels.append(scan.vessel)
        return vessels

    @property
    def bytes_total(self):
        return sum(i.source_bytes for i in self.images)

    def by_well(self):
        """Group the tiles by well name, in plate order."""
        grouped = {}
        for image in self.images:
            grouped.setdefault(image.well, []).append(image)
        return grouped

    def channels_present(self):
        """Channel display names, in acquisition order."""
        pairs = {i.img_type: i.channel for i in self.images}
        return [pairs[t] for t in sorted(pairs, key=ch.image_type_sort_key)]

    # -- description ------------------------------------------------------

    @property
    def title(self):
        """One line naming what is on screen: the whole point of a preview."""
        if not self.scans:
            return "Preview"
        if len(self.scans) == 1:
            return self.scans[0].label
        vessels = self.vessels
        if len(vessels) == 1:
            return f"{vessels[0].label} - {len(self.scans)} scans"
        return f"{len(vessels)} vessels - {len(self.scans)} scans"

    @property
    def processing_description(self):
        return self.recipe.describe() if self.recipe is not None else ""

    def summary(self):
        wells = len({(i.vessel_id, i.well) for i in self.images})
        parts = [f"{self.count} of {self.requested} images",
                 f"{wells} wells",
                 " + ".join(self.channels_present()) or "no channels"]
        if self.recipe is not None and self.recipe.is_active:
            parts.append(self.recipe.describe())
        if self.skipped:
            parts.append(f"{self.skipped} not fetched (limit reached)")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        if self.cancelled:
            parts.append("cancelled")
        return " - ".join(parts)

    # -- output -----------------------------------------------------------

    def show(self, parent=None, title=None, block=None, dark=None):
        """Pop up the scrollable thumbnail window.

        In a script this blocks until the window is closed; inside a running
        Tk application it returns straight away, so the app stays responsive.
        """
        from .gui.preview import show_preview

        return show_preview(self, parent=parent, title=title, block=block,
                            dark=dark)

    def save(self, directory):
        """Write every tile to ``directory`` as a PNG. Returns the paths."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return [image.save(directory / image.filename()) for image in self.ok]

    def to_dict(self):
        return {
            "title": self.title,
            "summary": self.summary(),
            "requested": self.requested,
            "returned": self.count,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "size": self.size,
            "contrast": self.contrast,
            "processing": (self.recipe.to_dict() if self.recipe is not None
                           else None),
            "bytes_fetched": self.bytes_total,
            "scans": [scan.to_dict() for scan in self.scans],
            "images": [image.to_dict() for image in self.images],
            "errors": list(self.errors),
        }

    def __len__(self):
        return len(self.images)

    def __iter__(self):
        return iter(self.images)

    def __repr__(self):
        return f"<PreviewSet {self.title}: {self.summary()}>"


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _by_vessel(scans):
    """Group scans by vessel, keeping the order they were found in."""
    grouped = {}
    for scan in scans:
        grouped.setdefault(scan.vessel_id, []).append(scan)
    return grouped.values()


def resolve_pairs(recipe, scan):
    """The unmix terms for one scan: the vessel's own, or the ones asked for."""
    if recipe is None or not recipe.wants_unmixing:
        return []
    if recipe.uses_device_unmixing:
        saved = getattr(scan, "unmixing", None)
        return list(saved) if saved else []
    return proc.parse_unmix(recipe.unmix)


def build_requests(scans, wells=None, channels=None, site=0, max_images=None,
                   recipe=None):
    """Turn scans plus a well/channel selection into a flat list of tiles.

    Returns ``(requests, skipped)``.  Wells and channels the scan does not
    actually hold are dropped rather than requested - asking the device for an
    image that was never acquired only earns an error.

    Order is vessel, then well, then scan time, then channel.  Well before time
    is what makes a capped preview of several scans show a couple of wells
    across the whole time course rather than the newest scan's first few wells.

    A ``recipe`` attaches the same processing plan a download would use, so a
    preview can answer "is this unmix ratio right?" before anything is written.
    """
    requests, skipped = [], 0
    wanted_channels = (ch.parse_channels(channels)
                       if isinstance(channels, str) else
                       (set(channels) if channels else None))

    for group in _by_vessel(scans):
        vessel = group[0].vessel
        selection = wl.normalise_wells(wells, vessel.rows, vessel.cols)
        available = set()
        for scan in group:
            available |= (scan.wells or set())
        if selection is None:
            chosen = sorted(available) if available else sorted(
                wl.all_wells(vessel.rows, vessel.cols))
        elif available:
            chosen = sorted(selection & available)
        else:
            chosen = sorted(selection)
        labels = vessel.channel_labels or ch.IMAGE_TYPE_LABELS

        for row, col in chosen:
            for scan in group:
                if scan.wells and (row, col) not in scan.wells:
                    continue
                pairs = resolve_pairs(recipe, scan)
                coefficients = (getattr(scan, "coefficients", None)
                                or {}).get((row, col, int(site)), {})
                present = (scan.channels or vessel.active_channels
                           or set(ch.ALL_CHANNELS))
                types = (present if wanted_channels is None
                         else present & wanted_channels)
                for img_type in sorted(types, key=ch.image_type_sort_key):
                    if max_images is not None and len(requests) >= max_images:
                        skipped += 1
                        continue
                    requests.append({
                        "vessel_id": scan.vessel_id,
                        "vessel_name": vessel.name,
                        "scan_time": scan.scan_time,
                        "row": int(row), "col": int(col), "site": int(site),
                        "img_type": int(img_type),
                        "well": wl.well_name(row, col),
                        "channel": labels.get(img_type,
                                              ch.image_type_label(img_type)),
                        "elapsed": scan.elapsed,
                        "pyramid_levels": getattr(scan, "pyramid_levels", ()),
                        "fname": (f"preview VID{scan.vessel_id} "
                                  f"{wl.well_name(row, col)} type {img_type}"),
                        "processing": proc.plan_for_image(
                            recipe, img_type, coefficients, pairs),
                    })
    return requests, skipped


def _contributor(client, request, img_type):
    """Fetch the other channel of the same image, for an unmix term."""
    other = {k: v for k, v in request.items() if k != "processing"}
    other["img_type"] = img_type
    other["fname"] = f"{request.get('fname', 'preview')}:unmix{img_type}"
    raw, error = engine._fetch_scan_vessel_image_bytes(
        client.host, client.ensure_token(), other)
    if error or not raw:
        raise ValueError(f"could not read channel {img_type} to unmix ({error})")
    return engine._tiff_bytes_to_array(raw)


def _image_from(request, **extra):
    return PreviewImage(
        vessel_id=request["vessel_id"], vessel_name=request.get("vessel_name", ""),
        scan_time=request["scan_time"], well=request["well"],
        row=request["row"], col=request["col"], site=request["site"],
        img_type=request["img_type"], channel=request.get("channel", ""),
        elapsed=request.get("elapsed", ""), **extra)


def fetch_previews(client, requests, *, size=DEFAULT_SIZE, contrast="auto",
                   workers=4, progress=None, cancel=None, cache=None):
    """Fetch and render every request, in order.

    One unreadable image never costs the rest: a failure is recorded on its own
    :class:`PreviewImage` and the others carry on.
    """
    host = client.host
    token = client.ensure_token()
    results = [None] * len(requests)
    total = len(requests)
    counter = {"done": 0}
    lock = threading.Lock()

    def announce(image):
        with lock:
            counter["done"] += 1
            done = counter["done"]
        if progress:
            try:
                progress(ProgressEvent(
                    stage="previewing", detail=image.label, done=done,
                    total=total, unit="preview images",
                    vessel_id=image.vessel_id))
            except Exception:
                log.debug("preview progress callback raised", exc_info=True)

    def work(index):
        request = requests[index]
        if cancel is not None and cancel.is_set():
            return index, None
        key = ThumbCache.key(host, request, size, contrast)
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                image = _image_from(request, array=hit, cached=True)
                announce(image)
                return index, image
        level = None
        array = None
        source_bytes = 0
        levels = []
        if request.get("pyramid_levels"):
            from .pyramid import (PyramidUnavailable, choose_level,
                                  parse_pyramid_levels)

            levels = parse_pyramid_levels(request["pyramid_levels"])
            if levels:
                level = choose_level(levels, max(int(size), 256))
                try:
                    frame = client.preview_transport.fetch_frame(
                        request, level, cancel=cancel)
                    if frame.ok:
                        array = frame.array
                        source_bytes += frame.source_bytes
                except PyramidUnavailable:
                    array = None
        raw = None
        if array is None:
            raw, error = engine._fetch_scan_vessel_image_bytes(host, token, request)
            if error or not raw:
                image = _image_from(request, error=str(error or "no image data"))
                announce(image)
                return index, image
            source_bytes += len(raw)
            try:
                array = engine._tiff_bytes_to_array(raw)
            except Exception as exc:
                image = _image_from(request, error=f"could not render: {exc}",
                                    source_bytes=source_bytes)
                announce(image)
                return index, image

        def contributor(number):
            nonlocal source_bytes
            other = {key: value for key, value in request.items()
                     if key != "processing"}
            other["img_type"] = number
            other["fname"] = f"{request.get('fname', 'preview')}:unmix{number}"
            if level is not None:
                from .pyramid import PyramidUnavailable

                try:
                    frame = client.preview_transport.fetch_frame(
                        other, level, cancel=cancel)
                    if frame.ok:
                        source_bytes += frame.source_bytes
                        return frame.array
                except PyramidUnavailable:
                    pass
            payload, problem = engine._fetch_scan_vessel_image_bytes(
                client.host, client.ensure_token(), other)
            if problem or not payload:
                raise ValueError(
                    f"could not read channel {number} to unmix ({problem})")
            source_bytes += len(payload)
            return engine._tiff_bytes_to_array(payload)

        try:
            array = thumbnail_array(
                array, size=size, contrast=contrast,
                plan=request.get("processing"), fetch_contributor=contributor)
        except Exception as exc:            # a bad payload is not a fatal error
            image = _image_from(request, error=f"could not render: {exc}",
                                source_bytes=source_bytes)
            announce(image)
            return index, image
        if cache is not None:
            cache.put(key, array)
        image = _image_from(request, array=array, source_bytes=source_bytes)
        announce(image)
        return index, image

    if requests:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            for index, image in pool.map(work, range(len(requests))):
                results[index] = image

    return [image for image in results if image is not None]


__all__ = [
    "DEFAULT_SIZE", "DEFAULT_MAX_IMAGES", "CONTRAST_PERCENTILES", "CACHE_SIZE",
    "PreviewImage", "PreviewSet", "ThumbCache",
    "autoscale", "thumbnail", "thumbnail_array", "build_requests",
    "fetch_previews",
]
