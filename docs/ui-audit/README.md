# User-interface change audit

Each design batch records its requirement, before-and-after images, verification, and whether the corresponding PyLV200 interface should inherit the change.

## Batch 1 — vessel and wells panel state — 2026-09-03

Requirement: show the vessel list at startup; reveal wells only after a vessel is selected.

- Before: [collapsed vessel list](2026-09-03_01_before-panel-state.png)
- After: [selection-gated wells](2026-09-03_02_panel-state-fixed.png)
- Changed: `pyincucyte/gui/app.py`, `pyincucyte/gui/widgets.py`
- Guard: `VesselAndWellsPanelTests` in `tests/test_gui_threading.py`
- PyLV200 parity: adopted its 82% work-area / 18% activity-log split. Its experiment and field-of-view panels are analogous; selection-gating the field grid is logged as a parity candidate, not changed here.

## Batch 2 — visible dark mode — 2026-09-03

Requirement: make dark mode an obvious option instead of a hidden toggle.

- Before: [light interface without a visible theme control](2026-09-03_02_panel-state-fixed.png)
- After: [dark interface with the header option selected](2026-09-03_03_dark-mode.png)
- Changed: `pyincucyte/gui/app.py`
- Guard: `DarkModeTests` in `tests/test_gui_threading.py`
- PyLV200 parity: both applications already shared a saved dark palette and a View-menu command. The visible header checkbox is logged as the matching PyLV200 interface change; PyLV200 is not changed by this batch.

## Batch 3 — actions say what they do — 2026-09-03

Requirement: Preview must show images; the dry run, continuous synchronization, and scheduled download must be distinct actions; Watch becomes Sync.

- Before: [ambiguous Preview and Watch actions](2026-09-03_03_dark-mode.png)
- After: [separated preview, expected download, sync, and schedule actions](2026-09-03_04_action-meanings.png)
- Changed: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`, `pyincucyte/schedule.py`, `packaging/entry.py`
- Guards: `ActionMeaningTests`, `ScheduleCommandTests`, and the frozen-entry scheduled-command test
- PyLV200 parity: PyLV200 already separates Plan, Sync, and Watch internally. This batch records the shared target vocabulary—visual Preview, Expected download, continuous Sync, and Scheduled download—for a later PyLV200 interface pass; PyLV200 is not changed here.

## Batch 4 — explicit token expiry — 2026-09-03

Requirement: identify what the countdown beside the signed-in user measures.

- Before: [ambiguous connection countdown](2026-09-03_04_action-meanings.png)
- After: [explicit token expiry countdown](2026-09-03_05_token-expiry-label.png)
- Changed: `pyincucyte/gui/app.py`
- Guard: `ConnectionCountdownTests` in `tests/test_gui_threading.py`
- PyLV200 parity: no matching change. PyLV200 reports whether a selected microscope is reachable and does not display a login or token countdown.

## Batch 5 — export settings on demand — 2026-09-03

Requirement: remove the permanent Export and Activity panels; let Vessels span the top row, with a wider Wells card left of Summary below; show export settings only after an export action is chosen and require an explicit confirmation.

- Before: [persistent Export and Activity panels](2026-09-03_05_token-expiry-label.png)
- After: [top-row Vessels with Wells left of Summary](2026-09-03_06_export-settings-on-demand.png)
- Changed: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
- Guard: `ExportSettingsCheckpointTests` in `tests/test_gui_threading.py`
- PyLV200 parity: its always-visible recipe and Activity panels are logged as candidates for the same on-demand layout. PyLV200 is not changed by this batch.

## Batch 6 — Python and command-line copies — 2026-09-03

Requirement: offer a runnable Python equivalent as well as the command-line interface command for the current export.

- Before: [command-line interface copy only](2026-09-03_06_export-settings-on-demand.png)
- After: [Python and command-line interface copy actions](2026-09-03_07_python-and-cli-copy.png)
- Changed: `pyincucyte/options.py`, `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
- Guards: `PythonMirrorTests` in `tests/test_options.py` and `ExportCodeCopyTests` in `tests/test_gui_threading.py`
- PyLV200 parity: adding the same Python-first copy pair is logged as a candidate for its export review; PyLV200 is not changed by this batch.

## Batch 7 — action-specific download interfaces — 2026-09-03

Requirement: make the settings window visibly and functionally different for each download action instead of showing live-sync controls everywhere.

All four dialogs remain centred on PyIncucyte when the application is on a secondary monitor.

- Before: [one shared settings interface](2026-09-03_08_before-shared-download-settings.png)
- After — Download: [one-off download settings](2026-09-03_09_download-once-settings.png)
- After — Expected download: [estimate-only settings](2026-09-03_10_expected-download-settings.png)
- After — Sync: [live synchronization settings](2026-09-03_11_sync-settings.png)
- After — Schedule: [Windows scheduled-download settings](2026-09-03_12_scheduled-download-settings.png)
- Changed: `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`
- Guard: `ExportSettingsCheckpointTests.test_each_export_action_has_its_own_relevant_run_controls` in `tests/test_gui_threading.py`
- PyLV200 parity: action-specific export forms are logged as a parity candidate for its Plan, Sync, and Watch routes; PyLV200 is not changed by this batch.

