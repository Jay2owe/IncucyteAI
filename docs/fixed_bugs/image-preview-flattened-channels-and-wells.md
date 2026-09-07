# Image preview flattened channels and wells
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/preview.py`, `pyincucyte/gui/app.py`
**Guard**: `PreviewPlateViewerTests` in `tests/test_gui_threading.py`

## What went wrong
Opening **Preview images** put every well/channel combination into one flowing
thumbnail wall. A 24-well plate therefore stopped looking like a 4-by-6 plate,
channels appeared as adjacent duplicate wells, and there was no way to move
through the scan's per-well image positions.

## The broken pattern

```python
for image in preview.images:
    tiles.append(make_tile(image))
for index, tile in enumerate(tiles):
    tile.grid(row=index // columns, column=index % columns)  # flattens identity
```

## The fix

```python
visible = preview_plane_images(images, selected_channel, selected_site)
tile.grid(row=image.row + 1, column=image.col + 1)
```

The viewer now shows one selected Phase, Green, or Red acquisition channel,
preserves each well's physical row and column, and loads another Z-stack image
position only when the selector requests it.

## Why it matters

Adjacent tiles can otherwise be mistaken for adjacent wells when they are
actually different channels from the same well. Preserving the plate geometry
makes visual plate checks reliable before an export.
