# PyIncucyte

Download images off an Incucyte live-cell imaging system — from a desktop app, a
command line, or a Python script. It replaces click-by-click downloading with a
tool that knows what it already has and fetches only what is new.

Three front ends, one engine:

| You want to | Use |
|---|---|
| Pick a plate and grab the images | the desktop app — `pyincucyte gui` |
| Script it, or run it on a schedule | the CLI — `pyincucyte download …` |
| Call it from an analysis pipeline | the API — `from pyincucyte import IncucyteClient` |

## Requirements

- Python 3.10+
- A network route to the instrument (at Imperial the Incucyte is on the internal
  network only — it is not reachable from outside)
- Windows with the Incucyte desktop client installed, **for the login step only**.
  The password is hashed by the vendor's own .NET assembly before it leaves the
  machine, and only that hash is stored. Once a machine has logged in, the saved
  credentials work without it.

## Install

```bash
pip install PyIncucyte             # from PyPI
pip install -e .                   # from this checkout
```

One name throughout: the distribution, the import name and the command are all
`pyincucyte`. The desktop app is a module inside it, so `pyincucyte gui` and
`pyincucyte download ...` are the same program.

Installing does not replace `py-incucyte-gui`, the name this was published
under before 0.3. Uninstall that one if it is still on the machine:
`pip uninstall py-incucyte-gui`.

---

## The desktop app

```bash
pyincucyte gui        # or: pyincucyte-gui, python -m pyincucyte.gui
```

Sign in, pick a vessel, paint the wells you want, choose a layout, press
**Download**. Everything long-running happens on a worker thread, so the window
stays usable and **Cancel** in the status bar always works.

Worth knowing:

- **Preview** (Ctrl+P) counts exactly what would be downloaded and lists the
  filenames, without fetching a single image.
- **Scanned only** keeps just the wells the instrument actually imaged in the
  most recent scan — wells with no data are shown dimmed on the plate.
- **Copy CLI command** (Tools menu, and in the confirm dialog) turns whatever is
  on screen into the equivalent `pyincucyte` command, ready to paste into a
  pipeline script or a scheduled task.
- **Presets** (File menu) save the whole recipe as JSON. The same file works as
  `pyincucyte download --preset my-run.json` and as `ExportOptions.load(...)`.
- The plate picker takes click, drag-to-paint, shift-click for a block, and a
  click on a row letter or column number to flip a whole line.
- Light and dark themes follow Windows, and **View → Toggle dark mode** overrides.
- Settings, window size and per-vessel well selections are remembered. Settings
  saved by earlier versions are migrated on first run.

## The command line

```bash
pyincucyte probe                       # is the instrument reachable?
pyincucyte login                       # saves credentials
pyincucyte vessels                     # what is on the device
pyincucyte plan -v 38 -o ./images --start-from first     # dry run

pyincucyte download -v 38 -o ./images --start-from first
pyincucyte download -v 38 -o ./images --wells D2-D5 --channels phase,green \
                    --layout time_stack
pyincucyte watch    -v 38 -o ./images -i 10 --start-from first
```

Useful anywhere: `--json` for machine-readable output, `--preset FILE` to load a
saved recipe, `--save-preset FILE` to write one, `--dry-run`, `--quiet`.

`pyincucyte manifest ./images` summarises what a folder already contains.

### Time windows

`--start-from` and `--end-at` both take a date, a date *and time*, or a relative
offset. Both ends are inclusive.

| Written as | Means |
|---|---|
| `first` | the experiment's own first scan, at its real time of day |
| `today` / `now` | midnight this morning / this instant |
| `2026-03-05` | start: 00:00 that day. End: the whole of that day |
| `2026-03-05 14:30` | exactly that minute (`T` instead of a space also works) |
| `-48h` (start only) | a rolling window: 48 hours before now |
| `+72h` (end only) | 72 hours after the *start* |
| `-50f` (start only) | the last 50 frames |
| `+100f` (end only) | the first 100 frames from the start |

