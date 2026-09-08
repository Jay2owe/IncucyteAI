# Preview name did not open a visual preview
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`, `pyincucyte/schedule.py`
**Guard**: `tests/test_gui_threading.py::ActionMeaningTests`, `tests/test_schedule.py::ScheduleCommandTests`

## What went wrong
The main Preview button opened a dry-run list of expected files instead of showing well images. Watch did continuously download new scans, but its name did not clearly distinguish that live process from a download fired periodically by Windows.

## The broken pattern
```python
self.preview_btn = ttk.Button(text="Preview", command=self._preview)  # dry run
self.watch_btn = ttk.Button(text="Watch", command=self._start_watch)
```

## The fix
```python
"Preview images": self._view_images
"Expected download": self._preview
"Sync": self._start_sync
"Schedule...": self._schedule_download
```
The Schedule action registers a one-pass `watch --once` command with Windows Task Scheduler, separate from continuous in-app synchronization.

## Why it matters
Action names are promises. If Preview produces text or Watch silently writes files, a user cannot predict the consequence of clicking a primary control.
