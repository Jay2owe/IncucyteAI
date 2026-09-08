# Dry run was labelled Expected download

## Symptom

The estimate-only export action was labelled **Expected download**, which did
not read as a clear action beside **Preview images** and **Download**.

## Cause

The original label described the result rather than what the button does.

## Fix

The visible action, menu item, settings checkpoint, tooltip, and summary hint
now use **Preview download**. The operation remains a dry run: it lists and
counts files without fetching images or writing output.

## Regression guard

`ActionMeaningTests.test_visual_and_download_previews_have_distinct_names`
keeps **Preview images** attached to the visual viewer and **Preview download**
attached to the estimate-only planner. `ExportSettingsCheckpointTests` fixes
the matching dialog title, mode label, and confirmation text.
