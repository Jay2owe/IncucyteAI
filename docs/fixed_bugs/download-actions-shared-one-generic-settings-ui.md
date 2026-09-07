# Download actions shared one generic settings interface
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
**Guard**: `tests/test_gui_threading.py::ExportSettingsCheckpointTests::test_each_export_action_has_its_own_relevant_run_controls`

## What went wrong
Choosing Download, Expected download, Sync, or Schedule opened essentially the same settings interface. A one-off download therefore showed live controls such as “Sync every” and “New frames,” while an estimate looked like an operation that might write files.

## The broken pattern
```python
dialog = ExportSettingsDialog(..., self._build_export_form, ...)
# Every action built the same complete form; only its heading changed.
```

## The fix
Each action now declares its own identity, explanation, confirmation label, window size, and visible settings sections. The form builder hides irrelevant execution controls: Download shows one-pass speed, Expected download shows none, Sync shows polling and batching, and Schedule shows batching while leaving cadence to Windows.

## Why it matters
The selected action must make its consequences obvious before confirmation. Restoring the shared form would again make estimates, one-off downloads, live synchronization, and unattended schedules easy to confuse.
