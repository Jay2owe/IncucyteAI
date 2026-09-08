"""Resume state — which images have already been written.

The original script kept one global JSON file and rewrote it after *every*
image.  For a multi-thousand-image experiment that is both slow (the file is
rewritten O(n) times) and wrong for a pipeline that runs several experiments
side by side, because they all share one ledger.

:class:`StateStore` fixes both: writes are batched behind a dirty flag, and the
ledger lives next to the images it describes.
"""

import json
import os
import threading
import time
from pathlib import Path

from . import engine

#: Filename used for the per-output-folder ledger.
STATE_FILENAME = ".pyincucyte-state.json"

#: Seconds between background flushes while a download is running.
DEFAULT_FLUSH_INTERVAL = 2.0


def _is_inside(target, root):
    """Is the recorded path ``target`` under the resolved folder ``root``?

    Both sides are resolved. Resolving one and not the other is how a carry-over
    silently found nothing: a ledger written through a short 8.3 name, a mapped
    drive or a symlink records a path that names the same file by a different
    spelling, and the answer was "not in this folder" - so an install that had
    already downloaded everything downloaded it all again and said nothing.

    ``relative_to`` rather than a string prefix, because ``out`` prefixes
    ``outsider`` and that would carry another folder's entries into this one.
    """
    try:
        here = Path(target).resolve()
    except OSError:
        here = Path(target)
    try:
        here.relative_to(root)
    except ValueError:
        return False
    return True


class StateStore:
    """A resume ledger of already-downloaded outputs.

    Use :meth:`as_dict` to hand the ledger to the engine download functions;
    they mutate it in place and call back here to persist.
    """

    def __init__(self, path, flush_interval=DEFAULT_FLUSH_INTERVAL):
        self.path = Path(path)
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._dirty = False
        self._last_write = time.monotonic()
        self.data = self._read()

    # -- construction -----------------------------------------------------

    @classmethod
    def for_output(cls, output_dir, scope="auto", **kwargs):
        """Return the ledger for an output folder.

        ``scope="folder"`` keeps it beside the images, ``scope="global"`` uses
        the legacy shared file, and ``scope="auto"`` (the default) uses the
        folder ledger but seeds it once from any global entries that point
        inside this folder, so existing installs do not re-download.
        ``scope="none"`` disables resume tracking entirely.
        """
        if scope == "none":
            return NullStateStore()
        if scope == "global":
            return cls(engine.STATE_FILE, **kwargs)

        output_dir = Path(output_dir)
        store = cls(output_dir / STATE_FILENAME, **kwargs)
        if scope == "auto" and not store.path.exists():
            store._seed_from_global(output_dir)
        return store

    # -- persistence ------------------------------------------------------

    def _read(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("downloaded"), dict):
                    return data
            except (OSError, ValueError):
                pass
        return {"downloaded": {}}

    def _seed_from_global(self, output_dir):
        """Carry over legacy global entries that refer to this output folder."""
        try:
            legacy = engine.load_state()
        except (OSError, ValueError):
            return
        entries = legacy.get("downloaded", {})
        if not entries:
            return
        try:
            root = Path(output_dir).resolve()
        except OSError:
            return
        carried = {}
        for key, info in entries.items():
            target = info.get("file") if isinstance(info, dict) else None
            if target and _is_inside(target, root):
                carried[key] = info
        if carried:
            self.data["downloaded"].update(carried)
            self.flush(force=True)

    def mark_dirty(self):
        """Record that the ledger changed; flush if enough time has passed."""
        with self._lock:
            self._dirty = True
            due = (time.monotonic() - self._last_write) >= self.flush_interval
        if due:
            self.flush()

    def flush(self, force=False):
        """Write the ledger to disk if it changed."""
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {"downloaded": self.data.get("downloaded", {})}
            self._dirty = False
            self._last_write = time.monotonic()
        text = json.dumps(payload, indent=2, default=str)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # A read-only or vanished output folder must not kill a download.
            try:
                tmp.unlink()
            except OSError:
                pass

    # -- engine interop ---------------------------------------------------

    def as_dict(self):
        """Return the mutable dict the engine download functions expect."""
        self.data[engine.STATE_STORE_KEY] = self
        return self.data

    # -- introspection ----------------------------------------------------

    @property
    def entries(self):
        return self.data.get("downloaded", {})

    def __len__(self):
        return len(self.entries)

    def clear(self):
        """Forget every recorded download (forces a full re-fetch)."""
        with self._lock:
            self.data["downloaded"] = {}
            self._dirty = True
        self.flush(force=True)

    def prune_missing(self):
        """Drop ledger entries whose file no longer exists. Returns the count."""
        entries = self.entries
        gone = [key for key, info in entries.items()
                if isinstance(info, dict) and info.get("file")
                and not Path(info["file"]).exists()]
        for key in gone:
            entries.pop(key, None)
        if gone:
            with self._lock:
                self._dirty = True
            self.flush(force=True)
        return len(gone)

    def __repr__(self):
        return f"<StateStore {self.path} entries={len(self)}>"


class NullStateStore(StateStore):
    """A ledger that never reads or writes — every scan is treated as new."""

    def __init__(self):  # noqa: D107 - deliberately skips StateStore.__init__
        self.path = None
        self.flush_interval = 0
        self._lock = threading.Lock()
        self._dirty = False
        self._last_write = 0.0
        self.data = {"downloaded": {}}

    def mark_dirty(self):
        return

    def flush(self, force=False):
        return

    def _seed_from_global(self, output_dir):
        return


__all__ = ["StateStore", "NullStateStore", "STATE_FILENAME"]
