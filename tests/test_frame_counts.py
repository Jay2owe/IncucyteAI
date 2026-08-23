"""Windows expressed as a number of frames: '-50f' and '+100 frames'.

A frame is one scan time - one point on a stack's T axis - so a frame count is
independent of how many wells or channels are selected.
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.options import parse_duration, parse_frame_count

from fakes import FakeDevice, logged_in_store, patched, vessel_record

NOW = datetime(2026, 3, 10, 16, 0)
FIRST = datetime(2026, 3, 1, 14, 30)

#: Six-hourly scans across ten days: 2026-03-01 00:00 to 2026-03-10 18:00.
ALL_SCANS = [(datetime(2026, 3, 1) + timedelta(hours=6 * i)).strftime(
    "%Y-%m-%dT%H:%M:%S") for i in range(40)]


def options(start_from="first", end_at=None, **extra):
    return ExportOptions(output="o", vessels=[1], start_from=start_from,
                         end_at=end_at, **extra)


class ParsingTests(unittest.TestCase):
    def test_every_spelling_of_a_frame_count(self):
        for text, expected in (("-50f", -50), ("+100f", 100),
                               ("-24 frames", -24), ("+1 frame", 1),
                               ("-12 scans", -12), ("+3 SCAN", 3)):
            self.assertEqual(parse_frame_count(text), expected, text)

    def test_frame_counts_and_durations_never_collide(self):
        self.assertIsNone(parse_frame_count("-48h"))
        self.assertIsNone(parse_frame_count("2026-03-05"))
        self.assertIsNone(parse_duration("-50f"))
        self.assertIsNone(parse_duration("-50 frames"))
        # "-50s" is fifty seconds; only "scan"/"scans" means frames.
        self.assertIsNotNone(parse_duration("-50s"))
        self.assertIsNone(parse_frame_count("-50s"))

    def test_a_bare_number_is_not_a_frame_count(self):
        self.assertIsNone(parse_frame_count("50"))
        self.assertIsNone(parse_frame_count("50f"))      # sign is required

    def test_zero_frames_is_refused(self):
        with self.assertRaises(ValueError):
            parse_frame_count("+0f")


class DirectionTests(unittest.TestCase):
    def test_start_counts_backwards_and_end_counts_forwards(self):
        self.assertEqual(options("-50f").start_frames, 50)
        self.assertIsNone(options("-50f").end_frames)
        self.assertEqual(options("first", "+100f").end_frames, 100)
        self.assertIsNone(options("first", "+100f").start_frames)

    def test_a_forward_count_on_the_start_is_refused_with_the_fix(self):
        with self.assertRaises(ValueError) as raised:
            options("+50f")
        message = str(raised.exception)
        self.assertIn("-50f", message)
        self.assertIn("end_at", message)

    def test_a_backward_count_on_the_end_is_refused_with_the_fix(self):
        with self.assertRaises(ValueError) as raised:
            options("first", "-50f")
        message = str(raised.exception)
        self.assertIn("+50f", message)
        self.assertIn("start_from", message)

    def test_counting_from_both_ends_at_once_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            options("-50f", "+10f")
        self.assertIn("one end or the other", str(raised.exception))


class SlicingTests(unittest.TestCase):
    def test_the_last_n_frames(self):
        kept = options("-5f").apply_frame_limits(ALL_SCANS)
        self.assertEqual(kept, ALL_SCANS[-5:])

    def test_the_first_n_frames(self):
        kept = options("first", "+5f").apply_frame_limits(ALL_SCANS)
        self.assertEqual(kept, ALL_SCANS[:5])

    def test_asking_for_more_frames_than_exist_returns_what_there_is(self):
        self.assertEqual(len(options("-500f").apply_frame_limits(ALL_SCANS)),
                         len(ALL_SCANS))

    def test_frames_are_counted_chronologically_however_they_arrive(self):
        shuffled = list(reversed(ALL_SCANS))
        self.assertEqual(options("-3f").apply_frame_limits(shuffled),
                         ALL_SCANS[-3:])

    def test_the_count_applies_after_the_window_and_the_scan_filter(self):
        recipe = options("2026-03-02", "+3f")
        kept = recipe.filter_scan_times(ALL_SCANS)
        self.assertEqual(kept, ["2026-03-02T00:00:00", "2026-03-02T06:00:00",
                                "2026-03-02T12:00:00"])

    def test_a_frame_count_composes_with_a_scan_time_filter(self):
        recipe = options("-2f", scan_filter="T12:")
        start, end = recipe.resolve_window(first_scan=FIRST, now=NOW)
        kept = recipe.filter_scan_times(ALL_SCANS, start, end)
        self.assertEqual(kept, ["2026-03-09T12:00:00", "2026-03-10T12:00:00"])


class DescriptionTests(unittest.TestCase):
    def test_a_frame_window_describes_itself_in_frames(self):
        self.assertIn("last 50 frames",
                      options("-50f").window_description(FIRST, NOW))
        self.assertIn("first 100 frames",
                      options("first", "+100f").window_description(FIRST, NOW))
        self.assertIn("to", options("first").window_description(FIRST, NOW))

    def test_a_frame_recipe_still_round_trips_through_json(self):
        recipe = options("-50f", scan_filter="T12:")
        self.assertEqual(ExportOptions.from_dict(recipe.to_dict()), recipe)
        self.assertIn("--start-from -50f", recipe.cli_command())


class ClientFrameTests(unittest.TestCase):
    """Against a fake device, including how many days get queried."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.device = FakeDevice(
            vessels=[vessel_record(38, first="2026-03-01T00:00:00",
                                   last="2026-03-10T18:00:00")],
            scans=list(ALL_SCANS), wells=[(0, 0)], channels=(1,))
        self.client = IncucyteClient("10.0.0.1", store=logged_in_store(self.tmp))

    def tearDown(self):
        self._tmp.cleanup()

    def days_queried(self):
        return sum(1 for route, _ in self.device.calls
                   if route == "Scans/AllScanTimes")

    def plan(self, start_from, end_at=None):
        with patched(self.device):
            return self.client.plan(ExportOptions(
                output=str(self.tmp / "out"), vessels=[38], channels="phase",
                start_from=start_from, end_at=end_at))

    def test_the_first_ten_frames_of_the_experiment(self):
        plan = self.plan("first", "+10f")
        self.assertEqual(plan.output_file_count, 10)
        self.assertEqual(plan.scan_times[0], "2026-03-01T00:00:00")
        self.assertEqual(plan.scan_times[-1], "2026-03-03T06:00:00")

    def test_the_last_ten_frames(self):
        plan = self.plan("-10f")
        self.assertEqual(plan.output_file_count, 10)
        self.assertEqual(plan.scan_times[-1], "2026-03-10T18:00:00")
        self.assertEqual(plan.scan_times[0], "2026-03-08T12:00:00")

    def test_a_forward_count_stops_querying_days_once_it_has_enough(self):
        self.plan("first", "+6f")
        # Six-hourly scans: six frames are found inside the first two days,
        # so the other eight days are never queried.
        self.assertLessEqual(self.days_queried(), 3)

    def test_a_backward_count_walks_back_from_the_end_not_the_start(self):
        self.plan("-6f")
        self.assertLessEqual(self.days_queried(), 3)

    def test_a_frame_count_can_be_narrowed_by_a_date_first(self):
        plan = self.plan("2026-03-05", "+4f")
        self.assertEqual(plan.scan_times[0], "2026-03-05T00:00:00")
        self.assertEqual(plan.output_file_count, 4)

    def test_asking_for_more_frames_than_the_experiment_has(self):
        plan = self.plan("-500f")
        self.assertEqual(plan.output_file_count, len(ALL_SCANS))

    def test_a_frame_window_reaches_the_plan_and_the_manifest(self):
        plan = self.plan("-4f")
        start, end = plan.window
        self.assertEqual(len(plan.scan_times), 4)
        self.assertLessEqual(start, datetime(2026, 3, 10, 0, 0))
        self.assertEqual(plan.to_dict()["scan_time_count"], 4)

    def test_frames_are_scan_times_not_images(self):
        """Adding channels multiplies files, never the frame count."""
        self.device.channels = (1, 2)
        with patched(self.device):
            plan = self.client.plan(ExportOptions(
                output=str(self.tmp / "out"), vessels=[38],
                channels="phase,green", start_from="-5f"))
        self.assertEqual(len(plan.scan_times), 5)
        self.assertEqual(plan.output_file_count, 10)     # 5 frames x 2 channels

    def test_a_frame_count_builds_a_stack_of_exactly_that_depth(self):
        import tifffile
        with patched(self.device):
            result = self.client.download(ExportOptions(
                output=str(self.tmp / "out"), vessels=[38], channels="phase",
                layout="time_stack", start_from="-7f"))
        self.assertEqual(result.file_count, 1)
        with tifffile.TiffFile(result.files[0].path) as handle:
            self.assertEqual(handle.series[0].shape[0], 7)
        self.assertEqual(result.files[0].frame_count, 7)


if __name__ == "__main__":
    unittest.main()
