"""Large-run timeline behaviour without a real instrument."""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import tifffile

from pyincucyte import IncucyteClient
from pyincucyte.models import Vessel, VesselScan
from pyincucyte.pyramid import PyramidFrame, PyramidLevel, PyramidUnavailable
from pyincucyte.timeline import (
    HybridTimelineSource, TimelinePreview, choose_frames,
    discover_local_stacks, downsample_native,
)

from fakes import logged_in_store, tiff_bytes


class MinimalClient:
    host = "10.0.0.1"

    def ensure_token(self):
        return "token"

    def scan_detail(self, vessel_id, scan_time):
        return {"wells": {(0, 0)}, "channels": {1}, "sites": {0},
                "image_count": 1, "coefficients": {}, "unmixing": None,
                "pyramid_levels": []}


def scans(count, *, pyramid=False):
    vessel = Vessel(id=38, name="Long run", rows=1, cols=1,
                    channel_labels={1: "Phase"}, active_channels={1})
    levels = ([{"Level0": 3,
                "Size_pixels": {"Width": 64, "Height": 48}}]
              if pyramid else [])
    first = datetime(2026, 1, 1)
    return [VesselScan(
        vessel=vessel,
        scan_time=(first + timedelta(hours=index)).isoformat(timespec="seconds"),
        wells={(0, 0)}, channels={1}, sites={0}, image_count=1,
        pyramid_levels=levels)
        for index in range(count)]


class FakePyramidTransport:
    def __init__(self):
        self.calls = []
        self.capabilities = {"phase": True}

    def fetch_frame(self, request, level, cancel=None):
        self.calls.append((request["scan_time"], level.level))
        value = int(request["scan_time"][11:13])
        return PyramidFrame(array=np.full((48, 64), value, np.uint8),
                            source_bytes=512, level=level.level, tile_count=1)


class UnavailablePyramidTransport:
    capabilities = {"phase": False}

    def fetch_frame(self, request, level, cancel=None):
        raise PyramidUnavailable("viewer route unavailable")


class MissingPyramidTransport:
    capabilities = {"phase": True}

    def fetch_frame(self, request, level, cancel=None):
        return PyramidFrame(level=level.level, error="viewer returned no tile")


class TimelineSamplingTests(unittest.TestCase):
    def test_choose_frames_spans_first_and_latest_without_duplicates(self):
        picked = choose_frames(range(2000), 100)
        self.assertEqual(len(picked), 100)
        self.assertEqual((picked[0], picked[-1]), (0, 1999))
        self.assertEqual(tuple(sorted(set(picked))), picked)

    def test_block_mean_preserves_native_type_and_bounds(self):
        source = np.arange(1000 * 800, dtype=np.uint16).reshape(1000, 800)
        reduced = downsample_native(source, 256)
        self.assertLessEqual(max(reduced.shape), 256)
        self.assertEqual(reduced.dtype, np.uint16)


