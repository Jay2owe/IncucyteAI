"""Finding a vessel and looking at it, against the fake device.

Two questions are being tested.  Does ``find_scans`` land on the scan a person
would have picked by hand - the newest one that actually holds images for that
plate, found without sweeping every day since the experiment started?  And does
``preview`` turn that into labelled thumbnails without downloading the world?
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from PIL import Image

from pyincucyte import IncucyteClient, PreviewSet, VesselScan
from pyincucyte import cli, preview as preview_mod
from pyincucyte.pyramid import PyramidFrame

from fakes import FakeDevice, logged_in_store, patched, vessel_record


def gradient_tiff(shape=(40, 60), dtype=np.uint16, low=100, high=900):
    """A TIFF that is dim but not flat - what a fluorescence frame looks like."""
    ramp = np.linspace(low, high, shape[0] * shape[1], dtype="float64")
    buffer = io.BytesIO()
    Image.fromarray(ramp.reshape(shape).astype(dtype)).save(buffer, format="TIFF")
    return buffer.getvalue()


class PreviewTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = logged_in_store(self.tmp)
        self.device = FakeDevice()
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def days_queried(self):
        """Which calendar days the device was asked about, in order."""
        return [f"{p['Year']:04d}-{p['Month']:02d}-{p['Day']:02d}"
                for route, p in self.device.calls if route == "Scans/AllScanTimes"]


# ---------------------------------------------------------------------------
# finding
# ---------------------------------------------------------------------------

class FindScansTests(PreviewTestCase):
    def test_most_recent_scan_comes_back_resolved(self):
        with patched(self.device):
            scans = self.client.find_scans(most_recent=1)
        self.assertEqual(len(scans), 1)
        scan = scans[0]
        self.assertIsInstance(scan, VesselScan)
        self.assertEqual(scan.vessel_id, 38)
        self.assertEqual(scan.scan_time, "2026-03-01T12:00:00")
        self.assertEqual(scan.wells, {(0, 0), (0, 1)})
        self.assertEqual(scan.channels, {1, 2})
        self.assertEqual(scan.well_names, ["A1", "A2"])
        self.assertEqual(scan.elapsed, "00d03h00m")

    def test_days_are_walked_back_from_the_vessels_last_scan(self):
        # The vessel's last scan is 2026-03-03; today is years later.  Starting
        # from today would sweep every day since.
        with patched(self.device):
            self.client.find_scans(most_recent=1)
        self.assertEqual(self.days_queried(),
                         ["2026-03-03", "2026-03-02", "2026-03-01"])

    def test_the_walk_stops_at_the_vessels_own_first_scan(self):
        self.device.scans = []
        with patched(self.device):
            scans = self.client.find_scans(most_recent=1)
        self.assertEqual(scans, [])
        self.assertEqual(self.days_queried(),
                         ["2026-03-03", "2026-03-02", "2026-03-01"])

    def test_a_scan_that_does_not_contain_this_vessel_is_skipped(self):
        self.device.missing_scans = {"2026-03-01T12:00:00"}
        with patched(self.device):
            scans = self.client.find_scans(most_recent=1)
        self.assertEqual([s.scan_time for s in scans], ["2026-03-01T09:00:00"])

    def test_most_recent_n_returns_them_newest_first(self):
        with patched(self.device):
            scans = self.client.find_scans(most_recent=2)
        self.assertEqual([s.scan_time for s in scans],
                         ["2026-03-01T12:00:00", "2026-03-01T09:00:00"])

    def test_since_trims_the_older_end(self):
        with patched(self.device):
            scans = self.client.find_scans(most_recent=5,
                                           since="2026-03-01 10:00")
        self.assertEqual([s.scan_time for s in scans], ["2026-03-01T12:00:00"])

    def test_at_picks_the_nearest_scan_either_side(self):
        with patched(self.device):
            scans = self.client.find_scans(at="2026-03-01 09:30")
        self.assertEqual([s.scan_time for s in scans], ["2026-03-01T09:00:00"])

    def test_name_matches_a_substring_and_a_bare_number_is_an_id(self):
        self.device.vessels = [vessel_record(38, name="Cry1-GFP U2OS"),
                               vessel_record(41, name="Bmal1 control")]
        with patched(self.device):
            self.assertEqual([s.vessel_id for s in self.client.find_scans("cry1")],
                             [38])
            self.assertEqual([s.vessel_id for s in self.client.find_scans("41")],
                             [41])

    def test_plate_owner_and_channel_filters_narrow_the_list(self):
        self.device.vessels = [
            vessel_record(38, name="Green plate", color1="GFP"),
            vessel_record(41, name="Red plate", plate="Corning 96-well",
                          color1="YFP"),
        ]
        with patched(self.device):
            self.assertEqual(
                [v.id for v in self.client.find_vessels(plate=24)], [38])
            self.assertEqual(
                [v.id for v in self.client.find_vessels(plate="96-well")], [41])
            self.assertEqual(
                [v.id for v in self.client.find_vessels(channel="GFP")], [38])
            self.assertEqual(
                [v.id for v in self.client.find_vessels(channel="green")],
                [41, 38])
            self.assertEqual(
                [v.id for v in self.client.find_vessels(owner="TEST")], [41, 38])

    def test_a_scan_serialises_to_json_for_a_pipeline(self):
        with patched(self.device):
            scan = self.client.find_scans()[0]
        payload = json.loads(json.dumps(scan.to_dict(), default=str))
        self.assertEqual(payload["vessel_id"], 38)
        self.assertEqual(payload["wells"], ["A1", "A2"])
        self.assertEqual(payload["channel_names"], ["Phase", "GFP"])


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

class RenderTests(unittest.TestCase):
    def test_autoscale_makes_a_dim_16_bit_frame_visible(self):
        raw = np.linspace(100, 900, 600, dtype="uint16").reshape(20, 30)
        stretched = preview_mod.autoscale(raw)
        self.assertEqual(stretched.dtype, np.uint8)
        self.assertEqual((stretched.min(), stretched.max()), (0, 255))

    def test_raw_keeps_the_bit_depth_scaling(self):
        raw = np.linspace(100, 900, 600, dtype="uint16").reshape(20, 30)
        self.assertLess(preview_mod.autoscale(raw, contrast=None).max(), 10)

    def test_a_flat_frame_does_not_divide_by_zero(self):
        flat = np.full((8, 8), 7, dtype="uint16")
        self.assertEqual(preview_mod.autoscale(flat).max(), 0)

    def test_thumbnails_are_no_bigger_than_asked_for(self):
        small = preview_mod.thumbnail(gradient_tiff((400, 600)), size=64)
        self.assertLessEqual(max(small.shape), 64)
        self.assertEqual(small.dtype, np.uint8)


# ---------------------------------------------------------------------------
# previewing
# ---------------------------------------------------------------------------

class PreviewTests(PreviewTestCase):
    def test_one_tile_per_well_and_channel_with_the_vessels_own_names(self):
        with patched(self.device):
            result = self.client.preview(vessel=38)
        self.assertIsInstance(result, PreviewSet)
        self.assertEqual([image.label for image in result.images],
                         ["A1 - Phase", "A1 - GFP", "A2 - Phase", "A2 - GFP"])
        self.assertEqual(result.channels_present(), ["Phase", "GFP"])
        self.assertTrue(all(image.ok for image in result.images))
        self.assertIn("Vessel 38", result.title)

    def test_wells_and_channels_narrow_the_request(self):
        with patched(self.device):
            result = self.client.preview(vessel=38, wells="A2", channels="phase")
        self.assertEqual([image.label for image in result.images], ["A2 - Phase"])

    def test_wells_the_scan_does_not_hold_are_never_asked_for(self):
        with patched(self.device):
            result = self.client.preview(vessel=38, wells="A1,D6")
        self.assertEqual(sorted({image.well for image in result.images}), ["A1"])
        self.assertFalse(any("D6" in str(name) for name in self.device.fetches))

    def test_the_image_cap_stops_the_download_and_is_reported(self):
        with patched(self.device):
            result = self.client.preview(vessel=38, max_images=2)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.skipped, 2)
        self.assertIn("not fetched", result.summary())

    def test_a_second_look_is_served_from_the_cache(self):
        with patched(self.device):
            self.client.preview(vessel=38)
            first = len(self.device.fetches)
            result = self.client.preview(vessel=38)
        self.assertEqual(len(self.device.fetches), first)
        self.assertTrue(all(image.cached for image in result.images))
        self.assertEqual(result.count, 4)

    def test_advertised_pyramid_tiles_avoid_the_full_tiff_route(self):
        class Tiles:
            capabilities = {}

            def fetch_frame(self, request, level, cancel=None):
                return PyramidFrame(
                    array=np.full((24, 32), request["img_type"], np.uint16),
                    source_bytes=400, level=level.level, tile_count=1)

        with patched(self.device):
            scan = self.client.find_scans(vessel=38)[0]
        scan.pyramid_levels = [
            {"Level0": 3, "Size_pixels": {"Width": 32, "Height": 24}}]
        self.client._preview_transport = Tiles()
        with patch("pyincucyte.preview.engine._fetch_scan_vessel_image_bytes",
                   side_effect=AssertionError("pyramid tile should win")):
            result = self.client.preview(scan, wells="A1", channels="phase")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.images[0].source_bytes, 400)

    def test_read_only_tile_probe_reports_shapes_but_not_pixels(self):
        class ProbeTiles:
            capabilities = {"phase": True, "colour": True}

            def fetch_frame(self, request, level, cancel=None):
                return PyramidFrame(
                    array=np.full((level.height, level.width),
                                  request["img_type"], np.uint16),
                    source_bytes=300 + level.level,
                    level=level.level, tile_count=1)

        original = self.device.api_post

        def with_levels(host, token, route, payload=None, timeout=None):
            response = original(host, token, route, payload, timeout)
            if route == "Vessels/GetScanVessel":
                response["Data"]["ImagePyramidLevels"] = {"$values": [
                    {"Level0": 1,
                     "Size_pixels": {"Width": 8, "Height": 6}},
                    {"Level0": 2,
                     "Size_pixels": {"Width": 4, "Height": 3}},
                ]}
            return response

        self.device.api_post = with_levels
        self.client._preview_transport = ProbeTiles()
        with patched(self.device):
            scan = self.client.find_scans(vessel=38)[0]
            report = self.client.probe_preview_tiles(scan, wells="A1")
        self.assertEqual(report["lowest_resolution_level"], 2)
        self.assertEqual(report["channels"]["1"]["levels"][0]
                         ["decoded_shape"], [6, 8])
        self.assertGreater(report["channels"]["1"]["full_tiff_bytes"], 0)
        self.assertNotIn("array", str(report).lower())
        self.assertNotIn("bytes': b", str(report).lower())

    def test_one_unreadable_image_does_not_cost_the_others(self):
        def broken(host, token, item, max_retries=3):
            if item["img_type"] == 2:
                return None, "SKIP: no image data in response"
            return self.device.fetch_image(host, token, item)

        with patched(self.device):
            preview_mod.engine._fetch_scan_vessel_image_bytes = broken
            result = self.client.preview(vessel=38)
        self.assertEqual(result.count, 2)
        self.assertEqual(len(result.errors), 2)
        self.assertIn("A1 - GFP", result.errors[0])

    def test_a_scan_previews_itself(self):
        with patched(self.device):
            scan = self.client.find_scans(vessel=38)[0]
            result = scan.preview(wells="A1", channels="phase")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.scans, [scan])

    def test_saving_writes_one_png_per_tile(self):
        with patched(self.device):
            result = self.client.preview(vessel=38, wells="A1")
        paths = result.save(self.tmp / "thumbs")
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertTrue(path.exists())
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")

    def test_the_result_serialises_for_a_json_pipeline(self):
        with patched(self.device):
            result = self.client.preview(vessel=38, wells="A1", channels="phase")
        payload = json.loads(json.dumps(result.to_dict(), default=str))
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(payload["images"][0]["well"], "A1")
        self.assertEqual(payload["images"][0]["channel"], "Phase")

    def test_several_scans_are_ordered_by_well_then_time(self):
        # Reading a time course means one well across time, not one moment
        # across wells - and the cap has to leave the older scans a share.
        self.device.scans = ["2026-03-01T09:00:00", "2026-03-01T12:00:00",
                             "2026-03-02T09:00:00"]
        with patched(self.device):
            scans = self.client.find_scans(vessel=38, most_recent=3)
            result = self.client.preview(scans, channels="phase", max_images=4)
        self.assertEqual(
            [(image.well, image.scan_time) for image in result.images],
            [("A1", "2026-03-02T09:00:00"), ("A1", "2026-03-01T12:00:00"),
             ("A1", "2026-03-01T09:00:00"), ("A2", "2026-03-02T09:00:00")])
        self.assertEqual(result.skipped, 2)

    def test_nothing_matched_is_an_empty_set_not_an_explosion(self):
        self.device.scans = []
        with patched(self.device):
            result = self.client.preview(vessel=38)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.requested, 0)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

class PreviewCliTests(PreviewTestCase):
    def run_cli(self, *argv):
        args = ["--config-file", str(self.tmp / "credentials.json"),
                "--host", "10.0.0.1", "--quiet", *argv]
        buffer = io.StringIO()
        with patched(self.device), redirect_stdout(buffer):
            code = cli.main(args)
        return code, buffer.getvalue()

    def test_find_lists_the_newest_scan_per_vessel(self):
        code, output = self.run_cli("find")
        self.assertEqual(code, 0)
        self.assertIn("2026-03-01 12:00", output)
        self.assertIn("Phase + GFP", output)

    def test_find_json_is_machine_readable(self):
        code, output = self.run_cli("--json", "find", "--most-recent", "2")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual([entry["scan_time"] for entry in payload],
                         ["2026-03-01T12:00:00", "2026-03-01T09:00:00"])

    def test_preview_saves_pngs_without_opening_a_window(self):
        target = self.tmp / "cli-thumbs"
        code, output = self.run_cli("preview", "-v", "38", "-w", "A1",
                                    "-c", "phase", "--no-show",
                                    "--save", str(target))
        self.assertEqual(code, 0)
        self.assertIn("A1 - Phase", output)
        self.assertEqual(len(list(target.glob("*.png"))), 1)

    def test_preview_json_reports_every_tile(self):
        code, output = self.run_cli("--json", "preview", "-v", "38", "-w", "A1",
                                    "--no-show", "--size", "48")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(payload["size"], 48)
        self.assertTrue(all(tile["width"] <= 48 for tile in payload["images"]))

    def test_timeline_command_is_bounded_and_machine_readable(self):
        code, output = self.run_cli(
            "--json", "timeline", "-v", "38", "-w", "A1", "-c", "phase",
            "--since", "2026-03-01", "--until", "2026-03-01",
            "--anchors", "2", "--no-show")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["frame_count"], 2)
        self.assertEqual(payload["network_fetches"], 2)


if __name__ == "__main__":
    unittest.main()
