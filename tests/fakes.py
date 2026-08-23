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

from pyincucyte import device, engine
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


def image_info(row, col, image_type, site=0, scale=None, bias=0.0, median=0.0):
    """One ImageInfos entry. Fluorescence carries calibration, Phase does not."""
    info = {
        "Swell": {"RowZeroBased": row, "ColumnZeroBased": col},
        "SwellSite": {"ValueZeroBased": site},
        "ImageType": image_type,
    }
    if scale is not None:
        info.update({"Scale": scale, "Bias": bias, "ImageMedian": median,
                     "NoiseStd": 0.0134})
    return info


def unmix_pair(recipient, contributor, ratio, sigma=0.0):
    """One ColorUnmixes entry. Colours are numbered 1 and 2, not 2 and 3."""
    return {"Recipient": recipient, "Contributor": contributor,
            "ValueRatio": ratio, "BlurringSigma": sigma}


class FakeDevice:
    """Answers the handful of routes planning and downloading actually use."""

    def __init__(self, vessels=None, scans=None, wells=((0, 0), (0, 1)),
                 channels=(1, 2), missing_scans=(), calibration=None,
                 unmixes=None, pixels=None, activity="Idle", drawer="Closed",
                 user_id=7, next_scan=None, refuse=None):
        self.vessels = list(vessels or [vessel_record()])
        self.scans = list(scans or ["2026-03-01T09:00:00", "2026-03-01T12:00:00"])
        self.wells = list(wells)
        self.channels = list(channels)
        self.missing_scans = set(missing_scans)
        #: channel number -> (Scale, Bias, ImageMedian), as the real device
        #: reports for fluorescence and never for Phase.
        self.calibration = dict(calibration or {})
        self.unmixes = list(unmixes or [])
        #: channel number -> the value every pixel of that channel holds.
        self.pixels = dict(pixels or {})
        #: what the instrument says it is doing, by DeviceActivityTypeCode name.
        self.activity = activity
        self.drawer = drawer
        self.next_scan = next_scan
        self.user_id = user_id
        #: route -> message, to make one route answer with an API exception.
        self.refuse = dict(refuse or {})
        self.calls = []
        self.fetches = []
        #: every write the device was asked to make, so a test can prove that
        #: an unconfirmed call sent nothing at all.
        self.scans_requested = []
        self.saved_unmixes = []

    # -- routes -----------------------------------------------------------

    def api_post(self, host, token, route, payload=None, timeout=None):
        self.calls.append((route, payload))
        if route in self.refuse:
            raise engine.ApiError(f"API exception: {self.refuse[route]}",
                                  route=route)
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
            infos = []
            for (r, c) in self.wells:
                for t in self.channels:
                    scale, bias, median = self.calibration.get(t, (None, 0.0, 0.0))
                    infos.append(image_info(r, c, t, scale=scale, bias=bias,
                                            median=median))
            scan = {"ImageInfos": infos}
            if self.unmixes:
                scan["ColorUnmixes"] = {"$values": list(self.unmixes)}
            return {"Data": scan}
        if route == device.ROUTE_STATUS:
            return {"Data": self.status_payload()}
        if route == device.ROUTE_TEMPERATURES:
            return {"Data": {"$values": [self.temperature_payload()]}}
        if route == device.ROUTE_SCAN_PATTERN:
            return {"Data": {
                "Name": "Standard",
                "ImagesPerSwell": 2,
                "Magnification": "X10",
                "IsWholeWellSamplePattern": False,
                "Swells": {"$values": [{"RowZeroBased": r, "ColumnZeroBased": c}
                                       for (r, c) in self.wells]},
            }}
        if route == device.ROUTE_VALIDATE_LOGIN:
            return {"Data": {"ID": self.user_id, "UserName": "tester",
                             "PermissionLevel": 2}}
        if route == device.ROUTE_BEGIN_SCAN:
            self.scans_requested.append(payload)
            return {"Data": True}
        if route == device.ROUTE_SAVE_UNMIX:
            self.saved_unmixes.append(payload)
            return {"Data": True}
        raise AssertionError(f"unexpected route {route}")

    # -- device state -----------------------------------------------------

    def status_payload(self):
        """A DeviceStatusUpdateState, with the enums as the ints .NET sends."""
        return {
            "DeviceStatus": {
                "DeviceActivity": device.DEVICE_ACTIVITY.index(self.activity),
                "DrawerStatus": device.DRAWER_STATUS.index(self.drawer),
                "IsAutomationMode": False,
                "PercentageComplete": 42.0 if self.activity == "Scanning" else None,
                "TimeToComplete": "01:12:00" if self.activity == "Scanning" else None,
                "LastScan": self.scans[-1] if self.scans else None,
                "DateTime": datetime.now().isoformat(timespec="seconds"),
                "NextScanInfo": {"NextScan": self.next_scan,
                                 "UserName": "tester" if self.next_scan else "",
                                 "LastModified": None},
                "Temperature": self.temperature_payload(),
            },
            "ValidAcquisitionTypes": {"$values": [0]},
        }

    def temperature_payload(self):
        return {
            "DeviceDateTime": datetime.now().isoformat(timespec="seconds"),
            "DeviceActivity": device.DEVICE_ACTIVITY.index(self.activity),
            "GantryBoardDegreesCelcius": 37.4,
            "OpticsBoardDegreesCelcius": 36.1,
            "CubeBoardDegreesCelcius": None,
            "CameraDegreesCelcius": 29.8,
            "PredictedTemp": 37.0,
            "AcquisitionTypeID": 1,
        }

    # -- pixels -----------------------------------------------------------

    def fetch_image(self, host, token, item, max_retries=3):
        self.fetches.append(item.get("fname"))
        img_type = item.get("img_type", 1)
        return tiff_bytes(value=self.pixels.get(img_type, img_type)), None


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
