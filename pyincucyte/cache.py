"""A local cache of source image payloads.

Time stacks have an awkward property: a stack must contain every frame, so the
arrival of one new scan invalidates the whole file.  Rebuilt naively, a watch
loop re-downloads the entire experiment on every poll — an hour into a five-day
run that is already thousands of redundant images, and it gets worse every hour.

The cache fixes that by keeping each source payload on disk the first time it is
fetched.  Rebuilding a stack then costs one disk read per frame instead of one
network round trip, and only genuinely new frames touch the instrument.

It is a cache, not a record: deleting it only costs time.
"""

import logging
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("pyincucyte.cache")

#: Folder created inside the output directory.
CACHE_DIRNAME = ".pyincucyte-cache"

#: Cached payloads older than this are swept away (14 days).
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 3600


def payload_key(item):
    """Return the stable identity of one source image payload."""
    scan = str(item.get("scan_time", "")).replace(":", "").replace("-", "")
    scan = scan.replace("+", "_").replace(".", "_")
    return (f"V{item.get('vessel_id')}_r{item.get('row', 0)}"
            f"_c{item.get('col', 0)}_s{item.get('site', 0)}"
            f"_t{item.get('img_type', 1)}_{scan}.tif")


class PayloadCache:
    """Stores raw payload bytes on disk, keyed by well/channel/scan time."""

    def __init__(self, root, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
        self.root = Path(root)
        self.max_age_seconds = max_age_seconds
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._ready = False

    # -- lifecycle --------------------------------------------------------

    def _ensure(self):
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self._ready = True
            except OSError as exc:
                log.debug("payload cache unavailable at %s: %s", self.root, exc)
                return False
        return True

    # -- access -----------------------------------------------------------

    def get(self, item):
        """Return cached bytes for this item, or None."""
        path = self.root / payload_key(item)
        try:
            data = path.read_bytes()
        except OSError:
            with self._lock:
                self.misses += 1
            return None
        if not data:
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return data

    def put(self, item, data):
        """Store payload bytes. Failures are ignored — it is only a cache."""
        if not data or not self._ensure():
            return
        path = self.root / payload_key(item)
        tmp = path.with_suffix(".part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    # -- housekeeping -----------------------------------------------------

    def size_bytes(self):
        try:
            return sum(f.stat().st_size for f in self.root.glob("*.tif"))
        except OSError:
            return 0

    def count(self):
        try:
            return sum(1 for _ in self.root.glob("*.tif"))
        except OSError:
            return 0

    def sweep(self, max_age_seconds=None):
        """Delete cached payloads older than the age limit. Returns the count."""
        limit = self.max_age_seconds if max_age_seconds is None else max_age_seconds
        if not limit or not self.root.is_dir():
            return 0
        cutoff = time.time() - limit
        removed = 0
        for path in self.root.glob("*.tif"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def clear(self):
        """Remove the cache folder entirely."""
        try:
            shutil.rmtree(self.root)
        except OSError:
            pass
        self._ready = False

    @property
    def hit_rate(self):
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def summary(self):
        from .models import human_bytes
        return (f"{self.count():,} cached payloads ({human_bytes(self.size_bytes())}), "
                f"{self.hits:,} hits / {self.misses:,} misses")

    def __repr__(self):
        return f"<PayloadCache {self.root} hits={self.hits} misses={self.misses}>"


def cache_for_output(output_dir, mode="auto", layout="separate",
                     max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Return the cache to use for a download, or None.

    ``"auto"`` caches only for the layouts that rebuild whole files when a scan
    arrives — the ones where the cache actually saves work.
    """
    if mode == "never":
        return None
    if mode == "auto" and "time" not in layout:
        return None
    return PayloadCache(Path(output_dir) / CACHE_DIRNAME,
                        max_age_seconds=max_age_seconds)


__all__ = ["PayloadCache", "cache_for_output", "payload_key", "CACHE_DIRNAME",
           "DEFAULT_MAX_AGE_SECONDS"]
