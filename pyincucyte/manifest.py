"""The manifest: a machine-readable index of everything a download wrote.

Think of it as the packing list that ships with the box.  The next stage of an
automated pipeline (find the SCN, outline, crop, analyse) should read this file
rather than globbing the folder and re-parsing filenames, because it already
knows which well, channel and timepoint every plane belongs to.

Watch mode merges into the same manifest, so it stays a complete index of the
folder however many polls it took to fill.
"""

import csv
import json
import os
from datetime import datetime
from pathlib import Path

MANIFEST_VERSION = 1

#: Default manifest filename, written into the output folder.
MANIFEST_FILENAME = "pyincucyte-manifest.json"

#: Default flat index filename - the same file list, for pandas/Excel.
INDEX_FILENAME = "pyincucyte-index.csv"

CSV_COLUMNS = [
    "path", "vessel_id", "vessel_name", "well", "row", "col", "site",
    "layout", "axes", "channels", "image_types", "frame_count",
    "first_scan_time", "last_scan_time", "elapsed", "bytes",
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
    data["frame_count"] = output_file.frame_count
    return data


def _stats(entries):
    return {
        "file_count": len(entries),
        "bytes_total": sum(e.get("bytes", 0) for e in entries),
        "wells": sorted({e.get("well") for e in entries if e.get("well")}),
        "channels": sorted({c for e in entries for c in e.get("channels", [])}),
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
            row["channels"] = ";".join(str(c) for c in entry.get("channels", []))
            row["image_types"] = ";".join(str(c) for c in entry.get("image_types", []))
            writer.writerow(row)
    return path


__all__ = [
    "MANIFEST_VERSION", "MANIFEST_FILENAME", "INDEX_FILENAME",
    "build_manifest", "load_manifest", "merge_manifests", "write_manifest",
    "write_index_csv",
]