So the first three days of an experiment that began at 14:30 is
`--start-from first --end-at +72h`, and it correctly excludes that morning's
scans. Time units are `s`, `m`, `h`, `d`, `w`, and the sign is required so an
offset can never be mistaken for a date.

**Frame counts** measure the same window in scan times rather than clock time —
`-50f`, `+100 frames`, `-24 scans` all work. Use them when the *length of the
stack* is what matters, which is usually the case for a video: `--start-from
-100f` gives a 100-frame stack whatever the scan interval was, where `-48h`
gives however many frames happened to fall in two days.

A frame is one scan time, so the count is unaffected by how many wells or
channels you select — five frames of two channels is five timepoints and ten
files. Count from one end or the other, not both.

PyIncucyte walks the days backwards for `-50f` and stops as soon as it has
enough, so asking for the last 50 frames of a three-month run costs a couple of
metadata calls rather than ninety.

The instrument only lists scans one calendar day at a time, so PyIncucyte sweeps
whole days and then trims to your exact window.

### Layouts

`--layout` replaces the old `--hyperstack` / `--time-stack` flag pair, which
still works.

| Layout | Axes | One file per |
|---|---|---|
| `separate` (default) | `YX` | well, channel and scan time |
| `channel_stack` | `CYX` | well and scan time |
| `time_stack` | `TYX` | well and channel |
| `time_channel_stack` | `TCYX` | well |

### Options

| Flag | Description |
|------|-------------|
| `-v`, `--vessel` | Vessel ID — repeat for several |
| `-o`, `--output` | Output folder |
| `-w`, `--wells` | Well filter (`A1`, `A1,B3`, `A1-D4`, `all`) |
| `--vessel-wells ID:WELLS` | Per-vessel well filter (`-f` is the old spelling) |
| `-c`, `--channels` | `phase`, `green`/`color1`, `red`/`color2`, `all` |
| `--layout` | See the table above |
| `-s`, `--start-from` | `first`, `today`, `now`, a date/time, `-48h`, or `-50f` |
| `--end-at` | `now` (default), a date/time, `+72h`, or `+100f` |
| `-d`, `--date` | Shorthand for a single day |
| `-t`, `--scan-time` | Only scan times containing this text |
| `--workers` | Parallel fetches (default 4) |
| `--green-lut` / `--no-green-lut` | Recolour Phase as green RGB (display only) |
| `--state-scope` | `auto` (default), `folder`, `global`, `none` |
| `--cache` | `auto` (default), `always`, `never` — cache source payloads |
| `--no-manifest` | Skip writing the manifest and CSV index |
| `-i`, `--interval` | `watch` only: poll interval in minutes |

---

## The Python API

This is the part an automated pipeline should use.

```python
from pyincucyte import IncucyteClient

with IncucyteClient.from_saved() as incucyte:
    plan = incucyte.plan(
        vessel=38, output="./run-01",
        wells="A1-D6", channels="phase,green",
        layout="time_channel_stack",
        start_from="first", end_at="+72h")     # the first three days

    print(plan.summary())
    # 1 vessel - 24 wells - 118 scan times - One ImageJ TCYX stack per well
    # 24 output files - 5,664 source images - ~15.8 GB

    result = incucyte.download(plan, progress=print)

for image in result.files:
    segment(image.path,
            well=image.well,            # "A1"
            channels=image.channels,     # ["Phase", "GFP"] in stack order
            axes=image.axes,             # "TCYX"
            timepoints=image.scan_times)
```

`incucyte.fetch(...)` plans and downloads in one call. `start_from` and `end_at`
also accept `date` and `datetime` objects directly, and `plan.window` reports the
`(start, end)` the plan actually used — it is recorded in the manifest too.

### What you get back

`DownloadResult` carries `.files` (typed `OutputFile` records), `.paths`,
`.errors`, `.cancelled`, `.bytes_total`, `.duration_seconds` and `.summary()`.
Failures are collected, not raised — one unreadable well never aborts a plate.

Each `OutputFile` knows its vessel, well, row/column, site, channel display
names, device channel numbers, scan times, ImageJ axis order, elapsed-time label
and size.

### The manifest

