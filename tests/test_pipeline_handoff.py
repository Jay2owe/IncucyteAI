"""The fields the SCN pipeline's next stage reads out of the manifest.

Auto-Organotypic reads a folder of stacks plus ``pyincucyte-manifest.json`` and
outlines the suprachiasmatic nucleus in each.  Everything tested here exists so
that step does not have to guess: which plane is the red channel, whether the
file has stopped growing, how fast the run was sampled and how big a pixel is.

Nothing here touches a written pixel or a filename.
"""

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.manifest import INDEX_FILENAME, MANIFEST_FILENAME, load_manifest

from fakes import FakeDevice, logged_in_store, patched, vessel_record


class HandoffTestCase(unittest.TestCase):
    """A fake 24-well plate scanned twice on 2026-03-01, phase + GFP + mCherry."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = logged_in_store(self.tmp)
        self.device = FakeDevice(channels=(1, 2, 3))
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **changes):
        base = dict(output=str(self.tmp / "out"), vessels=[38],
                    channels="phase,green", start_from="2026-03-01",
                    end_at="2026-03-01")
        base.update(changes)
        return ExportOptions(**base)

    def entries(self, **changes):
        """Download and return the manifest's file entries."""
        with patched(self.device):
            result = self.client.download(self.options(**changes))
        manifest = load_manifest(result.manifest_path)
        return result, manifest, manifest["files"]


class ChannelIndexTests(HandoffTestCase):
    """Item 1: the one-based index is written down, not left to be inferred."""

    def test_each_file_says_which_plane_each_channel_is(self):
        _, _, entries = self.entries(layout="time_channel_stack")
        records = entries[0]["channels"]
        self.assertEqual([c["index"] for c in records], [1, 2])
        self.assertEqual([c["name"] for c in records], ["Phase", "GFP"])
        self.assertEqual([c["image_type"] for c in records], [1, 2])
        self.assertTrue(all(c["source"] for c in records))

    def test_the_index_describes_the_stack_and_not_the_device_numbering(self):
        # Red is ImageType 3 but the second plane written, because phase was
        # not asked for.  A consumer computing scn_channel from the device's
        # own numbering would land on plane 3, which does not exist.
        _, _, entries = self.entries(layout="time_channel_stack",
                                     channels="green,red")
        records = entries[0]["channels"]
        self.assertEqual([(c["index"], c["image_type"]) for c in records],
                         [(1, 2), (2, 3)])
        red = [c for c in records if c["name"] == "mCherry"][0]
        self.assertEqual(red["index"], 2)

    def test_the_names_are_still_summarised_for_a_human_reader(self):
        # The stats block and the CSV column are read by people, so they keep
        # the names; the per-file records keep the numbering.
        _, manifest, entries = self.entries(layout="time_channel_stack")
        self.assertEqual(entries[0]["image_types"], [1, 2])
        self.assertEqual(sorted(manifest["stats"]["channels"]),
                         ["GFP", "Phase"])

    def test_the_object_offers_the_names_without_unpacking_the_records(self):
        with patched(self.device):
            result = self.client.download(
                self.options(layout="time_channel_stack"))
        self.assertEqual(result.files[0].channel_names, ["Phase", "GFP"])


