"""Well and plate-layout helpers.

A "well selection" is a set of zero-based ``(row, col)`` tuples, or ``None``
meaning every well on the plate.
"""

import re

from .engine import parse_wells, well_site_name

#: Well count -> (rows, columns) for the plate formats the Incucyte handles.
PLATE_FORMATS = {
    6: (2, 3), 12: (3, 4), 24: (4, 6), 48: (6, 8),
    96: (8, 12), 384: (16, 24),
}

DEFAULT_PLATE = PLATE_FORMATS[96]


def guess_plate_size(vessel_type_name):
    """Parse the plate geometry from a vessel type like 'Sarstedt 24-well'."""
    match = re.search(r"(\d+)\s*-?\s*well", str(vessel_type_name or ""),
                      re.IGNORECASE)
    if match:
        count = int(match.group(1))
        if count in PLATE_FORMATS:
            return PLATE_FORMATS[count]
    return DEFAULT_PLATE


def well_name(row, col):
    """Return the A1-style name of a zero-based ``(row, col)``."""
    return f"{chr(65 + int(row))}{int(col) + 1}"


def all_wells(rows, cols):
    """Return every ``(row, col)`` on a plate of this geometry."""
    return {(r, c) for r in range(rows) for c in range(cols)}


def well_spec(wells):
    """Return a compact, re-parsable spec string for a well selection.

    Runs of consecutive wells in a row collapse to ``A1-A6``; the result round
    trips through :func:`parse_wells`.
    """
    if wells is None:
        return "all"
    if not wells:
        return ""
    parts = []
    by_row = {}
    for row, col in wells:
        by_row.setdefault(row, []).append(col)
    for row in sorted(by_row):
        cols = sorted(by_row[row])
        start = prev = cols[0]
        for col in cols[1:] + [None]:
            if col is not None and col == prev + 1:
                prev = col
                continue
            if start == prev:
                parts.append(well_name(row, start))
            else:
                parts.append(f"{well_name(row, start)}-{well_name(row, prev)}")
            if col is not None:
                start = prev = col
    return ",".join(parts)


def format_wells(wells, max_names=6):
    """Return a short human-readable description of a well selection."""
    if wells is None:
        return "All wells"
    if not wells:
        return "No wells"
    names = [well_name(r, c) for r, c in sorted(wells)]
    if len(names) <= max_names:
        return ", ".join(names)
    return f"{len(names)} wells ({', '.join(names[:4])}, ...)"


def normalise_wells(wells, rows=None, cols=None):
    """Coerce any accepted well input to a set of tuples, or ``None`` for all.

    Accepts ``None``/``"all"``, a spec string, or any iterable of ``(row, col)``
    pairs or ``"A1"`` names.  When the plate geometry is known and every well is
    selected, returns ``None`` so downstream code takes the no-filter path.
    """
    if wells is None:
        return None
    if isinstance(wells, str):
        wells = parse_wells(wells)
        if wells is None:
            return None
    resolved = set()
    for item in wells:
        if isinstance(item, str):
            single = parse_wells(item)
            if single:
                resolved.update(single)
        else:
            row, col = item
            resolved.add((int(row), int(col)))
    if rows and cols and resolved == all_wells(rows, cols):
        return None
    return resolved


__all__ = [
    "PLATE_FORMATS", "DEFAULT_PLATE", "guess_plate_size", "well_name",
    "well_site_name", "all_wells", "well_spec", "format_wells",
    "normalise_wells", "parse_wells",
]
