# Export could only be copied as a command-line invocation
**Date**: 2026-09-03
**Files changed**: `pyincucyte/options.py`, `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
**Guard**: `tests/test_options.py::PythonMirrorTests`, `tests/test_gui_threading.py::ExportCodeCopyTests`

## What went wrong
The desktop interface could copy the current export only as a command-line interface command. Python users had to translate that command back into `ExportOptions` and `IncucyteClient` calls by hand, even though the application already had the complete recipe.

## The broken pattern
```python
self.clipboard_append(self.options.cli_command())
# No equivalent Python workflow was offered.
```

## The fix
`ExportOptions.python_code()` now generates a runnable, minimal Python program. The Tools menu and final export review place Copy Python beside Copy CLI command, and both use the same current recipe.

## Why it matters
Python is the normal integration surface for analysis pipelines. Keeping both generated forms prevents the graphical interface, Python application programming interface, and command-line interface from drifting apart.

## Verification
- `tests/test_options.py::PythonMirrorTests::test_python_code_reconstructs_the_exact_download_recipe`
- `tests/test_options.py::PythonMirrorTests::test_watch_python_code_runs_a_continuous_watcher`
- `tests/test_gui_threading.py::ExportCodeCopyTests::test_plan_dialog_copies_python_and_cli_from_the_same_options`
- Full suite: 516 tests and 4 subtests passed.
