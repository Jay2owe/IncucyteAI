# Phase recolouring remained in export paths
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`, `pyincucyte/options.py`, `pyincucyte/cli.py`, `pyincucyte/client.py`, `pyincucyte/engine.py`, `README.md`
**Guard**: `RemovedGreenLutTests` in `tests/test_removed_green_lut.py`

## What went wrong
Every export dialog offered an option to recolour grayscale Phase images into green red-green-blue images. The same setting was present in saved recipes, generated commands, command-line flags, client calls, and the download engine even though it changed pixel structure for a cosmetic display effect.

## The broken pattern
```python
green_lut: bool = False
parser.add_argument("--green-lut", ...)
img_bytes = apply_green_lut(img_bytes)  # Cosmetic processing in data export.
```

## The fix
The checkbox, export field, generated command flag, command-line arguments, client bridge, and engine transformation were removed. Old preset keys are treated as unknown fields and ignored when loading.

## Why it matters
Phase downloads now consistently preserve the image structure returned by the instrument. The Green fluorescence channel remains available as a separate acquisition channel and cannot be confused with cosmetic Phase recolouring.
