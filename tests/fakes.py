"""A fake Incucyte device, so the API can be tested without the instrument.

The real device is only reachable from the site network, so every test that
exercises planning or downloading stands this in for the two calls that matter:
``api_post`` for metadata and ``_fetch_scan_vessel_image_bytes`` for pixels.
"""

import io
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
from PIL import Image

from pyincucyte import engine
from pyincucyte.config import ConfigStore, Credentials


def tiff_bytes(value=7, shape=(6, 8), dtype=np.uint16):
    """Return a single-plane TIFF payload filled with one value."""
    buffer = io.BytesIO()
    Image.fromarray(np.full(shape, value, dtype=dtype)).save(buffer, format="TIFF")
    return buffer.getvalue()


def vessel_record(vessel_id=38, name="Test plate", plate="Sarstedt 24-well",
                  first="2026-03-01T09:00:00", last="2026-03-03T09:00:00",
                  color1="GFP", color2="mCherry"):
    """Build one record shaped like GetAllSearchVessels returns."""
    return {
        "VesselID": vessel_id,
        "VesselTypeName": plate,
        "VesselTypeID": 4,
        "VesselDocumentation": {"Label": name, "UserName": "tester"},
        "FirstScanDateTime": first,
        "LastScanDateTime": last,
        "HasBeenScanned": True,
        "ScanTypeDisplayText": "Standard",
        "Channels": {
            "Phase": {"On": True},
            "Colors": {
                "Color1": {"On": True, "ColorName": color1},
                "Color2": {"On": True, "ColorName": color2},
            },
            "Color1Name": color1,
            "Color2Name": color2,
        },
    }


def image_info(row, col, image_type, site=0):
    return {
        "Swell": {"RowZeroBased": row, "ColumnZeroBased": col},
        "SwellSite": {"ValueZeroBased": site},
        "ImageType": image_type,
    }


class FakeDevice:
    """Answers the handful of routes planning and downloading actually use."""

    def __init__(self, vessels=None, scans=None, wells=((0, 0), (0, 1)),
                 channels=(1, 2), missing_scans=()):
        self.vessels = list(vessels or [vessel_record()])
        self.scans = list(scans or ["2026-03-01T09:00:00", "2026-03-01T12:00:00"])
        self.wells = list(wells)
        self.channels = list(channels)
        self.missing_scans = set(missing_scans)
        self.calls = []
        self.fetches = []

    # -- routes -----------------------------------------------------------

    def api_post(self, host, token, route, payload=None, timeout=None):
        self.calls.append((route, payload))
        if route == "Vessels/GetAllSearchVessels":
            return {"Data": {"$values": self.vessels}}
        if route == "Scans/AllScanTimes":
            day = f"{payload['Year']:04d}-{payload['Month']:02d}-{payload['Day']:02d}"
            return {"Data": [s for s in self.scans if s.startswith(day)]}
        if route == "Vessels/GetScanVessel":
            when = payload["DateTime"]
            if when in self.missing_scans:
                raise engine.ApiError(
                    "API exception: ScanNotFoundException - Vessel ID='38' "
                    "existing scan was not found")
            infos = [image_info(r, c, t)
                     for (r, c) in self.wells for t in self.channels]
            return {"Data": {"ImageInfos": infos}}
        if route == "Device/Status/GetDeviceStatusUpdate":
            return {"Data": {"State": "Idle"}}
        raise AssertionError(f"unexpected route {route}")

    # -- pixels -----------------------------------------------------------

    def fetch_image(self, host, token, item, max_retries=3):
        self.fetches.append(item.get("fname"))
        return tiff_bytes(value=item.get("img_type", 1)), None


@contextmanager
def patched(device):
    """Point the engine at a :class:`FakeDevice` for the duration of a block."""
    real_post = engine.api_post
    real_fetch = engine._fetch_scan_vessel_image_bytes
    engine.api_post = device.api_post
    engine._fetch_scan_vessel_image_bytes = device.fetch_image
    try:
        yield device
    finally:
        engine.api_post = real_post
        engine._fetch_scan_vessel_image_bytes = real_fetch


def logged_in_store(tmp_path, host="10.0.0.1"):
    """Return a ConfigStore holding a token that is valid for another hour."""
    store = ConfigStore(tmp_path / "credentials.json")
    store.save(Credentials(
        host=host, username="tester", encrypted_password="hashed",
        token="test-token",
        token_expires_at=(datetime.now() + timedelta(hours=1)).isoformat(),
        login_time=datetime.now().isoformat()))
    return store
