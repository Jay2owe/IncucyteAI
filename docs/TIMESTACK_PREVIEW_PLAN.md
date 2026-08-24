# Hybrid pyramid-tile timestack preview

## Implementation status — 24 August 2026

Implemented in `pyincucyte/pyramid.py`, `pyincucyte/timeline.py`, the public
client and command line, and the desktop scrubber. The static well wall now
also prefers reduced tiles. Offline verification covers request JSON, response
wrappers, decoding, 32-item batching, cancellation, missing tiles, bounded
caches, concurrent-request deduplication, local stack/proxy precedence, rapid
selection generations, and the full-TIFF fallback.
The complete offline suite passes (447 tests), and the package wheel builds.

A 2,000-frame synthetic pyramid run loaded 100 anchors (4.85 MB declared
source bytes), fetched frame 1,337 on demand, retained 101 native arrays, and
peaked at 75.2 MiB in the test process. The laboratory device was unreachable
from this network, so the read-only live proof remains an external validation
gate rather than an implementation gap. Run it on-site with:

```text
pyincucyte --json preview-probe -v <VESSEL_ID> -w <WELL>
```

## Decision

Build the timestack scrubber on the Incucyte viewer's reduced-resolution tile
routes. Fetch about 100 evenly spaced low-resolution frames for the first view,
then fetch missing frames and their neighbours as the user scrubs. Keep the
existing full-TIFF preview as a compatibility fallback, not as the normal path.

The current statement that the device has no thumbnail route is too broad. The
`Images/Payloads/GetScanVesselImagePayload` export route has no size parameter,
but the installed Incucyte 2021C and 2022B Rev2 clients use these separate
viewer routes:

```text
Images/phasesourcecompressed
Images/colorsourcecompressed
```

They accept a list of image specifications containing `PyramidLevel` and
`TileId`, and return `CompressedTiffTileData`. `Vessels/GetScanVessel` supplies
the available `ImagePyramidLevels` for the scan.

## End state

The user opens one window, chooses a well and channel, and can:

- see an immediate overview spanning the entire run;
- drag a time slider to any frame, including one not in the initial sample;
- play forward without loading the whole experiment into memory;
- use the existing contrast and processing choices;
- switch wells or channels without reopening the window; and
- fall back to bounded full-image sampling when the private viewer route is not
  supported by the connected device.

The existing static well wall remains the fast answer to "is this the right
plate?". The new scrubber answers "what happened through time in this well?".

## Architecture

```text
GetScanVessel metadata
        |
        +-- ImagePyramidLevels --> choose the smallest useful level
        |
scan times --> 100 even anchors --> batches of reduced tile requests
                                      |
                                      v
                              native low-resolution arrays
                                      |
                   +------------------+------------------+
                   |                                     |
             raw frame cache                       contrast/render cache
             bounded least-                       bounded Tk images
             recently-used cache                         |
                   ^                                     v
                   +-- scrub miss --> requested frame + neighbours

unsupported route --> existing full-TIFF route --> shrink immediately --> same caches
local export/proxy -----------------------------------------------> same caches
```

The provider and the window must be separate. The window asks for frame `N`;
the provider decides whether that frame comes from a pyramid tile, a local
proxy, or the full-image fallback.

## Stable interface

Add a provider contract local to PyIncucyte so transport decisions do not leak
into Tkinter:

```python
class TimelineSource:
    @property
    def frame_count(self) -> int: ...

    def frame_label(self, index: int) -> str: ...
    def get_frame(self, well, site, channel, index, *, cancel=None): ...
    def prefetch(self, well, site, channel, indices, *, cancel=None): ...
    def close(self): ...
```

`get_frame` returns a low-resolution native array before display contrast is
applied. That keeps contrast changes local and prevents another device request.

## Request contract to prove first

The vendor client serialises a scanned-vessel request in this shape:

```json
[
  {
    "ImageTypeCode": 1,
    "Identifier": {
      "ScanVesselIdentifier": {
        "VesselID": 38,
        "ScanDateTime": "2026-08-24T12:34:00",
        "Swell": {"RowZeroBased": 0, "ColumnZeroBased": 1},
        "SwellSite": {"ValueZeroBased": 0}
      },
      "JobID": 0
    },
    "PyramidLevel": 3,
    "TileId": 0,
    "DataRequestType": {"IsScannedVesselRequest": true}
  }
]
```

Do not treat this as stable until a live probe has confirmed:

1. the exact response wrapper returned by phase and fluorescence requests;
2. whether list order is preserved and how a missing image is represented;
3. how `CompressedTiffTileData.Bytes` is encoded and decompressed;
4. that the largest numbered level is the lowest-resolution level;
5. whether the lowest-resolution frame always fits in `TileId = 0`; and
6. the largest safe batch size and cancellation granularity.

The probe must report shapes and byte counts, never save biological pixels or
credentials. Run it on the laboratory network; the device is unreachable from
off-site.

## Implementation stages

### 1. Prove the private tile contract

- Add a read-only probe that starts from a real `VesselScan` and requests one
  phase tile and one fluorescence tile at every advertised level.