Every download writes `pyincucyte-manifest.json` and `pyincucyte-index.csv` into
the output folder. **Read these instead of globbing the folder and re-parsing
filenames** — they already say which well, channel and timepoint every file is.
Watch mode merges into the same manifest, so it stays a complete index of the
folder however many polls filled it.

```python
import json, pandas as pd
manifest = json.load(open("run-01/pyincucyte-manifest.json"))
index = pd.read_csv("run-01/pyincucyte-index.csv")
```

### Watching, without blocking

```python
watcher = incucyte.watch(options, on_result=lambda r: analyse(r.paths))
...                                  # your pipeline carries on
watcher.stop(wait=30)
```

`Watcher` also works as a context manager, and `run_forever()` blocks if that is
what you want.

### Presets shared with the GUI

```python
from pyincucyte import ExportOptions

options = ExportOptions.load("nightly.json")     # saved from the GUI
result = incucyte.fetch(options.replace(output="./tonight"))
print(options.cli_command())                     # the equivalent CLI line
```

### Why watch mode stays cheap

A time stack has to contain every frame, so one new scan invalidates the whole
file. Rebuilt naively, every poll would re-download the entire experiment — and
the cost grows with each hour of the run. PyIncucyte keeps the source payloads
in `.pyincucyte-cache/` inside the output folder the first time it fetches them,
so a rebuild is a disk read and only genuinely new frames touch the instrument.

It is on automatically for the time layouts, which are the ones that rebuild.
`--cache always` / `--cache never` (or `cache_payloads=` in `ExportOptions`)
override that; deleting the folder only costs time. `result.cache.summary()`
reports the hit rate.

### Resume state

By default the ledger of what has already been fetched lives in the output
folder (`.pyincucyte-state.json`), so parallel experiments never collide and
moving a folder moves its history with it. `state_scope="global"` restores the
old shared file; `"none"` disables resume entirely.

### Errors

Everything derives from `IncucyteError`, so a pipeline can wrap a whole run in
one `except`: `DeviceUnreachableError`, `AuthenticationError`,
`NotLoggedInError`, `TokenExpiredError`, `ApiError`, `VesselNotFoundError`,
`EncryptionUnavailableError`, `ExportError`.

---

## Layout of this repository

```
PyIncucyte/
  pyincucyte/
    client.py       IncucyteClient - the object a pipeline imports
    options.py      ExportOptions - the recipe shared by GUI, CLI and API
    models.py       Vessel, ExportPlan, DownloadResult, OutputFile
    manifest.py     the JSON manifest and CSV index
    state.py        resume ledger, scoped to the output folder
    cache.py        source-payload cache, so rebuilt stacks do not re-download
    watch.py        Watcher - poll and download in a background thread
    engine.py       wire-level REST and ImageJ TIFF writing
    cli.py          command line
    compat.py       the import names retired in 0.3
    gui/            desktop app: theme.py, widgets.py, dialogs.py, app.py
  tests/            run with: python -m pytest
```

Nothing lives outside the package any more. The loose `incucyte_downloader.py`
and `incucyte_gui.py` modules are gone, and so is the second `py_incucyte_gui`
package; importing `pyincucyte` registers all three as aliases, so this still
resolves - to the very same module object, so monkeypatching behaves as before:

```python
import pyincucyte
from incucyte_downloader import download_scan_images
```

### Where settings live

- Windows: `%APPDATA%\PyIncucyte`
- macOS/Linux: `$XDG_CONFIG_HOME/pyincucyte` or `~/.config/pyincucyte`
- Override with `PYINCUCYTE_HOME`

A folder left over from before the rename (`PyIncucyteGUI` / `pyincucytegui`)
keeps being used if it holds settings, and `PYINCUCYTEGUI_HOME` still works, so
upgrading does not log anybody out.

A source checkout that already has a `.tmp/` folder keeps using it.
`PYINCUCYTE_CLIENT_DIR` overrides where the Incucyte client install is looked for.

## Build and publish

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

## License

BSD 3-Clause. Copyright (c) 2026, Jamie Malcolm. Ownership is personal rather
than lab or institute owned.
