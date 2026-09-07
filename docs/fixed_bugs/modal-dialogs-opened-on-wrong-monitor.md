# Modal dialogs opened on the wrong monitor
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/dialogs.py`
**Guard**: `tests/test_gui_threading.py::ExportSettingsCheckpointTests::test_export_dialog_stays_on_the_parents_negative_coordinate_monitor`

## What went wrong
When PyIncucyte was on a monitor to the left of the primary display, opening a download interface placed it on the primary display instead. The centring calculation treated every negative screen coordinate as invalid and replaced it with zero.

## The broken pattern
```python
self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
# A valid coordinate on a left or upper monitor was forced onto the primary screen.
```

## The fix
The dialog now keeps the signed coordinates produced by centring on its parent. Tk accepts positions such as `+-1564+132`, so the modal remains on the same monitor as PyIncucyte.

## Why it matters
Every export action opens a modal dialog. Clamping its position to zero separates the dialog from the application on common multi-monitor layouts and makes it appear that the action did nothing.