## Batch 8 — aligned startup Wells state — 2026-09-03

Requirement: make the folded Wells section look intentional when PyIncucyte first opens without a selected vessel.

- Before: [Wells floating midway down the lower row](2026-09-03_13_before-startup-wells-alignment.png)
- After: [Wells pinned directly below Vessels](2026-09-03_14_startup-wells-aligned.png)
- Changed: `pyincucyte/gui/app.py`
- Guard: `VesselAndWellsPanelTests.test_folded_wells_header_is_pinned_below_the_vessel_list` in `tests/test_gui_threading.py`
- PyLV200 parity: top-aligning a folded field or well selector beside a taller summary is logged as a layout parity candidate; PyLV200 is not changed by this batch.

## Batch 9 — recent vessels first and visible sorting — 2026-09-03

Requirement: open with the most recently scanned vessels first and make every sortable heading discoverable.

- Before: [identifier order without sort indicators](2026-09-03_15_before-vessel-sort-indicators.png)
- After: [latest scans first with heading arrows](2026-09-03_16_recent-vessels-and-sort-indicators.png)
- Changed: `pyincucyte/gui/app.py`
- Guard: `VesselSortingTests` in `tests/test_gui_threading.py`
- PyLV200 parity: recent-first experiment ordering and visible sort direction are logged as table-behaviour parity candidates; PyLV200 is not changed by this batch.

## Batch 10 — remove Phase green lookup table — 2026-09-03

Requirement: remove the cosmetic green lookup-table option for Phase images from every interface and execution path without removing the real Green fluorescence channel.

- Before: [Phase recolouring control in Download once](2026-09-03_17_before-green-lut-removal.png)
- After: [output options without Phase recolouring](2026-09-03_18_green-lut-removed.png)
- Changed: `pyincucyte/gui/app.py`, `pyincucyte/options.py`, `pyincucyte/cli.py`, `pyincucyte/client.py`, `pyincucyte/engine.py`, `README.md`
- Guard: `RemovedGreenLutTests` in `tests/test_removed_green_lut.py`
- PyLV200 parity: no matching change; PyLV200 does not expose this PyIncucyte-specific Phase recolouring option.

## Batch 11 — named multiple-device chooser — 2026-09-03

Requirement: remember several Incucyte devices and let the user move between them without overwriting or mixing their login tokens.

- Before: [single editable device address](2026-09-03_19_before-multiple-devices.png)
- After: [saved-device chooser in the header](2026-09-03_20_multiple-device-chooser.png)
- Add device: [friendly name and address in Sign in](2026-09-03_21_add-named-device.png)
- Changed: `pyincucyte/config.py`, `pyincucyte/client.py`, `pyincucyte/cli.py`, `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`, `README.md`
- Guard: `DeviceProfileTests` in `tests/test_device_profiles.py` and `DeviceChooserTests` in `tests/test_gui_threading.py`
- PyLV200 parity: named microscope profiles with isolated connection details are logged as a parity candidate; PyLV200 is not changed by this batch.

## Batch 12 — Preview download wording — 2026-09-03

Requirement: rename the estimate-only **Expected download** action to **Preview download** without changing what it does.

- Before: [Expected download action](2026-09-03_22_before-preview-download-name.png)
- After: [Preview download action](2026-09-03_23_preview-download-name.png)
- Changed: `pyincucyte/gui/app.py`, `README.md`
- Guards: `ActionMeaningTests.test_visual_and_download_previews_have_distinct_names` and `ExportSettingsCheckpointTests.test_every_export_action_has_an_explicit_confirmation_label` in `tests/test_gui_threading.py`
- PyLV200 parity: **Preview download** is logged as the preferred dry-run wording for a later PyLV200 interface pass; PyLV200 is not changed by this batch.

## Batch 13 — plate-shaped image viewer — 2026-09-03

Requirement: show one selectable Phase, Green, or Red image per well; preserve the vessel's physical plate grid; and scroll through the scan's Z-stack image positions.

The Incucyte was unreachable during the visual check, so both screenshots render the real `PreviewWindow` with representative 24-well scan metadata and pixels; no instrument data was invented or changed.

- Before: [channels flattened into a flowing thumbnail wall](2026-09-03_24_before-plate-grid-viewer.png)
- After: [Green channel, Z-stack image 2 of 3, in a 4-by-6 plate](2026-09-03_25_plate-grid-channel-z-viewer.png)
- Changed: `pyincucyte/gui/preview.py`, `pyincucyte/gui/app.py`, `README.md`
- Guard: `PreviewPlateViewerTests` in `tests/test_gui_threading.py`
- PyLV200 parity: a single channel selector, stack-position control, and physical field grid are logged as viewer parity candidates; PyLV200 is not changed by this batch.
