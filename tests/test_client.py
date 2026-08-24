"""End-to-end tests of the importable API against a fake device."""

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import tifffile

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.manifest import INDEX_FILENAME, MANIFEST_FILENAME, load_manifest
from pyincucyte.models import ProgressEvent

from fakes import FakeDevice, logged_in_store, patched, vessel_record


class ClientTestCase(unittest.TestCase):
    """Shared setup: a temp folder, a saved login, and a fake instrument."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = logged_in_store(self.tmp)
        self.device = FakeDevice()
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **changes):
        base = dict(output=str(self.tmp / "out"), vessels=[38],
                    channels="phase", start_from="2026-03-01",
                    end_at="2026-03-01")
        base.update(changes)
        return ExportOptions(**base)


class VesselTests(ClientTestCase):
    def test_vessels_are_typed_and_carry_plate_geometry(self):
        with patched(self.device):
            vessels = self.client.vessels()
        self.assertEqual(len(vessels), 1)
        vessel = vessels[0]
        self.assertEqual(vessel.id, 38)
        self.assertEqual((vessel.rows, vessel.cols), (4, 6))
        self.assertEqual(vessel.plate_format, "24-well")
        self.assertEqual(vessel.channel_labels[2], "GFP")
        self.assertEqual(vessel.active_channels, {1, 2, 3})
        self.assertEqual(vessel.first_scan, datetime(2026, 3, 1, 9, 0))

    def test_duplicate_vessel_records_are_collapsed(self):
        self.device.vessels = [vessel_record(38), vessel_record(38),
                               vessel_record(41, name="Second")]
        with patched(self.device):
            vessels = self.client.vessels()
        self.assertEqual([v.id for v in vessels], [38, 41])

    def test_vessel_list_is_cached_until_refresh_is_asked_for(self):
        with patched(self.device):
            self.client.vessels()
            self.client.vessels()
            first = sum(1 for route, _ in self.device.calls
                        if route == "Vessels/GetAllSearchVessels")
            self.client.vessels(refresh=True)
            second = sum(1 for route, _ in self.device.calls
                         if route == "Vessels/GetAllSearchVessels")
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)


class PlanTests(ClientTestCase):
    def test_plan_counts_files_without_fetching_any_pixels(self):
        with patched(self.device):
            plan = self.client.plan(self.options())
        # 2 wells x 1 channel x 2 scan times
        self.assertEqual(plan.output_file_count, 4)
        self.assertEqual(plan.source_image_count, 4)
        self.assertEqual(plan.axes, "YX")
        self.assertEqual(self.device.fetches, [])
        self.assertGreater(plan.estimated_bytes, 0)
        self.assertIn("output file", plan.summary())

    def test_time_stack_layout_makes_one_file_per_well_and_channel(self):
        with patched(self.device):
            plan = self.client.plan(self.options(layout="time_stack",
                                                 channels="phase,green"))
        self.assertEqual(plan.output_file_count, 4)      # 2 wells x 2 channels
        self.assertEqual(plan.source_image_count, 8)     # x 2 scan times
        self.assertEqual(plan.axes, "TYX")

    def test_time_channel_stack_makes_one_file_per_well(self):
        with patched(self.device):
            plan = self.client.plan(self.options(layout="time_channel_stack",
                                                 channels="phase,green"))
        self.assertEqual(plan.output_file_count, 2)
        self.assertEqual(plan.source_image_count, 8)
        self.assertEqual(plan.axes, "TCYX")

    def test_well_filter_restricts_the_plan(self):
        self.device.wells = [(0, 0), (0, 1), (1, 0)]
        with patched(self.device):
            plan = self.client.plan(self.options(wells="A1"))
        self.assertEqual(plan.output_file_count, 2)      # 1 well x 2 scans
        self.assertEqual(plan.wells_by_vessel[38], {(0, 0)})

    def test_scan_times_missing_this_vessel_are_skipped_not_fatal(self):
        self.device.missing_scans = {"2026-03-01T12:00:00"}
        with patched(self.device):
            plan = self.client.plan(self.options())
        self.assertEqual(plan.output_file_count, 2)

    def test_progress_events_describe_each_stage(self):
        seen = []
        with patched(self.device):
            self.client.plan(self.options(), progress=seen.append)
        self.assertTrue(all(isinstance(e, ProgressEvent) for e in seen))
        self.assertIn("scanning", {e.stage for e in seen})
        self.assertIn("planning", {e.stage for e in seen})

    def test_keyword_form_matches_the_options_object(self):
        with patched(self.device):
            by_kwargs = self.client.plan(vessel=38, output=str(self.tmp / "out"),
                                         channels="phase",
                                         start_from="2026-03-01",
                                         end_at="2026-03-01")
        self.assertEqual(by_kwargs.output_file_count, 4)

    def test_missing_output_is_rejected_before_any_call(self):
        with patched(self.device):
            with self.assertRaises(ValueError):
                self.client.plan(self.options(output=""))

    def test_unknown_option_name_is_reported(self):
        with self.assertRaises(TypeError):
            self.client.make_options(None, wels="A1")


class DownloadTests(ClientTestCase):
    def test_download_writes_files_and_describes_each_one(self):
        with patched(self.device):
            result = self.client.download(self.options(channels="phase,green"))

        self.assertTrue(result.ok)
        self.assertEqual(result.file_count, 8)
        self.assertTrue(all(path.exists() for path in result.paths))

        one = sorted(result.files, key=lambda f: f.path.name)[0]
        self.assertEqual(one.vessel_id, 38)
        self.assertIn(one.well, ("A1", "A2"))
        self.assertEqual(one.layout, "separate")
        self.assertEqual(one.axes, "YX")
        self.assertEqual(len(one.channels), 1)
        self.assertIn(one.channel_names[0], ("Phase", "GFP"))
        self.assertEqual(one.channels[0]["index"], 1)
        self.assertGreater(one.bytes, 0)
        self.assertRegex(one.elapsed, r"^\d\dd\d\dh\d\dm$")

    def test_channel_names_come_from_the_vessel_not_the_device_defaults(self):
        with patched(self.device):
            result = self.client.download(self.options(channels="green"))
        self.assertEqual({name for f in result.files for name in f.channel_names},
                         {"GFP"})

    def test_time_channel_stack_writes_a_readable_imagej_tcyx_file(self):
        with patched(self.device):
            result = self.client.download(
                self.options(layout="time_channel_stack", channels="phase,green"))
        self.assertEqual(result.file_count, 2)
        with tifffile.TiffFile(result.files[0].path) as handle:
            self.assertTrue(handle.is_imagej)
            self.assertEqual(handle.series[0].shape, (2, 2, 6, 8))
            self.assertEqual(handle.imagej_metadata["Labels"],
                             ["Phase", "GFP"] * 2)
        written = result.files[0]
        self.assertEqual(written.axes, "TCYX")
        self.assertEqual(written.channel_names, ["Phase", "GFP"])
        self.assertEqual(len(written.scan_times), 2)

    def test_a_second_run_downloads_nothing_new(self):
        with patched(self.device):
            first = self.client.download(self.options())
            second = self.client.download(self.options())
        self.assertEqual(first.file_count, 4)
        self.assertEqual(second.file_count, 0)
        self.assertTrue(second.plan.is_empty)

    def test_failures_are_collected_rather_than_raised(self):
        def failing(host, token, item, max_retries=3):
            return None, f"SKIP {item['fname']}: no image data in response"

        with patched(self.device):
            from pyincucyte import engine
            engine._fetch_scan_vessel_image_bytes = failing
            result = self.client.download(self.options())
        self.assertEqual(result.file_count, 0)
        self.assertEqual(len(result.errors), 4)
        self.assertFalse(result.ok)

    def test_cancelling_stops_the_download(self):
        import threading
        cancel = threading.Event()
        cancel.set()
        with patched(self.device):
            result = self.client.download(self.options(), cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.file_count, 0)


class ManifestTests(ClientTestCase):
    def test_manifest_and_csv_index_land_beside_the_images(self):
        with patched(self.device):
            result = self.client.download(self.options(channels="phase,green"))

        manifest_path = Path(result.manifest_path)
        self.assertTrue(manifest_path.exists())
        self.assertEqual(manifest_path.name, MANIFEST_FILENAME)
        self.assertTrue((manifest_path.parent / INDEX_FILENAME).exists())

        manifest = load_manifest(manifest_path)
        self.assertEqual(manifest["stats"]["file_count"], 8)
        self.assertEqual(sorted(manifest["stats"]["wells"]), ["A1", "A2"])
        self.assertEqual(sorted(manifest["stats"]["channels"]), ["GFP", "Phase"])
        self.assertEqual(manifest["layout"], "separate")
        self.assertEqual(manifest["options"]["channels"], "phase,green")
        entry = manifest["files"][0]
        for key in ("path", "well", "channels", "scan_times", "axes",
                    "first_scan_time", "frames"):
            self.assertIn(key, entry)

    def test_a_later_run_merges_into_the_same_manifest(self):
        with patched(self.device):
            self.client.download(self.options(channels="phase"))
            self.device.scans.append("2026-03-02T09:00:00")
            self.client.download(self.options(channels="phase",
                                              end_at="2026-03-02"))
        manifest = load_manifest(self.tmp / "out" / MANIFEST_FILENAME)
        self.assertEqual(manifest["stats"]["file_count"], 6)
        self.assertEqual(len(manifest["runs"]), 2)

    def test_manifest_can_be_switched_off(self):
        with patched(self.device):
            result = self.client.download(self.options(write_manifest=False))
        self.assertIsNone(result.manifest_path)
        self.assertFalse((self.tmp / "out" / MANIFEST_FILENAME).exists())


class WatcherTests(ClientTestCase):
    def test_one_poll_downloads_and_reports_back(self):
        received = []
        with patched(self.device):
            watcher = self.client.watch(self.options(), start=False,
                                        on_result=received.append)
            result = watcher.poll_once()
        self.assertEqual(result.file_count, 4)
        self.assertEqual(watcher.file_count, 4)
        self.assertEqual(watcher.poll_count, 1)
        self.assertEqual(len(received), 1)

    def test_a_stopped_watcher_reports_itself_as_stopped(self):
        with patched(self.device):
            watcher = self.client.watch(self.options(), start=False)
        self.assertFalse(watcher.is_running)
        watcher.stop()
        self.assertTrue(watcher.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