class CompletenessTests(HandoffTestCase):
    """Item 2: say whether the file has stopped growing."""

    def test_a_per_scan_file_is_finished_the_moment_it_lands(self):
        _, _, entries = self.entries(layout="separate")
        self.assertTrue(all(e["complete"] for e in entries))

    def test_an_open_ended_time_stack_says_nothing_rather_than_guessing(self):
        # end_at="now" slides with the clock, so nobody can say whether the
        # instrument will scan again.  null, and not false: a consumer skips
        # what is still filling and must not skip every stack.
        _, _, entries = self.entries(layout="time_channel_stack", end_at="now")
        self.assertTrue(all(e["complete"] is None for e in entries))

    def test_a_time_stack_whose_stated_window_has_closed_is_finished(self):
        _, _, entries = self.entries(layout="time_channel_stack",
                                     end_at="2026-03-01")
        self.assertTrue(all(e["complete"] is True for e in entries))

    def test_a_relative_end_still_moves_so_it_cannot_be_called_finished(self):
        _, _, entries = self.entries(layout="time_channel_stack",
                                     start_from="2026-03-01", end_at="+72h")
        self.assertTrue(all(e["complete"] is None for e in entries))

    def test_a_well_that_missed_a_timepoint_is_visible_as_such(self):
        # Both scans exist, but the second only got as far as A1 - so A2's
        # stack is two frames short of the run without being a short
        # recording.  The frame count alone cannot tell those apart.
        self.device.scans = ["2026-03-01T09:00:00", "2026-03-02T09:00:00"]
        self.device.wells_for = {"2026-03-02T09:00:00": [(0, 0)]}
        _, _, entries = self.entries(layout="time_channel_stack",
                                     end_at="2026-03-02")
        by_well = {e["well"]: e for e in entries}
        self.assertEqual(by_well["A1"]["frames_expected"], 2)
        self.assertEqual(by_well["A1"]["frames"], 2)
        self.assertEqual(by_well["A2"]["frames_expected"], 2)
        self.assertEqual(by_well["A2"]["frames"], 1)

    def test_the_shortfall_is_readable_without_doing_the_subtraction(self):
        self.device.scans = ["2026-03-01T09:00:00", "2026-03-02T09:00:00"]
        self.device.wells_for = {"2026-03-02T09:00:00": [(0, 0)]}
        with patched(self.device):
            result = self.client.download(self.options(
                layout="time_channel_stack", end_at="2026-03-02"))
        missing = {f.well: f.missing_frames for f in result.files}
        self.assertEqual(missing, {"A1": 0, "A2": 1})

    def test_blank_planes_are_stated_as_none_rather_than_left_out(self):
        # Nothing is written until every plane is downloaded and checked, so
        # this is a fact about this package, not a placeholder.
        _, _, entries = self.entries(layout="time_channel_stack")
        self.assertTrue(all(e["blank_planes"] == 0 for e in entries))


class WatchCompletenessTests(HandoffTestCase):
    """A manifest written mid-watch says false; one written after says true."""

    def test_a_poll_marks_what_it_writes_as_still_filling(self):
        with patched(self.device):
            watcher = self.client.watch(self.options(layout="time_channel_stack"),
                                        start=False)
            result = watcher.poll_once()
        self.assertTrue(result.files)
        self.assertTrue(all(f.complete is False for f in result.files))

    def test_the_flush_that_ends_the_run_marks_them_finished(self):
        with patched(self.device):
            watcher = self.client.watch(self.options(layout="time_channel_stack"),
                                        start=False)
            watcher.poll_once()
            self.device.scans.append("2026-03-01T15:00:00")
            watcher.stop()
            result = watcher.flush()
        self.assertTrue(result.files)
        self.assertTrue(all(f.complete is True for f in result.files))

        manifest = load_manifest(self.tmp / "out" / MANIFEST_FILENAME)
        self.assertTrue(all(e["complete"] is True for e in manifest["files"]))

    def test_a_flush_that_is_not_the_end_does_not_claim_to_be(self):
        with patched(self.device):
            watcher = self.client.watch(self.options(layout="time_channel_stack"),
                                        start=False)
            result = watcher.flush(final=False)
        self.assertTrue(all(f.complete is False for f in result.files))


class DerivedValueTests(HandoffTestCase):
    """Item 3: a cadence and a pixel size, each saying where it came from."""

    def test_the_interval_comes_off_the_files_own_frames(self):
        _, _, entries = self.entries(layout="time_channel_stack")
        interval = entries[0]["interval_s"]
        self.assertEqual(interval["value"], 3 * 3600)
        self.assertIn("scan times", interval["source"])

    def test_a_single_frame_file_falls_back_to_the_runs_cadence_and_says_so(self):
        _, _, entries = self.entries(layout="separate")
        interval = entries[0]["interval_s"]
        self.assertEqual(interval["value"], 3 * 3600)
        self.assertIn("run's scan times", interval["source"])

    def test_a_pause_does_not_drag_the_interval_with_it(self):
        # An instrument that stopped overnight leaves one enormous gap; a mean
        # over it describes no cadence the run ever used.
        self.device.scans = ["2026-03-01T09:00:00", "2026-03-01T12:00:00",
                             "2026-03-01T15:00:00", "2026-03-02T21:00:00"]
        _, _, entries = self.entries(layout="time_channel_stack",
                                     end_at="2026-03-02")
        self.assertEqual(entries[0]["interval_s"]["value"], 3 * 3600)

    def test_one_timestamp_gives_a_reason_rather_than_a_number(self):
        self.device.scans = ["2026-03-01T09:00:00"]
        _, _, entries = self.entries(layout="time_channel_stack")
        interval = entries[0]["interval_s"]
        self.assertIsNone(interval["value"])
        self.assertIn("fewer than two", interval["source"])

    def test_the_pixel_size_is_read_off_the_vessel_not_inferred(self):
        _, _, entries = self.entries(layout="time_channel_stack")
        pixel = entries[0]["pixel_size_um"]
        self.assertAlmostEqual(pixel["value"], 2.824051)
        self.assertIn("read from", pixel["source"])

    def test_a_vessel_without_one_says_why_rather_than_guessing(self):
        self.device.vessels = [vessel_record(microns_per_pixel=None)]
        _, _, entries = self.entries(layout="time_channel_stack")
        pixel = entries[0]["pixel_size_um"]
        self.assertIsNone(pixel["value"])
        self.assertIn("MicronsPerPixel", pixel["source"])

    def test_the_vessel_itself_carries_it_for_a_caller_who_asks_directly(self):
        with patched(self.device):
            vessel = self.client.vessel(38)
        self.assertAlmostEqual(vessel.pixel_size_um, 2.824051)
        self.assertAlmostEqual(vessel.to_dict()["pixel_size_um"], 2.824051)


