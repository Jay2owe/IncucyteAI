"""The export window: start_from and end_at, at date and time resolution."""

import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.options import parse_duration, parse_moment

from fakes import FakeDevice, logged_in_store, patched, vessel_record

NOW = datetime(2026, 3, 10, 16, 0)
FIRST = datetime(2026, 3, 1, 14, 30)


def options(start_from="first", end_at=None, **extra):
    return ExportOptions(output="o", vessels=[1], start_from=start_from,
                         end_at=end_at, **extra)


class MomentParsingTests(unittest.TestCase):
    def test_every_written_form_is_accepted(self):
        for text, expected in (
            ("2026-03-05", datetime(2026, 3, 5, 0, 0)),
            ("2026-03-05 14:30", datetime(2026, 3, 5, 14, 30)),
            ("2026-03-05T14:30", datetime(2026, 3, 5, 14, 30)),
            ("2026-03-05 14:30:15", datetime(2026, 3, 5, 14, 30, 15)),
            ("2026-03-05T14:30:15", datetime(2026, 3, 5, 14, 30, 15)),
        ):
            self.assertEqual(parse_moment(text), expected, text)

    def test_a_bare_date_can_mean_the_end_of_that_day(self):
        end = parse_moment("2026-03-05", end_of_day=True)
        self.assertEqual(end.date(), date(2026, 3, 5))
        self.assertEqual((end.hour, end.minute), (23, 59))

    def test_date_and_datetime_objects_pass_straight_through(self):
        self.assertEqual(parse_moment(datetime(2026, 3, 5, 9, 0)),
                         datetime(2026, 3, 5, 9, 0))
        self.assertEqual(parse_moment(date(2026, 3, 5)),
                         datetime(2026, 3, 5, 0, 0))

    def test_nonsense_names_the_field_and_the_accepted_forms(self):
        with self.assertRaises(ValueError) as raised:
            parse_moment("last tuesday", "start_from")
        message = str(raised.exception)
        self.assertIn("start_from", message)
        self.assertIn("YYYY-MM-DD", message)

    def test_durations_need_a_sign_so_they_cannot_look_like_dates(self):
        self.assertEqual(parse_duration("-48h").total_seconds(), -48 * 3600)
        self.assertEqual(parse_duration("+3d").days, 3)
        self.assertEqual(parse_duration("+90m").total_seconds(), 90 * 60)
        self.assertIsNone(parse_duration("48h"))
        self.assertIsNone(parse_duration("2026-03-05"))


class WindowTests(unittest.TestCase):
    def window(self, start_from, end_at=None):
        return options(start_from, end_at).resolve_window(first_scan=FIRST, now=NOW)

    def test_first_starts_at_the_experiments_own_first_scan_time(self):
        start, end = self.window("first")
        self.assertEqual(start, FIRST)          # 14:30, not midnight
        self.assertEqual(end, NOW)

    def test_today_starts_at_midnight(self):
        start, _ = self.window("today")
        self.assertEqual(start, datetime(2026, 3, 10, 0, 0))

    def test_a_time_of_day_is_honoured_on_both_ends(self):
        start, end = self.window("2026-03-01 14:30", "2026-03-03 09:00")
        self.assertEqual(start, datetime(2026, 3, 1, 14, 30))
        self.assertEqual(end, datetime(2026, 3, 3, 9, 0))

    def test_a_bare_end_date_includes_the_whole_of_that_day(self):
        _, end = self.window("2026-03-01", "2026-03-05")
        self.assertEqual(end.date(), date(2026, 3, 5))
        self.assertGreater(end.hour, 22)

    def test_a_negative_start_is_a_rolling_window_ending_now(self):
        start, end = self.window("-48h")
        self.assertEqual(start, datetime(2026, 3, 8, 16, 0))
        self.assertEqual(end, NOW)

    def test_a_positive_end_is_measured_from_the_start(self):
        start, end = self.window("first", "+72h")
        self.assertEqual(start, FIRST)
        self.assertEqual(end, datetime(2026, 3, 4, 14, 30))

    def test_a_backwards_window_is_refused_with_both_values_named(self):
        with self.assertRaises(ValueError) as raised:
            self.window("2026-03-05", "2026-03-01")
        message = str(raised.exception)
        self.assertIn("start_from", message)
        self.assertIn("end_at", message)

    def test_bad_values_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            options(start_from="soon")
        with self.assertRaises(ValueError):
            options(end_at="whenever")

    def test_datetimes_are_stored_as_text_so_presets_stay_json(self):
        recipe = options(start_from=FIRST, end_at=date(2026, 3, 5))
        self.assertEqual(recipe.start_from, "2026-03-01T14:30:00")
        self.assertEqual(recipe.end_at, "2026-03-05")
        self.assertEqual(ExportOptions.from_dict(recipe.to_dict()), recipe)

    def test_the_old_end_date_key_still_loads(self):
        recipe = ExportOptions.from_dict(
            {"output": "o", "vessels": [1], "end_date": "2026-03-05"})
        self.assertEqual(recipe.end_at, "2026-03-05")


