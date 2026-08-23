"""Wire-level engine for the Incucyte REST API.

This module holds the proven request/response and TIFF-writing code that talks
to the Incucyte device.  It is deliberately low level: everything takes an
explicit ``host`` and bearer ``token``.  Application code should prefer
:class:`pyincucyte.client.IncucyteClient`, which wraps these functions in a
session object with typed results, structured errors, and a manifest.

Nothing in here writes to stdout — progress is emitted through the module
logger (``logging.getLogger("pyincucyte.engine")``) and through the explicit
``progress_callback`` arguments.
"""

import base64
import io
import json
import logging
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path

from .errors import (
    ApiError, AuthenticationError, DeviceUnreachableError,
    EncryptionUnavailableError, NotLoggedInError, TokenExpiredError,
)

# Incucyte device defaults
DEFAULT_HOST = "incucyte.invalid"
API_BASE_TEMPLATE = "https://{host}/IncucyteWSs"

# State/config files
PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = PACKAGE_DIR.parent          # repository root for a source checkout
LEGACY_APP_DIR = SCRIPT_DIR / ".tmp"

log = logging.getLogger("pyincucyte.engine")

# Where the vendor .NET client lives.  The password-encryption assembly ships
# with it, so we need one real install to hash a password.  Version folders
# change between releases (2021C, 2024B, ...), so discover rather than hardcode.
INCUCYTE_INSTALL_GLOBS = ("Incucyte*", "IncuCyte*")


def find_incucyte_install():
    """Return the newest installed Incucyte client folder, or None."""
    override = os.environ.get("PYINCUCYTE_CLIENT_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None

    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    ]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in INCUCYTE_INSTALL_GLOBS:
            for candidate in root.glob(pattern):
                if candidate.is_dir() and (candidate / "Dlls").is_dir():
                    found.append(candidate)
    if not found:
        return None
    # Newest version folder wins ("Incucyte 2024B" sorts after "Incucyte 2021C").
    return sorted(set(found), key=lambda p: p.name)[-1]


INCUCYTE_INSTALL_DIR = find_incucyte_install()


#: Files that mark a folder as a real settings folder rather than an empty one.
SETTINGS_FILES = ("incucyte_config.json", "download_state.json", "gui_state.json")


def default_app_dir():
    """Return the per-user folder for saved tokens, GUI state, and download state.

    The project was called PyIncucyteGUI until 0.3, so a settings folder under
    the old name is preferred when one exists: renaming the package must not
    log anybody out or lose their well selections.
    """
    for variable in ("PYINCUCYTE_HOME", "PYINCUCYTEGUI_HOME"):
        env_dir = os.environ.get(variable)
        if env_dir:
            return Path(env_dir).expanduser()

    if any((LEGACY_APP_DIR / name).exists() for name in SETTINGS_FILES):
        return LEGACY_APP_DIR

    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        candidates = (base / "PyIncucyte", base / "PyIncucyteGUI")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        candidates = (base / "pyincucyte", base / "pyincucytegui")

    for candidate in candidates:
        if any((candidate / name).exists() for name in SETTINGS_FILES):
            return candidate
    return candidates[0]


APP_DIR = default_app_dir()
STATE_FILE = APP_DIR / "download_state.json"
CONFIG_FILE = APP_DIR / "incucyte_config.json"

# Suppress SSL warnings (Incucyte uses self-signed cert)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


IMAGE_TYPE_MAP = {"phase": 1, "color1": 2, "green": 2, "color2": 3, "red": 3}
IMAGE_TYPE_LABELS = {1: "Phase", 2: "Green", 3: "Red"}
IMAGE_TYPE_SHORT_LABELS = {1: "phase", 2: "green", 3: "red"}
CHANNEL_HELP = "phase, green/color1, red/color2, all"


def parse_wells(spec):
    """Parse a well specification string into a set of (row, col) tuples (zero-based).

    Supports:
        "A1"          -> single well
        "A1,B3,C2"   -> comma-separated list
        "A1-A6"       -> range within a row
        "A1-D4"       -> rectangular range (all wells in the rectangle)
        "all" or None -> None (meaning no filter, download all)

    Returns a set of (row, col) tuples, or None for "all".
    """
    if spec is None or spec.strip().lower() == "all":
        return None
    if not spec.strip():
        return set()          # an empty spec selects nothing, the inverse of "all"

    def parse_single(w):
        w = w.strip().upper()
        if not w or len(w) < 2:
            raise ValueError(f"Invalid well: '{w}'")
        row = ord(w[0]) - ord('A')
        col = int(w[1:]) - 1
        if row < 0 or col < 0:
            raise ValueError(f"Invalid well: '{w}'")
        return (row, col)

    wells = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            # Range: "A1-A6" or "A1-D4"
            endpoints = part.split("-", 1)
            r1, c1 = parse_single(endpoints[0])
            r2, c2 = parse_single(endpoints[1])
            for r in range(min(r1, r2), max(r1, r2) + 1):
                for c in range(min(c1, c2), max(c1, c2) + 1):
                    wells.add((r, c))
        else:
            wells.add(parse_single(part))
    return wells


def parse_channels(spec):
    """Parse a channel specification string into a set of image type ints.

    Supports: "phase", "green"/"color1", "red"/"color2",
    comma-separated combinations, "all", or None.
    Returns a set of ints, or None for "all".
    """
    if spec is None or spec.strip().lower() == "all":
        return None
    channels = set()
    for name in spec.split(","):
        name = name.strip().lower()
        if name not in IMAGE_TYPE_MAP:
            raise ValueError(f"Unknown channel '{name}'. Use: {CHANNEL_HELP}")
        channels.add(IMAGE_TYPE_MAP[name])
    return channels


def parse_filter_arg(filter_str):
    """Parse a --filter argument like '38:A1,B3,C2' into (vessel_id, wells_set)."""
    if ":" in filter_str:
        vid_str, wells_str = filter_str.split(":", 1)
        return int(vid_str), parse_wells(wells_str)
    else:
        return int(filter_str), None


def ensure_tmp():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"downloaded": {}}


def save_state(state):
    ensure_tmp()
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# Key used to attach a StateStore to the plain dict the download functions pass
# around.  When present, persistence is delegated to that store (which batches
# writes and can be scoped to an output folder); when absent, behaviour falls
# back to the single global state file this script has always used.
STATE_STORE_KEY = "_store"