class PerFileCallbackTests(HandoffTestCase):
    """Item 4: drive the next step file by file, not poll by poll."""

    def test_download_hands_over_each_file_as_it_lands(self):
        seen = []
        with patched(self.device):
            result = self.client.download(self.options(), on_file=seen.append)
        self.assertEqual(len(seen), result.file_count)
        self.assertEqual([f.path for f in seen], result.paths)
        self.assertTrue(all(f.well for f in seen))

    def test_fetch_passes_the_callback_through(self):
        seen = []
        with patched(self.device):
            result = self.client.fetch(self.options(), on_file=seen.append)
        self.assertEqual(len(seen), result.file_count)

    def test_a_watcher_reports_files_before_the_poll_finishes(self):
        seen = []
        with patched(self.device):
            watcher = self.client.watch(self.options(), start=False,
                                        on_file=seen.append)
            result = watcher.poll_once()
        self.assertEqual(len(seen), result.file_count)
        self.assertGreater(len(seen), 1)

    def test_a_broken_callback_does_not_kill_the_download(self):
        def explode(_):
            raise RuntimeError("downstream fell over")

        with patched(self.device):
            result = self.client.download(self.options(), on_file=explode)
        self.assertEqual(result.file_count, 8)
        self.assertEqual(result.errors, [])


class SeparateLayoutTests(HandoffTestCase):
    """Item 5: leave layout="separate" exactly as it is.

    It writes one YX TIFF per well per channel per timepoint, which the SCN
    step would treat as thousands of recordings.  Refusing it is Auto-Organotypic's
    job, and ``axes: "YX"`` is the field that lets it - so that field is what
    this guards.  No warning belongs here.
    """

    def test_a_per_image_folder_stays_detectable_by_its_axes(self):
        _, manifest, entries = self.entries(layout="separate")
        self.assertEqual(manifest["axes"], "YX")
        self.assertTrue(all(e["axes"] == "YX" for e in entries))


class ManifestSummaryTests(HandoffTestCase):
    """The folder's own answer to "can the SCN step run on this yet?"."""

    def test_the_stats_count_finished_filling_and_unstated_separately(self):
        _, manifest, _ = self.entries(layout="time_channel_stack",
                                      end_at="2026-03-01")
        stats = manifest["stats"]
        self.assertEqual(stats["complete_files"], stats["file_count"])
        self.assertEqual(stats["filling_files"], 0)
        self.assertEqual(stats["unstated_files"], 0)

    def test_a_watched_folder_counts_its_files_as_still_filling(self):
        with patched(self.device):
            watcher = self.client.watch(self.options(layout="time_channel_stack"),
                                        start=False)
            watcher.poll_once()
        stats = load_manifest(self.tmp / "out" / MANIFEST_FILENAME)["stats"]
        self.assertEqual(stats["filling_files"], stats["file_count"])
        self.assertEqual(stats["complete_files"], 0)


class IndexCsvTests(HandoffTestCase):
    """The flat index carries the same facts, provenance included."""

    def test_the_csv_splits_a_derived_value_into_value_and_source(self):
        self.entries(layout="time_channel_stack", end_at="2026-03-01")
        with (self.tmp / "out" / INDEX_FILENAME).open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        row = rows[0]
        self.assertEqual(float(row["pixel_size_um"]), 2.824051)
        self.assertIn("read from", row["pixel_size_um_source"])
        self.assertEqual(float(row["interval_s"]), 3 * 3600)
        self.assertIn("scan times", row["interval_s_source"])
        self.assertEqual(row["complete"], "True")
        self.assertEqual(row["frames_expected"], "2")


if __name__ == "__main__":
    unittest.main()