class TimelineSourceTests(unittest.TestCase):
    def setUp(self):
        self.client = MinimalClient()

    def test_two_thousand_frames_prime_at_most_one_hundred(self):
        source = HybridTimelineSource(self.client, scans(2000), frame_cache=32,
                                      render_cache=8, max_edge=64)

        def fetch(_host, _token, item, max_retries=3):
            value = int(item["scan_time"][11:13])
            return tiff_bytes(value=value, shape=(80, 120)), None

        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   side_effect=fetch):
            anchors = source.prime("A1", 0, 1, count=100)
            before = source.network_fetches
            frame = source.get_frame("A1", 0, 1, 1337)
        self.assertEqual(len(anchors), 100)
        self.assertEqual(before, 100)
        self.assertEqual(source.network_fetches, 101)
        self.assertLessEqual(source.cache_size, 32)
        self.assertLessEqual(max(frame.shape), 64)

    def test_full_tiff_initial_reads_stop_at_the_byte_budget(self):
        raw = tiff_bytes(value=4, shape=(40, 60))
        source = HybridTimelineSource(
            self.client, scans(100), frame_cache=10,
            fallback_byte_budget=len(raw) * 3, max_edge=32)
        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   return_value=(raw, None)):
            source.prime("A1", 0, 1, count=100)
        self.assertEqual(source.network_fetches, 3)
        self.assertEqual(len(source.initial_indices), 3)

    def test_concurrent_requests_for_one_frame_are_deduplicated(self):
        source = HybridTimelineSource(self.client, scans(3), frame_cache=3)
        calls = []

        def fetch(_host, _token, item, max_retries=3):
            calls.append(item["scan_time"])
            time.sleep(0.03)
            return tiff_bytes(value=8), None

        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   side_effect=fetch):
            with ThreadPoolExecutor(max_workers=5) as pool:
                arrays = list(pool.map(
                    lambda _value: source.get_frame("A1", 0, 1, 1), range(5)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(np.array_equal(arrays[0], array) for array in arrays))

    def test_pyramid_tile_prevents_the_full_tiff_fallback(self):
        transport = FakePyramidTransport()
        source = HybridTimelineSource(self.client, scans(4, pyramid=True),
                                      transport=transport, max_edge=64)
        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   side_effect=AssertionError("full TIFF should not be read")):
            frame = source.get_frame("A1", 0, 1, 2)
        self.assertEqual(frame.shape, (48, 64))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(source.frame_info("A1", 0, 1, 2).source,
                         "pyramid tile")

    def test_unavailable_pyramid_route_falls_back_to_full_tiff(self):
        source = HybridTimelineSource(
            self.client, scans(1, pyramid=True),
            transport=UnavailablePyramidTransport(), max_edge=64)
        raw = tiff_bytes(value=17, shape=(80, 120))
        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   return_value=(raw, None)):
            frame = source.get_frame("A1", 0, 1, 0)
        self.assertTrue(np.all(frame == 17))
        self.assertEqual(source.frame_info("A1", 0, 1, 0).source,
                         "full TIFF fallback")

    def test_missing_pyramid_frame_falls_back_to_full_tiff(self):
        source = HybridTimelineSource(
            self.client, scans(1, pyramid=True),
            transport=MissingPyramidTransport(), max_edge=64)
        raw = tiff_bytes(value=21, shape=(80, 120))
        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   return_value=(raw, None)):
            frame = source.get_frame("A1", 0, 1, 0)
        self.assertTrue(np.all(frame == 21))
        self.assertEqual(source.frame_info("A1", 0, 1, 0).source,
                         "full TIFF fallback")

    def test_proxy_reopening_makes_no_device_request(self):
        with TemporaryDirectory() as tmp:
            source = HybridTimelineSource(self.client, scans(2), proxy_dir=tmp)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       return_value=(tiff_bytes(value=23), None)):
                first = source.get_frame("A1", 0, 1, 1)
            source.close()

            reopened = HybridTimelineSource(self.client, scans(2), proxy_dir=tmp)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       side_effect=AssertionError("proxy should win")):
                second = reopened.get_frame("A1", 0, 1, 1)
            self.assertTrue(np.array_equal(first, second))
            self.assertEqual(reopened.network_fetches, 0)
            self.assertEqual(reopened.frame_info("A1", 0, 1, 1).source, "proxy")

    def test_existing_local_stack_wins_before_the_device(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "VID38_A1_phase_timestack.tif"
            tifffile.imwrite(path, np.stack([
                np.full((10, 12), 3, np.uint16),
                np.full((10, 12), 7, np.uint16),
            ]), photometric="minisblack")
            source = HybridTimelineSource(
                self.client, scans(2), local_stack=path, max_edge=32)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       side_effect=AssertionError("local stack should win")):
                frame = source.get_frame("A1", 0, 1, 1)
            self.assertTrue(np.all(frame == 7))
            self.assertEqual(source.network_fetches, 0)
            self.assertEqual(source.frame_info("A1", 0, 1, 1).source,
                             "local stack")
            source.close()

    def test_export_folder_stacks_are_discovered_by_well_and_channel(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "VID38_A1_phase_timestack.tif"
            tifffile.imwrite(path, np.stack([
                np.full((10, 12), 2, np.uint16),
                np.full((10, 12), 5, np.uint16),
            ]), photometric="minisblack")
            (Path(tmp) / "pyincucyte-manifest.json").write_text(
                '{"files": [{"path": "VID38_A1_phase_timestack.tif", '
                '"vessel_id": 38, "well": "A1", '
                '"scan_times": ["2026-01-01T00:00:00", '
                '"2026-01-01T01:00:00"]}]}', encoding="utf-8")
            mapping = discover_local_stacks(tmp, 38, ["A1"], [1])
            self.assertEqual(Path(mapping[("A1", 1)]["path"]), path)
            source = HybridTimelineSource(
                self.client, scans(2), local_stack=mapping, max_edge=32)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       side_effect=AssertionError("exported stack should win")):
                self.assertTrue(np.all(source.get_frame("A1", 0, 1, 1) == 5))
            source.close()

    def test_partial_multichannel_stack_uses_its_own_timestamp_stride(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "VID38_A1_phase-green_timestack.tif"
            tifffile.imwrite(path, np.stack([
                np.full((10, 12), 10, np.uint16),  # first time, phase
                np.full((10, 12), 11, np.uint16),  # first time, green
                np.full((10, 12), 20, np.uint16),  # last time, phase
                np.full((10, 12), 21, np.uint16),  # last time, green
            ]), photometric="minisblack")
            timeline_scans = scans(3)
            mapping = {("A1", 1): {
                "path": path, "channels": (1, 2),
                "scan_times": [timeline_scans[0].scan_time,
                               timeline_scans[2].scan_time],
            }}
            source = HybridTimelineSource(
                self.client, timeline_scans, local_stack=mapping, max_edge=32)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       side_effect=AssertionError("local stack should win")):
                frame = source.get_frame("A1", 0, 1, 2)
            self.assertTrue(np.all(frame == 20))
            source.close()

    def test_render_cache_is_bounded_and_includes_contrast(self):
        source = HybridTimelineSource(self.client, scans(5), render_cache=2)
        with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                   return_value=(tiff_bytes(value=9), None)):
            source.render_frame("A1", 0, 1, 0, contrast="auto")
            source.render_frame("A1", 0, 1, 0, contrast="raw")
            source.render_frame("A1", 0, 1, 1, contrast="auto")
        self.assertEqual(source.render_cache_size, 2)

    def test_public_handle_serialises_without_pixel_arrays(self):
        source = HybridTimelineSource(self.client, scans(2))
        preview = TimelinePreview(source, well="A1", site=0, channel=1)
        payload = preview.to_dict()
        self.assertEqual(payload["frame_count"], 2)
        self.assertNotIn("array", str(payload))

    def test_client_api_returns_a_primed_timeline_handle(self):
        with TemporaryDirectory() as tmp:
            client = IncucyteClient("10.0.0.1", store=logged_in_store(Path(tmp)))
            target = scans(5)
            with patch("pyincucyte.timeline.engine._fetch_scan_vessel_image_bytes",
                       return_value=(tiff_bytes(value=11), None)):
                preview = client.timeline(target, wells="A1", channels="phase",
                                          anchors=3)
            self.assertIsInstance(preview, TimelinePreview)
            self.assertEqual(preview.frame_count, 5)
            self.assertEqual(len(preview.initial_indices), 3)
            preview.close()


if __name__ == "__main__":
    unittest.main()
