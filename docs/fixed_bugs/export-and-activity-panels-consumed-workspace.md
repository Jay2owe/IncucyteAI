# Export and Activity panels consumed the selection workspace
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
**Guard**: `tests/test_gui_threading.py::ExportSettingsCheckpointTests`

## What went wrong
The main window permanently displayed a large export form and Activity log. These secondary controls reduced the space available for choosing vessels and wells, even though export settings need review only when starting an export and activity history is not part of plate selection.

## The broken pattern
```python
self._build_export(right)  # occupied the main workspace at all times
self._build_log(lower)     # reserved the lower part of every screen
```

## The fix
The main workspace now builds only Vessels, Wells, and Summary. Download, Expected download, continuous Sync, and Scheduled download each open the same modal export-settings form with an action-specific confirmation button.

## Why it matters
Restoring either permanent panel would shrink the vessel and well selectors again. Bypassing the modal checkpoint would also allow an export action to reuse settings without the requested review and confirmation.