class ScanFilteringTests(unittest.TestCase):
    SCANS = [f"2026-03-0{day}T{hour:02d}:00:00"
             for day in (1, 2, 3) for hour in (9, 15, 21)]

    def test_scans_before_the_start_time_are_dropped(self):
        recipe = options("2026-03-01 14:00", "2026-03-02 16:00")
        kept = recipe.filter_scan_times(self.SCANS)
        self.assertEqual(kept, ["2026-03-01T15:00:00", "2026-03-01T21:00:00",
                                "2026-03-02T09:00:00", "2026-03-02T15:00:00"])

    def test_the_first_36_hours_of_an_afternoon_start(self):
        recipe = options("first", "+36h")
        start, end = recipe.resolve_window(first_scan=FIRST, now=NOW)
        kept = recipe.filter_scan_times(self.SCANS, start, end)
        self.assertNotIn("2026-03-01T09:00:00", kept)   # before the first scan
        self.assertIn("2026-03-02T21:00:00", kept)
        self.assertNotIn("2026-03-03T09:00:00", kept)   # past 02:30 on day 3

    def test_scan_filter_still_applies_inside_the_window(self):
        recipe = options("2026-03-01", "2026-03-03", scan_filter="T09:")
        kept = recipe.filter_scan_times(self.SCANS)
        self.assertEqual(kept, ["2026-03-01T09:00:00", "2026-03-02T09:00:00",
                                "2026-03-03T09:00:00"])


class ClientWindowTests(unittest.TestCase):
    """The window has to survive the round trip through a real plan."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.device = FakeDevice(
            vessels=[vessel_record(38, first="2026-03-01T14:30:00",
                                   last="2026-03-03T21:00:00")],
            scans=[f"2026-03-0{day}T{hour:02d}:00:00"
                   for day in (1, 2, 3) for hour in (9, 15, 21)],
            wells=[(0, 0)], channels=(1,))
        self.client = IncucyteClient("10.0.0.1", store=logged_in_store(self.tmp))

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, start_from, end_at=None):
        with patched(self.device):
            return self.client.plan(ExportOptions(
                output=str(self.tmp / "out"), vessels=[38], channels="phase",
                start_from=start_from, end_at=end_at))

    def test_a_time_of_day_narrows_the_plan(self):
        whole = self.plan("2026-03-01", "2026-03-03")
        trimmed = self.plan("2026-03-01 14:00", "2026-03-02 16:00")
        # 9 scan times exist, but 09:00 on day 1 predates the vessel's own
        # first scan at 14:30 and is dropped whatever the window says.
        self.assertEqual(whole.output_file_count, 8)
        self.assertEqual(trimmed.output_file_count, 4)

    def test_the_plan_records_the_window_it_used(self):
        plan = self.plan("2026-03-01 14:00", "2026-03-02 16:00")
        start, end = plan.window
        self.assertEqual(start, datetime(2026, 3, 1, 14, 0))
        self.assertEqual(end, datetime(2026, 3, 2, 16, 0))
        self.assertIn("2026-03-01 14:00", plan.summary())
        self.assertEqual(plan.to_dict()["window"][0], "2026-03-01T14:00:00")

    def test_first_uses_the_vessels_own_start_time_not_midnight(self):
        plan = self.plan("first", "2026-03-01")
        # 09:00 predates the 14:30 first scan, so only 15:00 and 21:00 count.
        self.assertEqual(plan.output_file_count, 2)

    def test_the_window_reaches_the_manifest(self):
        with patched(self.device):
            result = self.client.download(ExportOptions(
                output=str(self.tmp / "out"), vessels=[38], channels="phase",
                start_from="2026-03-01 14:00", end_at="2026-03-02 16:00"))
        self.assertEqual(result.file_count, 4)
        self.assertEqual(result.plan.window[1], datetime(2026, 3, 2, 16, 0))


if __name__ == "__main__":
    unittest.main()