- Decode each response and compare dimensions, orientation, and a downsampled
  appearance against the existing full TIFF for the same image.
- Record route availability per client session. An unsupported route is a
  capability result, not a fatal preview error.
- Freeze sanitised response shapes as test fixtures; do not commit image data.

Exit gate: one live scan proves the smallest level and the decoder for phase
and fluorescence, or explicitly selects the full-image fallback.

### 2. Add the pyramid transport without changing export

- Create `pyincucyte/pyramid.py`; keep the private viewer protocol out of the
  proven full-image download path in `engine.py`.
- Parse `ImagePyramidLevels` into level, width, and height records.
- Build phase and colour request specifications and batch them in groups small
  enough to report progress and honour cancellation; begin with 32 and adjust
  from the live probe.
- Decode `CompressedTiffTileData` into correctly typed NumPy arrays.
- Stitch multiple tiles only if the chosen level does not fit in one tile.
- Feature-detect and fall back on an unsupported route, malformed metadata,
  or an undecodable response.

Exit gate: synthetic tests cover request JSON, phase and fluorescence decode,
batch ordering, cancellation, missing tiles, and fallback selection.

### 3. Build the lazy timeline model

- Add `TimelineSource` and a pyramid-backed implementation.
- Port `choose_frames` from PyLV200 as one shared, tested implementation of
  evenly spaced temporal anchors.
- Fetch up to 100 anchors initially; do not permanently discard other scans.
- On a cache miss, fetch the requested frame and prefetch a small symmetric
  neighbourhood. Deduplicate concurrent requests for the same frame.
- Keep a bounded least-recently-used cache of native low-resolution frames and
  a separate rendered cache keyed by contrast and processing recipe.
- Preserve scan labels, elapsed time, well, site, channel, and source byte
  counts so progress and saved previews remain auditable.

Exit gate: a synthetic 2,000-frame run opens after at most 100 initial frame
fetches, frame 1,337 becomes available on demand, and cache size never exceeds
its configured bound.

### 4. Replace the static timestack window with a scrubber

- Keep Tkinter and the existing background-worker/queue boundary.
- Add one image canvas, well and channel selectors, a time slider, previous and
  next buttons, play/pause, contrast controls, and a loading state.
- Coalesce slider motion so dragging does not enqueue every crossed frame.
- Never update Tkinter from a worker thread. Late frame results must carry a
  request generation and be ignored after the user has moved elsewhere.
- Retain the existing static tile wall for multi-well plate recognition.

Exit gate: rapid dragging, playback, channel switching, closing during a
request, and reopening the same run complete without stale images or unbounded
Tk image references.

### 5. Add local proxy reuse

- When a local time stack or a proxy generated during export exists, serve it
  through the same `TimelineSource` contract before contacting the device.
- Store low-resolution native values rather than contrast-stretched display
  bytes where practical, so contrast changes remain free.
- Grow live proxies append-only and keep their index separate from scientific
  TIFF output. A damaged proxy is disposable and must never invalidate data.
- Do not download full images merely to populate a preview proxy.

Exit gate: reopening an exported run performs no device image requests, and a
deleted proxy is rebuilt or bypassed without affecting the export.

### 6. Verify the fallback and publish the real limitation

- Force the tile route to return not found, unauthorised, malformed data, and
  one missing frame; each case must fall back or show a per-frame error without
  losing the window.
- Keep a strict fallback read budget: about 100 evenly spaced full images,
  immediately shrunk and discarded, with later frames fetched lazily.
- Update the docstrings and README to say that the export route is full-size
  while compatible devices also expose an undocumented viewer tile route.
- Measure initial bytes, first-frame latency, random-scrub latency, and peak
  memory on a 2,000-frame synthetic run and one real run.

Exit gate: the pyramid path, local path, and full-image fallback all pass the
same timeline behaviour tests.

## Files expected to change

| Path | Change |
|---|---|
| `pyincucyte/pyramid.py` | New private viewer-route transport and decoder |
| `pyincucyte/timeline.py` | New provider, anchor selection, prefetch, and caches |
| `pyincucyte/preview.py` | Reuse rendering and preserve the existing static preview |
| `pyincucyte/client.py` | Public entry point and capability selection |
| `pyincucyte/gui/preview.py` | Scrubber window and selectors |
| `pyincucyte/gui/app.py` | Launch and worker integration |
| `tests/test_pyramid.py` | Request, decode, capability, and fallback tests |
| `tests/test_timeline.py` | Sampling, cache, prefetch, and cancellation tests |
| `tests/test_preview.py` | Existing preview compatibility |
| `README.md` | Accurate route and memory behaviour |

## Non-negotiable limits

- Previewing must remain read-only on the shared instrument.
- A private route may accelerate a preview but may never become required for
  scientific export.
- A 2,000-frame preview must not hold 2,000 native arrays or Tk images.
- Processing and contrast must be represented in rendered-cache keys.
- Unmixing may require a contributor channel tile; cache and byte accounting
  must include it.
- Preview pixels are for recognition only. Quantification continues to use the
  downloaded TIFF data.