def persist_state(state):
    """Persist download state through its attached store, or globally."""
    store = state.get(STATE_STORE_KEY) if isinstance(state, dict) else None
    if store is not None:
        store.mark_dirty()
        return
    save_state(state)


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(config):
    ensure_tmp()
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def encrypt_password(plain_password):
    """Encrypt password using Incucyte's Essen.Security.Encryption via pythonnet."""
    try:
        import clr
        install = INCUCYTE_INSTALL_DIR or find_incucyte_install()
        if install is None:
            raise FileNotFoundError(
                "no Incucyte client installation found under Program Files; "
                "set PYINCUCYTE_CLIENT_DIR to its folder")
        base = str(install)
        if base not in sys.path:
            sys.path.append(base)
            for root, dirs, files in os.walk(os.path.join(base, "Dlls")):
                sys.path.append(root)
            os.environ["PATH"] = os.path.join(base, "Dlls", "EssenCppLib") + ";" + os.environ.get("PATH", "")
        try:
            clr.AddReference("Essen")
        except Exception:
            pass
        from Essen.Security import Encryption
        return Encryption.EncryptedString(plain_password)
    except Exception as e:
        raise EncryptionUnavailableError(
            f"Could not encrypt password: {e}. Install pythonnet and make sure the "
            f"Incucyte client is present at {INCUCYTE_INSTALL_DIR}."
        ) from e


# One pooled HTTPS session per host.  Parallel downloads previously opened a
# fresh TLS connection per image; pooling removes that handshake per request.
_SESSIONS = {}
_SESSION_LOCK = threading.Lock()
API_TIMEOUT = 30
CONNECTION_POOL_SIZE = 32


def session_for(host):
    """Return the shared, connection-pooled requests.Session for a host."""
    import requests
    from requests.adapters import HTTPAdapter

    with _SESSION_LOCK:
        session = _SESSIONS.get(host)
        if session is None:
            session = requests.Session()
            session.verify = False
            adapter = HTTPAdapter(pool_connections=4,
                                  pool_maxsize=CONNECTION_POOL_SIZE)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _SESSIONS[host] = session
        return session


def close_sessions():
    """Close every pooled session (used on shutdown and in tests)."""
    with _SESSION_LOCK:
        for session in _SESSIONS.values():
            try:
                session.close()
            except Exception:
                pass
        _SESSIONS.clear()


