# Scheduled download paths were display-formatted arguments
**Date**: 2026-09-03
**Files changed**: `pyincucyte/options.py`, `pyincucyte/schedule.py`, `pyincucyte/gui/dialogs.py`
**Guard**: `tests/test_options.py::CommandLineMirrorTests::test_paths_with_spaces_are_quoted`, `tests/test_schedule.py::ScheduleCommandTests`

## What went wrong
The first scheduled-download implementation reused a command formatted for display, where a Dropbox output path already contained literal quote marks. Passing that string into another Windows command introduced a second quoting layer, so Task Scheduler rejected the task. Pressing Enter to open the dialog could also submit it immediately.

## The broken pattern
```python
display_command = options.cli_command("watch")  # contains embedded quotes
inner = f"python -m pyincucyte {display_command} --once"
```

## The fix
`ExportOptions.cli_args()` now returns raw argument values, while `cli_command()` alone formats them for display. The scheduler passes the raw list through Windows' own quoting function exactly once. Schedule creation is only available through the dialog's visible Create schedule button.

## Why it matters
Every lab path contains spaces. Double-quoting one can register a broken task—or prevent registration—while making the command look superficially correct.
