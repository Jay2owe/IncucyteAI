# PyIncucyteGUI

Automatically download TIF images from the Incucyte live-cell imaging system via its SOAP API. Replaces the manual click-to-download workflow with a script that polls for new scans and saves them to a folder.

## Requirements

- Python 3.10+
- Must be on the Imperial College London network (the Incucyte device is not internet-accessible)
- Windows with the Incucyte desktop client installed is required for first-time password encryption/login

## Install

From PyPI:

```bash
pip install PyIncucyteGUI
```

From this source checkout:

```bash
pip install -e .
```

## Quick Start (GUI)

```bash
py-incucyte-gui
```

1. Enter the Incucyte host IP and your credentials, click **Login**
2. Select one or more vessels from the list
3. (Optional) Click **Select Wells** to choose specific wells
4. Choose channel filters (Phase, Green/Color 1, Red/Color 2)
5. Set the output folder
6. Set **Start from**: "First scan" to get all historical images, "Today" for new ones only, or a custom date
7. Click **Download Now** to review the export dialog, or **Start Watching** to poll automatically

## Quick Start (CLI)

```bash
# Test connectivity
py-incucyte probe --host incucyte.invalid

# Login (saves credentials)
py-incucyte login --host incucyte.invalid

# List experiments and vessels
py-incucyte vessels

# Download all images from a vessel, starting from its very first scan
py-incucyte download -v 38 -o ./images --start-from first

# Download from a specific date
py-incucyte download -v 38 -o ./images --start-from 2026-03-01

# Download Phase + green + red fluorescence channels as ImageJ hyperstacks
py-incucyte download -v 38 -o ./images --channels phase,green,red --hyperstack

# Download each selected well/channel as a time stack across scans
py-incucyte download -v 38 -o ./images --wells D2-D5 --channels phase,green --time-stack

# Download one channel+time hyperstack per selected well
py-incucyte download -v 38 -o ./images --channels phase,green,red --time-stack --hyperstack

# Watch mode: poll every 10 minutes for new scans
py-incucyte watch -v 38 -o ./images -i 10 --start-from first
```

The original script entry points still work from a source checkout:

```bash
python incucyte_gui.py
python incucyte_downloader.py vessels
python -m py_incucyte_gui vessels
```

Compatibility aliases are also installed: `py-incucyte`, `py_incucyte_gui`, and `incucyte-downloader`.

## Features

- **Parallel scan-time checks** - the export-list building step also uses the worker count

- **Parallel downloads** — configurable number of worker threads (default 4)
- **Optional green LUT for Phase** — Phase contrast images are kept as returned by the API by default; use `--green-lut` to save green-channel RGB TIFs
- **Named fluorescence channels** — Incucyte Color 1 is shown as Green and Color 2 as Red; CLI aliases `color1` and `color2` still work
- **ImageJ hyperstack export** — optional one-file-per-well/time TIFF stacks containing all selected channels
- **ImageJ time stack export** — optional one-file-per-well/channel stacks across selected scans; combine with `--hyperstack` for channel+time stacks
- **Pre-flight export dialog** — one-shot downloads show source/calibration details, stack axes, channels, wells, output folder, and filename examples before starting
- **Start from any date** — download from the vessel's first scan, today, or any custom date
- **Progress dialog** (GUI) — file count, progress bar, speed estimate, ETA, cancel button
- **Source-image progress for stacks** — time-stack exports show inner source-image progress plus output-file completion
- **Window title updates** — shows download progress and next-poll countdown when minimized
- **Retry with backoff** — transient network failures retry up to 3 times
- **State tracking** — already-downloaded images are skipped on subsequent runs
- **Well & channel filters** — download only specific wells and/or image channels
- **Elapsed-time filenames** — files named with elapsed time from experiment start (e.g., `VID38_A1_1_024h30m.tif`)
- **Graceful shutdown** — closing the window during watch mode stops cleanly

## CLI Options

### `download`
| Flag | Description |
|------|-------------|
| `-v`, `--vessel` | Vessel ID to download |
| `-o`, `--output` | Output directory |
| `-s`, `--start-from` | `first` (all history), `YYYY-MM-DD`, or omit for today |
| `-d`, `--date` | Single date to download (YYYY-MM-DD) |
| `-w`, `--wells` | Well filter (e.g., `A1`, `A1,B3`, `A1-D4`) |
| `-c`, `--channels` | Channel filter (`phase`, `green`/`color1`, `red`/`color2`, `all`) |
| `--hyperstack` | Save selected channels as one ImageJ hyperstack TIFF per well/site/time |
| `--time-stack` | Save individual wells as ImageJ time stacks across selected scans |
| `--workers` | Parallel download threads (default: 4) |
| `--green-lut` | Apply green LUT to Phase images |
| `--no-green-lut` | Keep Phase images as returned by the API (default) |

### `watch`
Same as `download`, plus:
| Flag | Description |
|------|-------------|
| `-i`, `--interval` | Poll interval in minutes (default: 10) |

## File Structure

```
PyIncucyteGUI/
  pyproject.toml           pip/build metadata and command entry points
  LICENSE                  BSD-3-Clause license
  py_incucyte_gui/         package namespace for python -m py_incucyte_gui
  incucyte_downloader.py   CLI tool and core download logic
  incucyte_gui.py          Tkinter GUI
  README.md                This file
  .gitignore               Excludes state files and WSDL
  .tmp/                    Config, state, captures (gitignored)
```

Runtime config is saved outside the package when installed with pip:

- Windows: `%APPDATA%\PyIncucyteGUI`
- macOS/Linux: `$XDG_CONFIG_HOME/pyincucytegui` or `~/.config/pyincucytegui`
- Override: set `PYINCUCYTEGUI_HOME` to a folder path

Existing source checkouts keep using `.tmp/` if local state already exists there.

## License

PyIncucyteGUI is distributed under the BSD 3-Clause License.

Copyright (c) 2026, Jamie Malcolm. Ownership is personal rather than lab or institute owned.

## Build and Publish

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

## Support Notes

This package is intended for Brancaccio Lab use on machines that can access the Incucyte device. First-time login requires the Incucyte desktop client installation because the downloader uses the vendor encryption library for credentials.