def get_token(host, username, encrypted_password):
    """Get an OAuth2 Bearer token from the Incucyte API."""
    import requests
    url = f"{API_BASE_TEMPLATE.format(host=host)}/token"
    try:
        resp = session_for(host).post(url,
            data=f"grant_type=password&username={username}&password={encrypted_password}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15)
    except requests.exceptions.RequestException as e:
        raise DeviceUnreachableError(
            f"Could not reach the Incucyte at {host}: {e}. "
            f"The device is only routable from the site network."
        ) from e
    if resp.status_code != 200:
        try:
            error = resp.json().get("error_description", resp.text[:200])
        except ValueError:
            error = resp.text[:200]
        raise AuthenticationError(f"Authentication failed: {error}")
    data = resp.json()
    return data["access_token"], data.get("expires_in", 86400)


def api_post(host, token, route, payload=None, timeout=None):
    """Make an authenticated POST to the Incucyte REST API."""
    import requests
    url = f"{API_BASE_TEMPLATE.format(host=host)}/api/{route}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = session_for(host).post(url, json=payload or {}, headers=headers,
                                      timeout=timeout or API_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise DeviceUnreachableError(
            f"Could not reach the Incucyte at {host} ({route}): {e}") from e
    if resp.status_code == 401:
        raise TokenExpiredError("Token expired or invalid — re-run login")
    if resp.status_code != 200:
        raise ApiError(f"API error {resp.status_code}: {resp.text[:200]}",
                       status_code=resp.status_code, route=route,
                       body=resp.text[:2000])
    data = resp.json()
    if data.get("Status") == "Exception":
        raise ApiError(f"API exception: {data.get('ShortMessage', 'unknown')}",
                       route=route, body=data)
    return data


def unpack_values(obj):
    """Recursively convert .NET $values arrays to Python lists."""
    if isinstance(obj, dict):
        if "$values" in obj:
            return [unpack_values(v) for v in obj["$values"]]
        return {k: unpack_values(v) for k, v in obj.items() if not k.startswith("$")}
    elif isinstance(obj, list):
        return [unpack_values(v) for v in obj]
    return obj


def channel_name_from_channels(channels, img_type):
    """Return an Incucyte channel display name from vessel channel metadata."""
    if img_type == 1:
        return "Phase"

    color_key = {2: "Color1", 3: "Color2"}.get(img_type)
    default = IMAGE_TYPE_LABELS.get(img_type, f"ImageType{img_type}")
    if not color_key or not isinstance(channels, dict):
        return default

    colors = channels.get("Colors", {})
    candidates = [
        channels.get(f"{color_key}Name"),
        colors.get(f"{color_key}Name") if isinstance(colors, dict) else None,
    ]
    for container in (colors, channels):
        if isinstance(container, dict):
            state = container.get(color_key, {})
            if isinstance(state, dict):
                candidates.extend([
                    state.get("ColorName"),
                    state.get("Name"),
                    state.get("DisplayName"),
                ])

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def vessel_id_from_record(vessel):
    """Return a numeric vessel id from Incucyte vessel/search-vessel records."""
    if not isinstance(vessel, dict):
        return None
    for key in ("VesselID", "VesselId", "vesselId", "ID", "Id", "id"):
        value = vessel.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    nested = vessel.get("Vessel")
    if isinstance(nested, dict) and nested is not vessel:
        return vessel_id_from_record(nested)
    return None


def _looks_like_search_vessel(record):
    """Return True when a dict is an Incucyte search-vessel record."""
    if not isinstance(record, dict) or vessel_id_from_record(record) is None:
        return False
    vessel_keys = (
        "VesselTypeID", "VesselTypeName", "VesselDocumentation",
        "FirstScanDateTime", "LastScanDateTime", "HasBeenScanned",
        "ScanTypeDisplayText", "Channels",
    )
    return any(key in record for key in vessel_keys)


def _valid_search_vessel_list(candidate):
    """Normalize a possible vessel collection and return only real vessel records."""
    values = unpack_values(candidate)
    if not isinstance(values, list):
        return []
    return [item for item in values if _looks_like_search_vessel(item)]


def extract_search_vessels(response):
    """Extract real Incucyte search-vessel records from API response variants."""
    data = response.get("Data", response) if isinstance(response, dict) else response

    direct_candidates = [data]
    if isinstance(data, dict):
        for key in ("SearchVessels", "Vessels", "Items", "Results"):
            if key in data:
                direct_candidates.append(data[key])

        unpacked_data = unpack_values(data)
        direct_candidates.append(unpacked_data)
        if isinstance(unpacked_data, dict):
            for key in ("SearchVessels", "Vessels", "Items", "Results"):
                if key in unpacked_data:
                    direct_candidates.append(unpacked_data[key])

    best = []
    for candidate in direct_candidates:
        vessels = _valid_search_vessel_list(candidate)
        if len(vessels) > len(best):
            best = vessels

    def walk(obj):
        nonlocal best
        vessels = _valid_search_vessel_list(obj)
        if len(vessels) > len(best):
            best = vessels
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    if not best:
        walk(data)
    return best


def authenticate(args):
    """Authenticate and return (host, token). Uses saved config if available."""
    config = load_config()
    host = getattr(args, "host", None) or config.get("host", DEFAULT_HOST)

    # Check for saved token
    if config.get("token") and config.get("token_expires_at"):
        expires = datetime.fromisoformat(config["token_expires_at"])
        if datetime.now() < expires:
            return host, config["token"]

    # Need to get a new token
    username = config.get("username")
    encrypted_pw = config.get("encrypted_password")
    if not username or not encrypted_pw:
        raise NotLoggedInError("Not logged in. Run 'login' first.")

    token, expires_in = get_token(host, username, encrypted_pw)

    # Save token
    config["token"] = token
    config["token_expires_at"] = (datetime.now().replace(microsecond=0) +
                                   __import__("datetime").timedelta(seconds=expires_in - 60)).isoformat()
    save_config(config)
    return host, token


# --- Commands ---


def parse_scan_datetime(scan_time):
    """Parse a scan time string into a datetime object."""
    # Handle ISO format like "2026-03-23T12:30:00+00:00" or "2026-03-23T12:30:00"
    clean = scan_time.split("+")[0].split("Z")[0]
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")


def format_elapsed(delta):
    """Format a timedelta as DDdHHhMMm (e.g. '00d00h30m')."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days:02d}d{hours:02d}h{minutes:02d}m"


def find_first_scan_time(host, token, max_days_back=90):
    """Search backwards from today to find the earliest scan time.

    Returns a datetime, or None if no scans found.
    """
    earliest = None
    empty_streak = 0
    d = date.today()
    for i in range(max_days_back):
        check = d - __import__("datetime").timedelta(days=i)
        try:
            data = api_post(host, token, "Scans/AllScanTimes", {
                "Year": check.year, "Month": check.month, "Day": check.day,
            })
            scans = unpack_values(data.get("Data", []))
            if isinstance(scans, list) and scans:
                earliest = parse_scan_datetime(scans[0])
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= 3 and earliest is not None:
                    break
        except Exception:
            empty_streak += 1
            if empty_streak >= 3 and earliest is not None:
                break
    return earliest


def collect_scans_in_range(host, token, start_date, end_date=None,
                           progress_callback=None, stop_event=None,
                           reverse=False, enough=None):
    """Fetch all scan times from start_date through end_date (inclusive).

    Args:
        start_date: date object for the first day to check.
        end_date: date object for the last day (default: today).
        reverse: walk from end_date backwards - what "the last N frames" needs,
            because its start date is not known until enough scans are found.
        enough: optional predicate called with the scans gathered so far; when
            it returns True the sweep stops early. Saves querying three months
            of days to satisfy "the first 100 frames".

    Returns a list of scan time strings.
    """
    if end_date is None:
        end_date = date.today()
    scans = []
    one_day = __import__("datetime").timedelta(days=1)
    total_days = max(1, (end_date - start_date).days + 1)
    d = end_date if reverse else start_date
    done = 0
    while start_date <= d <= end_date:
        if stop_event and stop_event.is_set():
            break
        done += 1
        if progress_callback:
            progress_callback(d, done, total_days)
        try:
            data = api_post(host, token, "Scans/AllScanTimes", {
                "Year": d.year, "Month": d.month, "Day": d.day,
            })
            day_scans = unpack_values(data.get("Data", []))
            if isinstance(day_scans, list):
                scans.extend(day_scans)
        except Exception:
            pass
        if enough is not None and enough(scans):
            break
        d = (d - one_day) if reverse else (d + one_day)
    return scans


def apply_green_lut(tif_bytes):
    """Convert grayscale TIF bytes to green-channel RGB TIF bytes."""
    from PIL import Image
    import numpy as np

    img = Image.open(io.BytesIO(tif_bytes))
    arr = np.array(img)
    rgb = np.zeros((*arr.shape, 3), dtype=arr.dtype)
    rgb[..., 1] = arr  # Green channel only
    rgb_img = Image.fromarray(rgb)
    out = io.BytesIO()
    rgb_img.save(out, format="TIFF")
    return out.getvalue()


def image_type_sort_key(img_type):
    """Return the channel order used in ImageJ hyperstacks."""
    return {1: 0, 2: 1, 3: 2}.get(img_type, img_type)


def image_type_label(img_type):
    """Return a readable channel label for ImageJ metadata and filenames."""
    return IMAGE_TYPE_LABELS.get(img_type, f"ImageType{img_type}")


def channel_tag(img_types):
    """Return a compact, stable filename tag for a set/list of image types."""
    names = [
        IMAGE_TYPE_SHORT_LABELS.get(img_type, f"type{img_type}")
        for img_type in sorted(img_types, key=image_type_sort_key)
    ]
    return "-".join(names)


def well_site_name(row, col, site=0):
    """Return a well name, adding a site suffix only for multi-site scans."""
    name = f"{chr(65 + row)}{col + 1}"
    if site:
        name = f"{name}_s{site + 1}"
    return name


def is_missing_scan_vessel_error(error):
    """Return True when a global scan exists but does not contain this vessel."""
    msg = str(error)
    return (
        "ScanNotFoundException" in msg
        or "Did not look for scan" in msg
        or (
            "Vessel ID='" in msg
            and "existing scan" in msg
            and "was not found" in msg
        )
    )


def collect_scan_images(host, token, vessel_id, scan_time, output_dir,
                        state=None, wells=None, channels=None,
                        reference_time=None):
    """Collect the list of images to download (without downloading them).

    Returns a list of dicts with keys: fname, fpath, state_key, row, col, site,
    img_type, vessel_id, scan_time.
    """
    try:
        sv_data = api_post(host, token, "Vessels/GetScanVessel", {
            "VesselID": vessel_id,
            "DateTime": scan_time,
            "IncludeDiagnosticMetrics": False,
        })
    except RuntimeError as e:
        if is_missing_scan_vessel_error(e):
            return []
        raise

    sv = unpack_values(sv_data.get("Data", {}))
    images = sv.get("ImageInfos", [])
    if not isinstance(images, list):
        images = []

    scan_dt_obj = parse_scan_datetime(scan_time)
    elapsed = format_elapsed(scan_dt_obj - reference_time) if reference_time else None

    to_download = []
    for img in images:
        swell = img.get("Swell", {})
        swell_site = img.get("SwellSite", {})
        img_type = img.get("ImageType", 1)
        row = swell.get("RowZeroBased", 0)
        col = swell.get("ColumnZeroBased", 0)
        site = swell_site.get("ValueZeroBased", 0)

        if wells is not None and (row, col) not in wells:
            continue
        if channels is not None and img_type not in channels:
            continue

        well_letter = chr(65 + row)
        well_name = f"{well_letter}{col + 1}"
        if elapsed:
            fname = f"VID{vessel_id}_{well_name}_{img_type}_{elapsed}.tif"
        else:
            scan_dt = scan_time.replace(":", "").replace("-", "").split("+")[0].split("T")
            scan_tag = f"{scan_dt[0]}_{scan_dt[1]}" if len(scan_dt) == 2 else scan_time
            fname = f"VID{vessel_id}_{well_name}_{img_type}_{scan_tag}.tif"
        fpath = output_dir / fname

        if fpath.exists():
            continue

        state_key = f"{vessel_id}_{scan_time}_{row}_{col}_{site}_{img_type}"
        if state and state_key in state.get("downloaded", {}):
            continue

        to_download.append({
            "fname": fname, "fpath": fpath, "state_key": state_key,
            "row": row, "col": col, "site": site, "img_type": img_type,
            "vessel_id": vessel_id, "scan_time": scan_time,
        })

    return to_download


def collect_scan_hyperstacks(host, token, vessel_id, scan_time, output_dir,
                             state=None, wells=None, channels=None,
                             reference_time=None):
    """Collect ImageJ hyperstacks to create for a scan.

    A hyperstack item groups all selected channel payloads for one
    vessel/well/site/scan time into a single output TIFF.
    """
    try:
        sv_data = api_post(host, token, "Vessels/GetScanVessel", {
            "VesselID": vessel_id,
            "DateTime": scan_time,
            "IncludeDiagnosticMetrics": False,
        })
    except RuntimeError as e:
        if is_missing_scan_vessel_error(e):
            return []
        raise

    sv = unpack_values(sv_data.get("Data", {}))
    images = sv.get("ImageInfos", [])
    if not isinstance(images, list):
        images = []

    grouped = {}
    for img in images:
        swell = img.get("Swell", {})
        swell_site = img.get("SwellSite", {})
        img_type = img.get("ImageType", 1)
        row = swell.get("RowZeroBased", 0)
        col = swell.get("ColumnZeroBased", 0)
        site = swell_site.get("ValueZeroBased", 0)

        if wells is not None and (row, col) not in wells:
            continue
        if channels is not None and img_type not in channels:
            continue

        key = (row, col, site)
        grouped.setdefault(key, {})[img_type] = {
            "row": row, "col": col, "site": site, "img_type": img_type,
            "vessel_id": vessel_id, "scan_time": scan_time,
        }

    scan_dt_obj = parse_scan_datetime(scan_time)
    elapsed = format_elapsed(scan_dt_obj - reference_time) if reference_time else None

    to_download = []
    for (row, col, site), by_type in grouped.items():
        selected_types = set(channels) if channels is not None else set(by_type)
        if not selected_types:
            continue
        if not selected_types.issubset(by_type):
            continue

        ordered_types = sorted(selected_types, key=image_type_sort_key)
        ch_tag = channel_tag(ordered_types)
        well_letter = chr(65 + row)
        well_name = f"{well_letter}{col + 1}"
        if elapsed:
            fname = f"VID{vessel_id}_{well_name}_{ch_tag}_{elapsed}.tif"
        else:
            scan_dt = scan_time.replace(":", "").replace("-", "").split("+")[0].split("T")
            scan_tag = f"{scan_dt[0]}_{scan_dt[1]}" if len(scan_dt) == 2 else scan_time
            fname = f"VID{vessel_id}_{well_name}_{ch_tag}_{scan_tag}.tif"
        fpath = output_dir / fname

        if fpath.exists():
            continue

        state_key = f"hyperstack_{vessel_id}_{scan_time}_{row}_{col}_{site}_{ch_tag}"
        if state and state_key in state.get("downloaded", {}):
            continue

        to_download.append({
            "fname": fname, "fpath": fpath, "state_key": state_key,
            "row": row, "col": col, "site": site, "vessel_id": vessel_id,
            "scan_time": scan_time,
            "channels": [by_type[img_type] for img_type in ordered_types],
            "channel_types": ordered_types,
        })

    return to_download


def collect_time_stacks(host, token, vessel_id, scan_times, output_dir,
                        state=None, wells=None, channels=None,
                        reference_time=None, channel_hyperstack=False,
                        progress_callback=None, stop_event=None, max_workers=1):
    """Collect time stacks to create across multiple scan times.

    When channel_hyperstack is False, returns one stack per well/site/channel.
    When True, returns one ImageJ C+T hyperstack per well/site.
    """
    scan_times = sorted(scan_times, key=parse_scan_datetime)
    grouped = {}

    total_scans = len(scan_times)
    completed_scans = 0

    def fetch_images(scan_time):
        if stop_event and stop_event.is_set():
            return scan_time, []
        try:
            sv_data = api_post(host, token, "Vessels/GetScanVessel", {
                "VesselID": vessel_id,
                "DateTime": scan_time,
                "IncludeDiagnosticMetrics": False,
            })
        except RuntimeError as e:
            if is_missing_scan_vessel_error(e):
                return scan_time, []
            raise

        sv = unpack_values(sv_data.get("Data", {}))
        images = sv.get("ImageInfos", [])
        if not isinstance(images, list):
            images = []
        return scan_time, images

    def add_images(scan_time, images):
        for img in images:
            swell = img.get("Swell", {})
            swell_site = img.get("SwellSite", {})
            img_type = img.get("ImageType", 1)
            row = swell.get("RowZeroBased", 0)
            col = swell.get("ColumnZeroBased", 0)
            site = swell_site.get("ValueZeroBased", 0)

            if wells is not None and (row, col) not in wells:
                continue
            if channels is not None and img_type not in channels:
                continue

            key = (row, col, site)
            grouped.setdefault(key, {}).setdefault(scan_time, {})[img_type] = {
                "row": row, "col": col, "site": site, "img_type": img_type,
                "vessel_id": vessel_id, "scan_time": scan_time,
            }

    if total_scans:
        try:
            workers = int(max_workers or 1)
        except (TypeError, ValueError):
            workers = 1
        workers = max(1, min(workers, total_scans))
    else:
        workers = 1

    if workers == 1:
        for scan_time in scan_times:
            if stop_event and stop_event.is_set():
                break
            scan_time, images = fetch_images(scan_time)
            completed_scans += 1
            if progress_callback:
                progress_callback(vessel_id, scan_time, completed_scans, total_scans)
            add_images(scan_time, images)
    else:
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(fetch_images, scan_time): scan_time
                       for scan_time in scan_times}
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                scan_time, images = future.result()
                completed_scans += 1
                if progress_callback:
                    progress_callback(vessel_id, scan_time, completed_scans, total_scans)
                add_images(scan_time, images)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    to_download = []
    downloaded_state = state.get("downloaded", {}) if state else {}

    for (row, col, site), by_time in grouped.items():
        available_types = set()
        for by_type in by_time.values():
            available_types.update(by_type)
        selected_types = sorted(
            set(channels) if channels is not None else available_types,
            key=image_type_sort_key,
        )
        if not selected_types:
            continue

        well_name = well_site_name(row, col, site)
        if channel_hyperstack:
            frames = []
            for scan_time in scan_times:
                by_type = by_time.get(scan_time, {})
                if all(img_type in by_type for img_type in selected_types):
                    frames.append({
                        "scan_time": scan_time,
                        "channels": [by_type[img_type] for img_type in selected_types],
                    })
            if not frames:
                continue

            ch_tag = channel_tag(selected_types)
            fname = f"VID{vessel_id}_{well_name}_{ch_tag}_timestack.tif"
            state_key = f"timestack_hyper_{vessel_id}_{row}_{col}_{site}_{ch_tag}"
            labels = [image_type_label(img_type) for img_type in selected_types]
            frame_times = [frame["scan_time"] for frame in frames]
            fpath = output_dir / fname
            state_info = downloaded_state.get(state_key, {})
            if fpath.exists() and state_info.get("scan_times") == frame_times:
                continue
            to_download.append({
                "fname": fname, "fpath": fpath, "state_key": state_key,
                "row": row, "col": col, "site": site, "vessel_id": vessel_id,
                "frames": frames, "scan_times": frame_times, "labels": labels,
                "channel_hyperstack": True,
            })
            continue

        for img_type in selected_types:
            frames = []
            for scan_time in scan_times:
                by_type = by_time.get(scan_time, {})
                if img_type in by_type:
                    frames.append(by_type[img_type])
            if not frames:
                continue

            ch_tag = IMAGE_TYPE_SHORT_LABELS.get(img_type, f"type{img_type}")
            fname = f"VID{vessel_id}_{well_name}_{ch_tag}_timestack.tif"
            state_key = f"timestack_{vessel_id}_{row}_{col}_{site}_{ch_tag}"
            frame_times = [frame["scan_time"] for frame in frames]
            fpath = output_dir / fname
            state_info = downloaded_state.get(state_key, {})
            if fpath.exists() and state_info.get("scan_times") == frame_times:
                continue
            to_download.append({
                "fname": fname, "fpath": fpath, "state_key": state_key,
                "row": row, "col": col, "site": site, "vessel_id": vessel_id,
                "frames": frames, "scan_times": frame_times,
                "labels": [image_type_label(img_type)],
                "channel_hyperstack": False,
            })

    return to_download


def collect_scan_items_parallel(host, token, vessel_id, scan_times, output_dir,
                                state=None, wells=None, channels=None,
                                reference_time=None, hyperstack=False,
                                max_workers=4, progress_callback=None,
                                stop_event=None):
    """Collect per-scan image or hyperstack download items using parallel scan checks."""
    scan_times = sorted(scan_times, key=parse_scan_datetime)
    total_scans = len(scan_times)
    if not total_scans:
        return []

    try:
        workers = int(max_workers or 1)
    except (TypeError, ValueError):
        workers = 1
    workers = max(1, min(workers, total_scans))

    collector = collect_scan_hyperstacks if hyperstack else collect_scan_images

    def collect_one(scan_time):
        if stop_event and stop_event.is_set():
            return scan_time, []
        items = collector(
            host, token, vessel_id, scan_time, output_dir,
            state=state, wells=wells, channels=channels,
            reference_time=reference_time)
        return scan_time, items

    completed = 0
    results = {}

    if workers == 1:
        for scan_time in scan_times:
            if stop_event and stop_event.is_set():
                break
            scan_time, items = collect_one(scan_time)
            completed += 1
            if progress_callback:
                progress_callback(vessel_id, scan_time, completed, total_scans)
            results[scan_time] = items
    else:
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(collect_one, scan_time): scan_time
                       for scan_time in scan_times}
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                scan_time, items = future.result()
                completed += 1
                if progress_callback:
                    progress_callback(vessel_id, scan_time, completed, total_scans)
                results[scan_time] = items
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    to_download = []
    for scan_time in scan_times:
        to_download.extend(results.get(scan_time, []))
    return to_download


def _fetch_scan_vessel_image_bytes(host, token, item, max_retries=3):
    """Fetch one image payload from the REST API and return raw TIFF bytes."""
    last_error = None
    for attempt in range(max_retries):
        try:
            payload_data = api_post(host, token, "Images/Payloads/GetScanVesselImagePayload", {
                "Identifier": {
                    "VesselID": item["vessel_id"],
                    "ScanDateTime": item["scan_time"],
                    "Swell": {"RowZeroBased": item["row"], "ColumnZeroBased": item["col"]},
                    "SwellSite": {"ValueZeroBased": item["site"]},
                },
                "ScanVesselImageType": item["img_type"],
            })
            break  # Success
        except RuntimeError as e:
            last_error = e
            if "Token expired" in str(e) or attempt == max_retries - 1:
                return None, f"SKIP {item['fname']}: {e}"
            time.sleep(2 ** attempt)  # 1s, 2s backoff
    else:
        return None, f"SKIP {item['fname']}: {last_error}"

    img_bytes = extract_image_bytes(payload_data)
    if not img_bytes:
        return None, f"SKIP {item['fname']}: no image data in response"

    return img_bytes, None


def _fetch_payload(host, token, item, max_retries=3, cache=None):
    """Fetch one payload, serving it from the local cache when possible.

    Rebuilding a time stack re-reads every frame it contains.  Without the
    cache that means re-downloading the whole experiment each time a new scan
    lands; with it, each source image crosses the network exactly once.
    """
    if cache is not None:
        cached = cache.get(item)
        if cached:
            return cached, None

    img_bytes, error = _fetch_scan_vessel_image_bytes(host, token, item, max_retries)
    if cache is not None and img_bytes:
        cache.put(item, img_bytes)
    return img_bytes, error


def _download_single_image(host, token, item, state, state_lock, green_phase=False,
                           max_retries=3):
    """Download a single image with retry. Returns (fname, size) on success, None on failure."""
    img_bytes, error = _fetch_scan_vessel_image_bytes(host, token, item, max_retries)
    if error:
        return None, error

    # Apply green LUT for Phase images (ImageType == 1)
    if green_phase and item["img_type"] == 1:
        try:
            img_bytes = apply_green_lut(img_bytes)
        except Exception as exc:
            # The LUT is cosmetic; a failure must not cost us the image.
            log.debug("green LUT failed for %s: %s", item["fname"], exc)

    item["fpath"].write_bytes(img_bytes)

    if state is not None:
        with state_lock:
            state.setdefault("downloaded", {})[item["state_key"]] = {
                "file": str(item["fpath"]),
                "time": datetime.now().isoformat(),
                "size": len(img_bytes),
            }
            persist_state(state)

    return item["fname"], len(img_bytes)


def _tiff_bytes_to_array(tif_bytes):
    """Read the first TIFF plane from Incucyte payload bytes as a NumPy array."""
    from PIL import Image
    import numpy as np

    with Image.open(io.BytesIO(tif_bytes)) as img:
        arr = np.array(img)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr


def _common_hyperstack_dtype(arrays):
    """Choose a stack dtype that preserves all selected channel values."""
    import numpy as np

    if any(np.issubdtype(arr.dtype, np.floating) for arr in arrays):
        return np.float32
    if any(np.issubdtype(arr.dtype, np.signedinteger) for arr in arrays):
        return np.int32 if any(arr.dtype.itemsize > 2 for arr in arrays) else np.int16
    return np.uint16 if any(arr.dtype.itemsize > 1 for arr in arrays) else np.uint8


def _validate_stack_array(arr, expected_shape, output_dtype, label):
    """Return one frame/channel array in the exact shape and dtype being written."""
    import numpy as np

    if arr.shape != expected_shape:
        raise ValueError(
            f"{label} dimensions do not match: expected {expected_shape}, got {arr.shape}"
        )
    output_dtype = np.dtype(output_dtype)
    if arr.dtype != output_dtype and not np.can_cast(arr.dtype, output_dtype, casting="safe"):
        raise ValueError(f"{label} dtype {arr.dtype} cannot be safely written as {output_dtype}")
    return arr.astype(output_dtype, copy=False)


def _write_imagej_stack(path, arrays, shape, dtype, axes, labels=None):
    """Write an ImageJ stack from an array iterator without building a full stack."""
    import tifffile

    metadata = {
        "axes": axes,
        "mode": "grayscale",
    }
    if labels is not None:
        metadata["Labels"] = labels

    tifffile.imwrite(str(path), arrays, shape=shape, dtype=dtype,
                     imagej=True, metadata=metadata,
                     photometric="minisblack")


def write_imagej_hyperstack(path, channel_arrays, labels):
    """Write channel arrays as an ImageJ-compatible CYX TIFF hyperstack."""
    if not channel_arrays:
        raise ValueError("No channel arrays provided")

    shape = channel_arrays[0].shape
    if any(arr.shape != shape for arr in channel_arrays):
        shapes = ", ".join(str(arr.shape) for arr in channel_arrays)
        raise ValueError(f"Channel dimensions do not match: {shapes}")

    dtype = _common_hyperstack_dtype(channel_arrays)
    arrays = (
        _validate_stack_array(arr, shape, dtype, "Channel")
        for arr in channel_arrays
    )
    _write_imagej_stack(path, arrays, (len(channel_arrays), *shape),
                        dtype, "CYX", labels=labels)


def write_imagej_time_stack(path, frame_arrays):
    """Write one channel over time as an ImageJ TYX TIFF stack."""
    if not frame_arrays:
        raise ValueError("No time frames provided")

    shape = frame_arrays[0].shape
    if any(arr.shape != shape for arr in frame_arrays):
        shapes = ", ".join(str(arr.shape) for arr in frame_arrays)
        raise ValueError(f"Frame dimensions do not match: {shapes}")

    dtype = _common_hyperstack_dtype(frame_arrays)
    arrays = (
        _validate_stack_array(arr, shape, dtype, "Frame")
        for arr in frame_arrays
    )
    _write_imagej_stack(path, arrays, (len(frame_arrays), *shape),
                        dtype, "TYX")


def write_imagej_time_hyperstack(path, timepoint_channel_arrays, labels):
    """Write selected channels over time as an ImageJ TCYX TIFF hyperstack."""
    import numpy as np

    if not timepoint_channel_arrays:
        raise ValueError("No time frames provided")
    if not timepoint_channel_arrays[0]:
        raise ValueError("No channel arrays provided")

    n_channels = len(timepoint_channel_arrays[0])
    if any(len(channels) != n_channels for channels in timepoint_channel_arrays):
        raise ValueError("Time points do not all contain the same number of channels")

    all_arrays = [arr for channels in timepoint_channel_arrays for arr in channels]
    shape = all_arrays[0].shape
    if any(arr.shape != shape for arr in all_arrays):
        shapes = ", ".join(str(arr.shape) for arr in all_arrays)
        raise ValueError(f"Frame dimensions do not match: {shapes}")

    dtype = _common_hyperstack_dtype(all_arrays)
    arrays = (
        np.stack([
            _validate_stack_array(arr, shape, dtype, "Frame")
            for arr in channels
        ], axis=0)
        for channels in timepoint_channel_arrays
    )
    _write_imagej_stack(path, arrays,
                        (len(timepoint_channel_arrays), n_channels, *shape),
                        dtype, "TCYX", labels=labels)


def _download_hyperstack(host, token, item, state, state_lock, max_retries=3,
                         cache=None):
    """Download selected channels and write a single ImageJ hyperstack TIFF."""
    arrays = []
    labels = []
    for channel_item in item["channels"]:
        channel_item = dict(channel_item)
        channel_item["fname"] = f"{item['fname']}:{image_type_label(channel_item['img_type'])}"
        img_bytes, error = _fetch_payload(host, token, channel_item, max_retries,
                                          cache=cache)
        if error:
            return None, error.replace(channel_item["fname"], item["fname"], 1)
        try:
            arrays.append(_tiff_bytes_to_array(img_bytes))
            labels.append(image_type_label(channel_item["img_type"]))
        except Exception as e:
            return None, f"SKIP {item['fname']}: could not read channel TIFF ({e})"

    try:
        write_imagej_hyperstack(item["fpath"], arrays, labels)
    except ImportError as e:
        return None, f"SKIP {item['fname']}: missing dependency ({e}); install tifffile"
    except Exception as e:
        _cleanup_partial_file(item["fpath"])
        return None, f"SKIP {item['fname']}: could not write hyperstack ({e})"

    size = item["fpath"].stat().st_size
    if state is not None:
        with state_lock:
            state.setdefault("downloaded", {})[item["state_key"]] = {
                "file": str(item["fpath"]),
                "time": datetime.now().isoformat(),
                "size": size,
                "hyperstack": True,
                "channels": labels,
            }
            persist_state(state)

    return item["fname"], size


def _cleanup_partial_file(path):
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def count_time_stack_payloads(items):
    """Count source image payloads needed for collected time stack items."""
    total = 0
    for item in items:
        if item.get("channel_hyperstack"):
            total += sum(len(frame["channels"]) for frame in item["frames"])
        else:
            total += len(item["frames"])
    return total


class _StackDownloadError(RuntimeError):
    """Expected stack-building failure with a user-facing message."""


class _StackDownloadCancelled(RuntimeError):
    """Internal signal used to stop a stack between source-image downloads."""


def _stack_dtype_from_first(arrays):
    """Choose the output dtype once a streaming stack has its first plane(s)."""
    return _common_hyperstack_dtype(arrays)


def _stream_time_stack(first_array, remaining_arrays, path, frame_count):
    """Write a TYX ImageJ stack while keeping only the current frame in memory."""
    dtype = _stack_dtype_from_first([first_array])
    frame_shape = first_array.shape

    def arrays():
        yield _validate_stack_array(first_array, frame_shape, dtype, "Frame")
        for arr in remaining_arrays:
            yield _validate_stack_array(arr, frame_shape, dtype, "Frame")

    _write_imagej_stack(path, arrays(), (frame_count, *frame_shape), dtype, "TYX")


def _stack_timepoint_channels(channel_arrays, frame_shape, dtype):
    """Build one CYX timepoint from its channel arrays."""
    import numpy as np

    return np.stack([
        _validate_stack_array(arr, frame_shape, dtype, "Frame")
        for arr in channel_arrays
    ], axis=0)


def _stream_time_hyperstack(first_channels, remaining_timepoints,
                            path, timepoint_count, labels):
    """Write a TCYX ImageJ stack while keeping one timepoint in memory."""
    if not first_channels:
        raise ValueError("No channel arrays provided")

    dtype = _stack_dtype_from_first(first_channels)
    frame_shape = first_channels[0].shape
    channel_count = len(first_channels)

    def arrays():
        yield _stack_timepoint_channels(first_channels, frame_shape, dtype)
        for channel_arrays in remaining_timepoints:
            if len(channel_arrays) != channel_count:
                raise ValueError("Time points do not all contain the same number of channels")
            yield _stack_timepoint_channels(channel_arrays, frame_shape, dtype)

    _write_imagej_stack(path, arrays(),
                        (timepoint_count, channel_count, *frame_shape),
                        dtype, "TCYX", labels=labels)


def _download_time_stack(host, token, item, state, state_lock, max_retries=3,
                         unit_progress_callback=None, stop_event=None,
                         cache=None):
    """Download all frames for one time stack output file."""
    if item.get("channel_hyperstack"):
        def timepoint_arrays():
            for frame in item["frames"]:
                if stop_event and stop_event.is_set():
                    raise _StackDownloadCancelled
                channel_arrays = []
                for channel_item in frame["channels"]:
                    if stop_event and stop_event.is_set():
                        raise _StackDownloadCancelled
                    channel_item = dict(channel_item)
                    channel_item["fname"] = (
                        f"{item['fname']}:{frame['scan_time']}:"
                        f"{image_type_label(channel_item['img_type'])}"
                    )
                    img_bytes, error = _fetch_payload(
                        host, token, channel_item, max_retries, cache=cache)
                    if error:
                        raise _StackDownloadError(
                            error.replace(channel_item["fname"], item["fname"], 1)
                        )
                    if unit_progress_callback:
                        unit_progress_callback(channel_item["fname"], len(img_bytes))
                    try:
                        channel_arrays.append(_tiff_bytes_to_array(img_bytes))
                    except Exception as e:
                        raise _StackDownloadError(
                            f"SKIP {item['fname']}: could not read channel TIFF ({e})"
                        ) from e
                    finally:
                        img_bytes = None
                yield channel_arrays

        try:
            timepoints = timepoint_arrays()
            first_channels = next(timepoints)
            _stream_time_hyperstack(
                first_channels, timepoints, item["fpath"],
                len(item["frames"]), item["labels"])
        except StopIteration:
            return None, f"SKIP {item['fname']}: no time frames provided"
        except _StackDownloadCancelled:
            _cleanup_partial_file(item["fpath"])
            return None, None
        except _StackDownloadError as e:
            _cleanup_partial_file(item["fpath"])
            return None, str(e)
        except ImportError as e:
            return None, f"SKIP {item['fname']}: missing dependency ({e}); install tifffile"
        except Exception as e:
            _cleanup_partial_file(item["fpath"])
            return None, f"SKIP {item['fname']}: could not write time hyperstack ({e})"
    else:
        def frame_arrays():
            for frame_item in item["frames"]:
                if stop_event and stop_event.is_set():
                    raise _StackDownloadCancelled
                frame_item = dict(frame_item)
                frame_item["fname"] = f"{item['fname']}:{frame_item['scan_time']}"
                img_bytes, error = _fetch_payload(
                    host, token, frame_item, max_retries, cache=cache)
                if error:
                    raise _StackDownloadError(
                        error.replace(frame_item["fname"], item["fname"], 1)
                    )
                if unit_progress_callback:
                    unit_progress_callback(frame_item["fname"], len(img_bytes))
                try:
                    arr = _tiff_bytes_to_array(img_bytes)
                except Exception as e:
                    raise _StackDownloadError(
                        f"SKIP {item['fname']}: could not read frame TIFF ({e})"
                    ) from e
                finally:
                    img_bytes = None
                yield arr

        try:
            frames = frame_arrays()
            first_frame = next(frames)
            _stream_time_stack(first_frame, frames, item["fpath"], len(item["frames"]))
        except StopIteration:
            return None, f"SKIP {item['fname']}: no time frames provided"
        except _StackDownloadCancelled:
            _cleanup_partial_file(item["fpath"])
            return None, None
        except _StackDownloadError as e:
            _cleanup_partial_file(item["fpath"])
            return None, str(e)
        except ImportError as e:
            return None, f"SKIP {item['fname']}: missing dependency ({e}); install tifffile"
        except Exception as e:
            _cleanup_partial_file(item["fpath"])
            return None, f"SKIP {item['fname']}: could not write time stack ({e})"

    size = item["fpath"].stat().st_size
    if state is not None:
        with state_lock:
            state.setdefault("downloaded", {})[item["state_key"]] = {
                "file": str(item["fpath"]),
                "time": datetime.now().isoformat(),
                "size": size,
                "time_stack": True,
                "channel_hyperstack": bool(item.get("channel_hyperstack")),
                "channels": item["labels"],
                "scan_times": item["scan_times"],
            }
            persist_state(state)

    return item["fname"], size


def download_scan_images(host, token, vessel_id, scan_time, output_dir,
                         state=None, wells=None, channels=None,
                         reference_time=None, max_workers=4,
                         green_phase=False, progress_callback=None,
                         stop_event=None, hyperstack=False):
    """Download images for a vessel at a given scan time.

    Args:
        wells: set of (row, col) tuples to include, or None for all.
        channels: set of image type ints to include, or None for all.
        reference_time: datetime for elapsed time calculation (experiment start).
        max_workers: number of parallel download threads (default 4).
        green_phase: if True, apply green LUT to Phase (ImageType 1) images.
        hyperstack: if True, save selected channels as one ImageJ TIFF per well/site/time.
        progress_callback: callable(fname, size, downloaded_count, total_count)
                          called after each successful download.
        stop_event: threading.Event — if set, abort remaining downloads.
    """
    collector = collect_scan_hyperstacks if hyperstack else collect_scan_images
    to_download = collector(host, token, vessel_id, scan_time,
                            output_dir, state, wells, channels,
                            reference_time)
    if not to_download:
        return 0

    state_lock = threading.Lock()
    downloaded = 0
    total = len(to_download)
    print_lock = threading.Lock()

    def do_one(item):
        if stop_event and stop_event.is_set():
            return None, None
        if hyperstack:
            return _download_hyperstack(host, token, item, state, state_lock)
        return _download_single_image(host, token, item, state, state_lock, green_phase)

    workers = min(max_workers, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(do_one, item): item for item in to_download}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            fname, result = future.result()
            if fname is None:
                if result:  # error message
                    with print_lock:
                        log.warning("%s", result)
            else:
                downloaded += 1
                with print_lock:
                    log.info("%s (%s bytes)", fname, f"{result:,}")
                if progress_callback:
                    progress_callback(fname, result, downloaded, total)

    return downloaded


def download_collected_scan_items(host, token, to_download, state=None,
                                  max_workers=4, green_phase=False,
                                  progress_callback=None, stop_event=None,
                                  hyperstack=False, error_callback=None,
                                  cache=None):
    """Download already-collected scan image or hyperstack items."""
    if not to_download:
        return 0

    state_lock = threading.Lock()
    downloaded = 0
    total = len(to_download)
    print_lock = threading.Lock()

    def do_one(item):
        if stop_event and stop_event.is_set():
            return None, None
        if hyperstack or "channels" in item:
            return _download_hyperstack(host, token, item, state, state_lock,
                                        cache=cache)
        return _download_single_image(host, token, item, state, state_lock, green_phase)

    workers = min(max_workers, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(do_one, item): item for item in to_download}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            fname, result = future.result()
            if fname is None:
                if result:
                    with print_lock:
                        log.warning("%s", result)
                    if error_callback:
                        error_callback(result)
            else:
                downloaded += 1
                with print_lock:
                    log.info("%s (%s bytes)", fname, f"{result:,}")
                if progress_callback:
                    progress_callback(fname, result, downloaded, total)

    return downloaded


def download_time_stacks(host, token, vessel_id, scan_times, output_dir,
                         state=None, wells=None, channels=None,
                         reference_time=None, max_workers=2,
                         progress_callback=None, stop_event=None,
                         channel_hyperstack=False,
                         collection_callback=None,
                         unit_progress_callback=None, cache=None):
    """Download selected scans as one or more ImageJ time stacks."""
    to_download = collect_time_stacks(
        host, token, vessel_id, scan_times, output_dir,
        state=state, wells=wells, channels=channels,
        reference_time=reference_time, channel_hyperstack=channel_hyperstack,
        progress_callback=collection_callback, stop_event=stop_event,
        max_workers=max_workers)
    return download_collected_time_stack_items(
        host, token, to_download, state=state, max_workers=max_workers,
        progress_callback=progress_callback, stop_event=stop_event,
        unit_progress_callback=unit_progress_callback, cache=cache)


def download_collected_time_stack_items(host, token, to_download,
                                        state=None, max_workers=2,
                                        progress_callback=None,
                                        stop_event=None,
                                        unit_progress_callback=None,
                                        error_callback=None, cache=None):
    """Download already-collected time stack items."""
    if not to_download:
        return 0

    state_lock = threading.Lock()
    unit_lock = threading.Lock()
    downloaded = 0
    total = len(to_download)
    total_units = count_time_stack_payloads(to_download)
    completed_units = 0
    print_lock = threading.Lock()

    def emit_unit_progress(label, size):
        nonlocal completed_units
        if not unit_progress_callback:
            return
        with unit_lock:
            completed_units += 1
            done = completed_units
        unit_progress_callback(label, size, done, total_units)

    def do_one(item):
        if stop_event and stop_event.is_set():
            return None, None
        return _download_time_stack(
            host, token, item, state, state_lock,
            unit_progress_callback=emit_unit_progress,
            stop_event=stop_event, cache=cache)

    workers = min(max_workers, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(do_one, item): item for item in to_download}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            fname, result = future.result()
            if fname is None:
                if result:
                    with print_lock:
                        log.warning("%s", result)
                    if error_callback:
                        error_callback(result)
            else:
                downloaded += 1
                with print_lock:
                    log.info("%s (%s bytes)", fname, f"{result:,}")
                if progress_callback:
                    progress_callback(fname, result, downloaded, total)

    return downloaded


def extract_image_bytes(payload_data):
    """Recursively find and decode the base64 image data from the API response."""
    def find_b64(obj):
        if isinstance(obj, str) and len(obj) > 1000:
            try:
                decoded = base64.b64decode(obj)
                if decoded[:2] in (b"II", b"MM"):  # TIFF header
                    return decoded
            except Exception:
                pass
        elif isinstance(obj, dict):
            for v in obj.values():
                result = find_b64(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for v in obj:
                result = find_b64(v)
                if result:
                    return result
        return None

    return find_b64(payload_data)


