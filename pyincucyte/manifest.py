"""The manifest: a machine-readable index of everything a download wrote.

Think of it as the packing list that ships with the box.  The next stage of an
automated pipeline (find the SCN, outline, crop, analyse) should read this file
rather than globbing the folder and re-parsing filenames, because it already
knows which well, channel and timepoint every plane belongs to.

Watch mode merges into the same manifest, so it stays a complete index of the
folder however many polls it took to fill.

Each file entry is written in the SCN pipeline's **shared handoff contract**,
defined in ``PySCNSlice/docs/scn-pipeline-plan.md`` and written the same way by
PyLV200, so one reader serves all three acquisition stages:

``path``, ``axes``
    the file, and the axis string its TIFF declares - ``TCYX``, ``TYX``, ``YX``.
``channels``
    one ``{index, name, image_type, source}`` per plane on the channel axis.
    ``index`` counts from **one**, as ImageJ and PySCNSlice's ``scn_channel``
    count, and describes the file rather than the vessel: a well that missed a
    channel would otherwise shift every name after the gap by one.
``frames``
    timepoints the file holds *now*.  A time stack is extended in place on
    every poll, so this is true when read and stale a moment later.
``complete``
    whether it can still gain frames.  Without it a consumer silently skips a
    stack that grew from 40 frames to 400.  ``null`` where nobody can honestly
    say - not the same answer as ``false``, and it must not be treated as one.
``frames_expected``
    how many it should hold once the run finishes, so a well that missed
    timepoints is visible as such rather than as a short recording.
``blank_planes``
    planes allocated but not acquired.  Always 0 here: nothing is written until
    every plane has been downloaded and checked.
``field``
    which well this is, as ``{"kind": "well", "name": "A1"}`` - the
    cross-instrument spelling of what ``well`` says in this package's own terms.
``interval_s``, ``pixel_size_um``
    each ``{"value": ..., "source": ...}``, because a cadence derived from
    timestamps and one read off the instrument are different facts, and
    ``null`` with a reason beats a plausible number.

Two contract fields are deliberately absent.  ``registered`` and ``valid_mask``
belong to whatever aligns the pixels: an instrument does not know whether its
output was later registered, and this one produces no valid-field mask at all.
"""

import csv
import json
import os
from datetime import datetime
from pathlib import Path

#: 2 moved each file entry onto the SCN pipeline's shared handoff contract.
#: ``channels`` holds one record per plane rather than a bare name, ``frames``
#: replaces ``frame_count``, ``interval_s`` replaces ``interval_seconds``, and
#: ``complete``, ``frames_expected``, ``blank_planes``, ``field`` and
#: ``pixel_size_um`` are new.  A reader that wants the old names should key on
#: this number rather than sniffing the shape.
MANIFEST_VERSION = 2

#: Default manifest filename, written into the output folder.
MANIFEST_FILENAME = "pyincucyte-manifest.json"

#: Default flat index filename - the same file list, for pandas/Excel.
INDEX_FILENAME = "pyincucyte-index.csv"

CSV_COLUMNS = [
    "path", "vessel_id", "vessel_name", "well", "row", "col", "site",
    "layout", "axes", "channels", "image_types", "frames",
    "frames_expected", "complete", "blank_planes",
    "first_scan_time", "last_scan_time", "elapsed", "bytes",
    "interval_s", "interval_s_source",
    "pixel_size_um", "pixel_size_um_source",
    "processed", "processing",
]


def _version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return "unknown"


def build_manifest(result, host=None, options=None):
    """Return the manifest dict for a :class:`~pyincucyte.models.DownloadResult`."""
    plan = result.plan
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generated_by": f"pyincucyte {_version()}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "host": host,
        "output_dir": str(plan.output_dir) if plan else None,
        "layout": plan.layout if plan else None,
        "axes": plan.axes if plan else None,
        "vessels": [v.to_dict() for v in (plan.vessels if plan else [])],
        "scan_times": list(plan.scan_times) if plan else [],
        "channel_labels": ({str(k): v for k, v in plan.channel_labels.items()}
                           if plan else {}),
        "options": options.to_dict() if options is not None else None,
        "runs": [{
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "finished_at": result.finished_at.isoformat() if result.finished_at else None,
            "file_count": result.file_count,
            "bytes_total": result.bytes_total,
            "cancelled": result.cancelled,
            "errors": list(result.errors),
        }],
        "files": [_file_entry(f) for f in result.files],
    }
    manifest["stats"] = _stats(manifest["files"])
    return manifest


