# Vessels opened oldest first without sort cues
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `tests/test_gui_threading.py`
**Guard**: `VesselSortingTests` in `tests/test_gui_threading.py`

## What went wrong
When PyIncucyte first populated the vessel table, it preserved the device's incoming order instead of putting recent work first. Although each heading could be clicked to sort, there was no visual cue that the headings were interactive or which direction was active.

## The broken pattern
```python
self._sort_column = None       # Startup population was left unsorted.
self._sort_reverse = False
self.vessel_tree.heading(key, text=text, command=sort_command)  # No cue.
```

## The fix
```python
self._sort_column = "last"
self._sort_reverse = True
```

The table now opens with the latest scan first. Every sortable heading shows a bidirectional arrow, while the active heading shows an upward or downward arrow that updates after each click.

## Why it matters
Recent experiments remain immediately visible even when the instrument returns vessels in an arbitrary order, and people can see how to change that order without guessing which headings are clickable.
