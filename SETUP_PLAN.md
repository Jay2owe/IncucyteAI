# IncucyteAI Setup Plan

Follow these steps IN ORDER when at Imperial (on-site or VPN connected).
Each step depends on the previous one succeeding.

## Prerequisites

- You must be on the Imperial College network (WiFi, wired, or VPN)
- You need admin rights on the machine you're setting up
- You need your Incucyte login credentials (username + password)
- The Incucyte device at `129.31.116.189` must be powered on

## Step 1: Install Python

1. Download Python 3.12+ from https://www.python.org/downloads/
2. Run the installer
3. **IMPORTANT**: Check "Add python.exe to PATH" at the bottom of the installer
4. Click "Install Now"
5. Open a NEW Command Prompt (old ones won't have PATH updated)
6. Verify:
   ```
   python --version
   pip --version
   ```

**If pip is missing** (shouldn't happen with full installer):
```
python -m ensurepip --upgrade
```

## Step 2: Install Dependencies

```
pip install zeep requests
```

That's it — only 2 packages needed (zeep handles SOAP, requests handles HTTP).

## Step 3: Get the Script

The `IncucyteAI` folder is in Dropbox:
```
Brancaccio Lab\Jamie\Macros and Scripts\Claude\IncucyteAI\
```

If Dropbox isn't synced on the machine, copy these files manually:
- `incucyte_downloader.py` (the main script)
- `CLAUDE.md` (agent context)

## Step 4: Test Network Connectivity

Open Command Prompt, navigate to the IncucyteAI folder, and run:

```
python incucyte_downloader.py probe --host 129.31.116.189
```

**Expected output if working:**
```
Port 80 is OPEN
Standard.asmx WSDL: OK
FastInitialConnection.asmx WSDL: OK
=== Available SOAP Methods ===
  - ValidateUserLogin
  - GetImagePayload
  - AllScanTimes
  ... (many more)
```

**If port 80 is CLOSED:**
- Verify you're on Imperial network: `ping 129.31.116.189` should work
- The Incucyte device may be off — check physically
- Try a different port: `python incucyte_downloader.py probe --host 129.31.116.189 --port 8080`
- Ask IT if there's a firewall rule blocking your machine

## Step 5: Save the WSDL (Critical!)

Once probe succeeds, save the WSDL locally so we can work on it offline:

```
curl "http://129.31.116.189/IncuCyteWS/Standard.asmx?WSDL" > Standard.wsdl
curl "http://129.31.116.189/IncuCyteWS/FastInitialConnection.asmx?WSDL" > FastInitial.wsdl
```

Or if curl isn't available, open these URLs in a browser and save the pages:
- `http://129.31.116.189/IncuCyteWS/Standard.asmx?WSDL`
- `http://129.31.116.189/IncuCyteWS/FastInitialConnection.asmx?WSDL`

Save both files into the `IncucyteAI/` folder. These define the exact API
methods and parameter types — the agent needs them to complete the download
and watch commands.

## Step 6: Login

```
python incucyte_downloader.py login --host 129.31.116.189
```

Enter your Incucyte username and password. If login succeeds, credentials are
saved to `.tmp/incucyte_config.json` so you don't need to re-enter them.

**If login fails** with a parameter error:
- The `ValidateUserLogin` signature may differ from what we guessed
- Check the probe output — it lists the exact parameter names
- Tell the agent what parameters the method expects

## Step 7: List Experiments

```
python incucyte_downloader.py experiments
```

This should show your available experiments. Note the experiment name/ID
you want to auto-download.

## Step 8: Test Download

```
python incucyte_downloader.py download -e "YourExperimentName" -o ./test_images
```

If download fails with a "parameters" error, the agent needs the WSDL files
from Step 5 to fix the method calls. Paste the probe output or WSDL content
to the agent.

## Step 9: Set Up Auto-Download (Watch Mode)

Once download works for a single image:

```
python incucyte_downloader.py watch -e "YourExperiment" -o "D:\path\to\images" -i 10
```

This polls every 10 minutes and downloads new scans automatically.

## Step 10: (Optional) Schedule as Windows Task

To run the watcher automatically without being logged in:

1. Open Task Scheduler (`taskschd.msc`)
2. Create Basic Task > name it "Incucyte Auto-Download"
3. Trigger: "When the computer starts" or "Daily"
4. Action: Start a program
   - Program: `python`
   - Arguments: `incucyte_downloader.py watch -e "YourExperiment" -o "D:\images" -i 10`
   - Start in: `C:\path\to\IncucyteAI`
5. Check "Run whether user is logged on or not"

---

## For the Agent

When Jamie runs these steps and hits a problem, here's what to do:

### If probe shows the methods but parameter names differ
Read the WSDL files (Standard.wsdl, FastInitial.wsdl) and update
`incucyte_downloader.py` with the correct parameter names and types.

### If probe fails (port closed)
- Try ports: 80, 8080, 443, 8443
- Check if the Incucyte app uses HTTPS (look at the .config file)
- Try `http://129.31.116.189/IncuCyteWS/Standard.asmx` in a browser

### If login works but experiments/download fails
The methods likely need session tokens or cookies. Check:
- Does `ValidateUserLogin` return a token?
- Does the WSDL show a SOAP header for authentication?
- Capture the Incucyte GUI's network traffic with Wireshark for the exact flow

### If WSDL is saved
Parse it and update the script:
1. Read Standard.wsdl
2. For each method the script uses, check the exact parameter names and types
3. Update `cmd_download`, `cmd_watch`, `cmd_scans` etc. with correct signatures
4. Implement the full download loop in `cmd_watch`

### Priority
The most important thing is `cmd_watch` — that's the whole point. Everything
else (login, experiments, scans) is infrastructure to make watch work.
The watch loop should:
1. Authenticate
2. Get list of scans (AllScanTimes or similar)
3. Compare against previously downloaded scans (state file)
4. For each new scan, call GetImagePayload or GetScanVesselImagePayload
5. Save the TIF data to the output directory
6. Update the state file
7. Sleep and repeat
