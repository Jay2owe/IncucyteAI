# Shipping PyIncucyte as a desktop app

## What this is about

Two jobs. Make the GUI something a colleague double-clicks without installing
Python, and add a preview that scrubs through a long timestack rather than
showing one image per well.

A packaged app is this code plus a private copy of the Python interpreter in one
folder behind an icon - like a food truck carrying its own generator.

## Recommended: keep Tkinter

The window is a form. Pick a vessel, describe the export, watch a progress bar.
There is no interactive canvas to be slow, the worker thread and queue already
keep it responsive, and `[project.scripts]` plus the packaging tests already
exist.

- **Pro** - already works. Nothing to rewrite.
- **Con** - Tkinter has no image widget, so the timestack scrubber is a few
  hundred lines you write and maintain: frame slider, play/pause, a
  least-recently-used frame cache, contrast controls.
- **Alternative** - open the preview as a separate Qt window in its own process,
  where pyqtgraph gives the slider, playback, draggable contrast histogram and
  ROI for about twenty lines. Costs a second toolkit in the product and roughly
  100 MB. It must be a separate process; two GUI event loops in one interpreter
  deadlock.

Bundle size is not the reason to prefer Tkinter. numpy alone is 35 MB installed
and Pillow 8 MB, so the toolkit is a small share either way. The reason is that
these windows work.

## The timestack preview

The toolkit is the second-order question. Two things decide whether large stacks
are viewable at all:

1. **Cap by temporal stride, not image count.** The device has no thumbnail
   route, so every tile in a preview is a full-size 1.5-3 MB download
   (`pyincucyte/preview.py` header). Previewing 2,000 frames one by one is
   gigabytes over the wire. Sample around 100 evenly-spaced frames instead -
   port `choose_frames` from PyLV200 (`src/pylv200/preview.py:95`) rather than
   writing it twice.
2. **Build one shrunken copy up front.** Downscale during the pass that already
   fetches the data, write a `uint8` proxy beside the export, memory-map it.
   Preview then never touches full-size data and never holds the stack in RAM.

Without these, no toolkit is fast enough. With them, both are.

## Measured, 2026-08-24 — Tkinter wins, decisively

Cost of one frame through `Image.fromarray` -> `ImageTk.PhotoImage`, this machine,
60 conversions per cell after a warm-up:

| Proxy size | 8-bit greyscale | RGB |
|---|---|---|
| 256 px | 0.33 ms | 0.42 ms |
| 384 px | 0.75 ms | 1.00 ms |
| 512 px | 1.30 ms | 1.77 ms |
| 768 px | 2.51 ms | 7.04 ms |
| 1024 px | 6.80 ms | 14.37 ms |

The threshold for comfortable scrubbing was ~15 ms per frame. At a 512 px proxy
the conversion costs under 2 ms, which leaves room for several hundred frames a
second — the slider will never be what makes it feel slow. Even a 1024 px RGB
proxy lands right on the threshold.

**So: build the scrubber in Tkinter.** The separate-Qt-window option is dropped.
It would have bought a free contrast histogram and ROI at the price of a second
toolkit and roughly 100 MB, and the capability argument for it has now failed a
measurement.

Reopen this only if a *dragged* contrast histogram or an ROI drawn on the preview
becomes a requirement — those are features, not frame rate, and they are what
pyqtgraph would still give for about twenty lines.

## Measure before choosing the scrubber toolkit

```python
import numpy as np, time
from PIL import Image, ImageTk
import tkinter as tk
tk.Tk()
a = np.random.randint(0, 255, (512, 512), dtype=np.uint8)   # your proxy size
t = time.perf_counter()
for _ in range(100):
    ImageTk.PhotoImage(Image.fromarray(a))
print((time.perf_counter() - t) / 100 * 1000, "ms/frame")
```

Under about 15 ms per frame means Tkinter scrubs acceptably and the Qt option
drops away.

## Steps

1. Run the benchmark. Record the number here.
2. Add proxy-stack generation to the export pass.
3. Extend preview to sample by temporal stride.
4. Build the scrub window in whichever toolkit step 1 points to.
5. **Done** - `packaging/pyincucyte.spec` freezes it one-folder with no console.
   Build to a local directory, never into this checkout (it is inside Dropbox):

   ```powershell
   $B = "$env:TEMP\pyincucyte-build"
   pyinstaller packaging/pyincucyte.spec --noconfirm --distpath $B\dist --workpath $Build
   ```

   Result: 77 MB, against 260 MB for Circadian Workbench - this package carries
   numpy, Pillow and tifffile but no scipy, pandas or plotly. The frozen `.exe`
   was launched against a scratch `PYINCUCYTE_HOME` and the window opened.

   Still to do: an Inno Setup script. Copy
   `CircadianWorkbench/packaging/circadian-workbench.iss`, drop its WebView2
   check (there is no webview here) and change the names.

   Two things that bit while doing the workbench, both already handled in this
   spec: the entry point cannot be the GUI module itself (PyInstaller runs the
   entry script as `__main__` with no parent package, so relative imports fail -
   hence `packaging/entry.py`), and the exclude list is load-bearing rather than
   tidiness. **`tkinter` must not be excluded here** - it is the interface.

PyIncucyte is the pilot for all four projects: it needs no redesign, so it
proves the packaging recipe before that recipe is spent on anything harder.

## Known cost

Unsigned Windows apps show a "unrecognised publisher" warning on first run.
Removing it needs a code-signing certificate, roughly £200-400/year. Fine to
skip for lab-internal use.