def _file_entry(output_file):
    data = output_file.to_dict()
    data["first_scan_time"] = (output_file.scan_times[0]
                               if output_file.scan_times else None)
    data["last_scan_time"] = (output_file.scan_times[-1]
                              if output_file.scan_times else None)
    return data


def _entry_channel_names(entry):
    """The display names out of a contract entry's channel records."""
    names = []
    for record in entry.get("channels") or ():
        name = record.get("name") if isinstance(record, dict) else record
        if name:
            names.append(str(name))
    return names


def _split_derived(row, key):
    """Flatten a ``{value, source}`` pair into two CSV columns.

    A spreadsheet cannot hold the pair, and a bare number in a column would
    lose exactly the distinction the pair exists to make, so the provenance
    gets a column of its own rather than being dropped.
    """
    value = row.get(key)
    if isinstance(value, dict):
        row[key] = value.get("value")
        row[f"{key}_source"] = value.get("source")
    return row


def _stats(entries):
    # Three counts rather than one flag, because "still filling" and "nobody
    # said" are different answers and a folder can hold both at once.
    states = [e.get("complete") for e in entries]
    return {
        "file_count": len(entries),
        "bytes_total": sum(e.get("bytes", 0) for e in entries),
        "wells": sorted({e.get("well") for e in entries if e.get("well")}),
        "channels": sorted({name for e in entries
                            for name in _entry_channel_names(e)}),
        "complete_files": sum(1 for s in states if s is True),
        "filling_files": sum(1 for s in states if s is False),
        "unstated_files": sum(1 for s in states if s is None),
    }


def load_manifest(path):
    """Read an existing manifest, or return None if there isn't one."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def merge_manifests(existing, fresh):
    """Merge a new manifest into an existing one, de-duplicating by path."""
    if not existing:
        return fresh
    merged = dict(existing)
    merged.update({k: v for k, v in fresh.items()
                   if k not in ("files", "runs", "stats", "scan_times")})

    by_path = {e.get("path"): e for e in existing.get("files", [])}
    for entry in fresh.get("files", []):
        by_path[entry.get("path")] = entry
    merged["files"] = sorted(by_path.values(), key=lambda e: str(e.get("path")))

    merged["runs"] = (existing.get("runs", []) + fresh.get("runs", []))[-50:]
    scan_times = set(existing.get("scan_times", [])) | set(fresh.get("scan_times", []))
    merged["scan_times"] = sorted(scan_times)
    merged["stats"] = _stats(merged["files"])
    return merged


def write_manifest(result, output_dir=None, path=None, host=None, options=None,
                   merge=True, write_index=True):
    """Write (or update) the manifest for a download. Returns its path."""
    output_dir = Path(output_dir or (result.plan.output_dir if result.plan else "."))
    path = Path(path) if path else output_dir / MANIFEST_FILENAME
    fresh = build_manifest(result, host=host, options=options)
    manifest = merge_manifests(load_manifest(path), fresh) if merge else fresh

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)

    if write_index:
        try:
            write_index_csv(manifest, output_dir / INDEX_FILENAME)
        except OSError:
            pass
    return path


def write_index_csv(manifest, path):
    """Write the manifest's file list as a flat CSV for pandas or Excel."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for entry in manifest.get("files", []):
            row = dict(entry)
            # A spreadsheet column cannot hold the channel records, so it
            # gets the names; the JSON keeps the indices beside them.
            row["channels"] = ";".join(_entry_channel_names(entry))
            row["image_types"] = ";".join(str(c) for c in entry.get("image_types", []))
            _split_derived(row, "interval_s")
            _split_derived(row, "pixel_size_um")
            writer.writerow(row)
    return path


__all__ = [
    "MANIFEST_VERSION", "MANIFEST_FILENAME", "INDEX_FILENAME",
    "build_manifest", "load_manifest", "merge_manifests", "write_manifest",
    "write_index_csv",
]
