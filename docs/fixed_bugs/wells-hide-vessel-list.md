# Wells hide the vessel list
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `pyincucyte/gui/widgets.py`
**Guard**: `tests/test_gui_threading.py::VesselAndWellsPanelTests`

## What went wrong
The application opened with the entire well plate visible before a vessel was selected. Its natural height consumed the left column, crushing the vessel list needed to make that selection down to its title bar.

## The broken pattern
```python
left.rowconfigure(0, weight=1)
left.rowconfigure(1, weight=0)  # The full-height wells card always kept this space.
self._build_wells(left)         # Its body was visible even without a vessel.
```

## The fix
The wells card now starts with only its header visible. `_on_vessel_select` expands its body and gives it grid weight after a real vessel is selected; clearing the selection folds it again. The main work area also uses the compact activity-log proportion already established in PyLV200.

## Why it matters
If the plate is allowed to claim space before it has a vessel, users cannot see the vessel rows that unlock every later action.
