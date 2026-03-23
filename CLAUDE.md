# IncucyteAI — Auto-Download Agent

Automate downloading TIF images from the Incucyte live-cell imaging system.
See `SETUP_PLAN.md` for the step-by-step setup guide to run at Imperial.

## Goal

Jamie manually clicks "download" in the Incucyte GUI every time a new scan
image appears. This project replaces that with a Python script that polls
the Incucyte SOAP API and auto-downloads TIFs to a configured folder.

## Architecture

- **Incucyte device** at `129.31.116.189` (Imperial College London network)
- **SOAP web services** at `http://<ip>/IncuCyteWS/Standard.asmx`
- **FastInitialConnection** at `http://<ip>/IncuCyteWS/FastInitialConnection.asmx`
- **Namespace**: `http://essenbio.com/IncuCyteWS/`
- **Auth**: Username + password via `ValidateUserLogin`
- **Client app**: Incucyte 2024B (.NET 4.8) at `C:\Program Files\Incucyte 2024B\`

## Network Requirements

The Incucyte device is ONLY accessible from the Imperial College network.
Port 80 must be reachable — it's blocked from outside Imperial.
Jamie has a remote desktop at Imperial but it doesn't have Python installed yet.

## Current Status

**Done:**
- Extracted ~100 SOAP method names from .NET DLLs (Essen.IncuCyte.Client.dll)
- Built `incucyte_downloader.py` with probe, login, experiments, download, watch commands
- Built `incucyte_gui.py` — full tkinter GUI with login, vessel selection, well picker, channel filter, download/watch controls
- Parallel downloads via ThreadPoolExecutor (configurable worker count)
- Green LUT for Phase images (converts grayscale to green-channel RGB)
- Progress dialog with file count, progress bar, speed, ETA, cancel button
- "Start from" date selection: First scan, Today, or custom date
- Multi-day scan collection via `collect_scans_in_range()`
- Retry logic with exponential backoff (3 attempts per image)
- Window title shows download progress and next-poll countdown
- Graceful shutdown on window close during watch mode
- Created setup plan for on-site deployment

**Blocked:**
- Can't reach `129.31.116.189:80` from Jamie's local machine
- Need to run `probe` from Imperial network to get the WSDL
- WSDL defines exact parameter types needed to complete download/watch commands

**Next (at Imperial):**
1. Install Python + pip on the remote desktop or lab machine
2. Run `probe` to fetch WSDL and discover method signatures
3. Save WSDL files locally (Standard.wsdl, FastInitial.wsdl)
4. Update the script with correct parameter types
5. Test login, experiments, download
6. Get `watch` mode working — the main deliverable

## Known SOAP Methods (extracted from DLL)

### Authentication
- `ValidateUserLogin` — login with username/password
- `GetWebServiceVersion` — check service version (FastInitialConnection)

### Experiments & Vessels
- `GetAllUsableExperimentDefs` — list experiments
- `CreateVessel` / `DeleteVessel` — manage vessels
- `GetExpDef` — get experiment definition
- `GetScanVessel` — get vessel scan data
- `GetAllRestorableVessels` — list restorable vessels
- `GetAllowedUnscannedVessels` — list unscanned vessels

### Scans & Timing
- `AllScanTimes` — list all scan timepoints
- `GetScanTime` — get specific scan info
- `BeginScan` — start a scan
- `GetScansCountBetween` — count scans in range
- `RetrieveLatestScanData` — get most recent scan

### Image Download
- `GetImagePayload` — download image data (main method)
- `GetImagePayloadLite` — lightweight image download
- `GetScanVesselImagePayload` — vessel-specific image
- `GetCalTestImagePayload` — calibration images
- `GetImageFiles` / `GetImageFileInfos` — list image files
- `GetImageMetrics` — image metrics/metadata
- `DownloadFile` / `DownloadFilesAsync` — file download

### Analysis
- `GetMetrics` / `GetVesselMetrics` — analysis metrics
- `GetObjects` — detected objects
- `GetSubPopObjectsComplete` — sub-population data

## Jamie's Workflow

- Incucyte hardware captures images on a schedule (e.g., every few hours)
- Jamie sets up one output folder per experiment
- Currently: manually opens GUI, clicks download for each new scan
- Goal: script polls automatically and saves TIFs to the experiment folder

## Usage

### GUI (recommended)
```bash
pip install zeep requests pillow numpy
python incucyte_gui.py
```
The GUI provides login, vessel/well selection, channel filters, output folder,
"Start from" date picker (First scan / Today / Custom), parallel worker count,
green LUT toggle, and a progress dialog during downloads.

### CLI
```bash
pip install zeep requests pillow numpy

# Test connection (must be on Imperial network)
python incucyte_downloader.py probe --host 129.31.116.189

# Login
python incucyte_downloader.py login --host 129.31.116.189

# List experiments
python incucyte_downloader.py experiments

# Download all images from the very first scan
python incucyte_downloader.py download -v 38 -o ./images --start-from first

# Download from a specific date
python incucyte_downloader.py download -v 38 -o ./images --start-from 2026-03-01

# Auto-download new images every 10 minutes (from first scan onward)
python incucyte_downloader.py watch -v 38 -o ./images -i 10 --start-from first

# Download with 8 parallel threads, no green LUT
python incucyte_downloader.py download -v 38 -o ./images --workers 8 --no-green-lut

# Debug: raw SOAP request
python incucyte_downloader.py raw GetWebServiceVersion
```

## Files

```
IncucyteAI/
  incucyte_downloader.py   Main CLI tool (probe, login, download, watch)
  incucyte_gui.py          Tkinter GUI for interactive use
  CLAUDE.md                This file — agent context
  SETUP_PLAN.md            Step-by-step setup guide for on-site deployment
  README.md                User-facing documentation
  .gitignore               Excludes .tmp/, __pycache__, WSDL files
  .tmp/                    State files, config, captures (gitignored)
  Standard.wsdl            (will exist after probe) — SOAP method definitions
  FastInitial.wsdl         (will exist after probe) — fast connection methods
```

## Key Technical Details

- The Incucyte .NET client uses `System.Web.Services.Protocols.SoapHttpClientProtocol`
- Source paths visible in stack traces: `H:\agent\_work\8\s\App\Essen.IncuCyte.Client\`
- Key classes: `IncuCyteDeviceClient`, `ConnectViewModel`, `TiffExportProvider`
- The client connects to port 80 using standard SOAP 1.1 over HTTP
- Authentication likely returns a session token used in subsequent requests
- Image data is returned as byte arrays (payloads) that are raw TIF data
- Downloads use ThreadPoolExecutor for parallel fetching (default 4 workers)
- Phase images (ImageType 1) are converted to green-channel RGB TIFs by default
- Retry logic: 3 attempts with exponential backoff (1s, 2s) on transient failures
- State tracked in `.tmp/incucyte_state.json` to avoid re-downloading
- GUI state persisted in `.tmp/gui_state.json`
