# IncucyteAI — Auto-Download Agent

Automatically download TIF images from the Incucyte live-cell imaging system via its SOAP API. Replaces the manual click-to-download workflow with a script that polls for new scans and saves them to a folder.

## Requirements

- Python 3.10+
- Must be on the Imperial College London network (the Incucyte device is not internet-accessible)

## Install

```bash
pip install zeep requests pillow numpy
```

## Quick Start (GUI)

```bash
python incucyte_gui.py
```

1. Enter the Incucyte host IP and your credentials, click **Login**
2. Select one or more vessels from the list
3. (Optional) Click **Select Wells** to choose specific wells
4. Choose channel filters (Phase, Color 1, Color 2)
5. Set the output folder
6. Set **Start from**: "First scan" to get all historical images, "Today" for new ones only, or a custom date
7. Click **Download Now** for a one-shot download, or **Start Watching** to poll automatically

## Quick Start (CLI)

```bash
# Test connectivity
python incucyte_downloader.py probe --host 129.31.116.189

# Login (saves credentials)
python incucyte_downloader.py login --host 129.31.116.189

# List experiments and vessels
python incucyte_downloader.py experiments

# Download all images from a vessel, starting from its very first scan
python incucyte_downloader.py download -v 38 -o ./images --start-from first

# Download from a specific date
python incucyte_downloader.py download -v 38 -o ./images --start-from 2026-03-01

# Watch mode: poll every 10 minutes for new scans
python incucyte_downloader.py watch -v 38 -o ./images -i 10 --start-from first
```

## Features

- **Parallel downloads** — configurable number of worker threads (default 4)
- **Green LUT for Phase** — Phase contrast images are saved as green-channel RGB TIFs (toggle off with `--no-green-lut`)
- **Start from any date** — download from the vessel's first scan, today, or any custom date
- **Progress dialog** (GUI) — file count, progress bar, speed estimate, ETA, cancel button
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
| `-c`, `--channels` | Channel filter (`phase`, `color1`, `color2`, `all`) |
| `--workers` | Parallel download threads (default: 4) |
| `--no-green-lut` | Disable green LUT for Phase images |

### `watch`
Same as `download`, plus:
| Flag | Description |
|------|-------------|
| `-i`, `--interval` | Poll interval in minutes (default: 10) |

## File Structure

```
IncucyteAI/
  incucyte_downloader.py   CLI tool and core download logic
  incucyte_gui.py          Tkinter GUI
  CLAUDE.md                Agent context (for Claude Code)
  SETUP_PLAN.md            On-site deployment guide
  README.md                This file
  .gitignore               Excludes state files and WSDL
  .tmp/                    Config, state, captures (gitignored)
```

## Setup at Imperial

See [SETUP_PLAN.md](SETUP_PLAN.md) for step-by-step instructions.
