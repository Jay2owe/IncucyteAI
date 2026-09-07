# Folded Wells header floated at startup
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`
**Guard**: `tests/test_gui_threading.py::VesselAndWellsPanelTests::test_folded_wells_header_is_pinned_below_the_vessel_list`

## What went wrong
When PyIncucyte opened without a selected vessel, the Wells body correctly stayed folded but its thin header floated halfway down the lower-left area. Summary made the shared grid row tall, while the Wells card used vertical centring inside that row.

## The broken pattern
```python
self.wells_card.grid_configure(sticky="ew")
# East-west stretching leaves a short card vertically centred in a tall row.
```

## The fix
```python
self.wells_card.grid_configure(sticky="new")
```
Adding north alignment pins the folded header directly beneath Vessels. Selecting a vessel still expands the Wells body to fill the available area.

## Why it matters
Wells is the primary selection surface. A detached header makes the startup layout look broken and obscures the intended sequence of selecting a vessel before selecting wells.
