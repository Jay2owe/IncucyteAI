"""Watch mode, and the chunking that lets an experiment be collected weekly.

The point of a chunk is that a poll which finds work does *not* download it: it
counts what is waiting and decides.  So most of these tests assert on what did
not happen - no files, an empty ledger - as much as on what did.
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.state import STATE_FILENAME
from pyincucyte.watch import format_age

from fakes import FakeDevice, logged_in_store, patched, vessel_record


def stamp(moment):
    """Write a datetime the way the device does."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


class WatchTestCase(unittest.TestCase):
    """A fake instrument whose scans sit at chosen ages, measured from now."""

    #: Ages, in hours before now, of the scans the device starts with.
    ages = (3, 2)

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.store = logged_in_store(self.tmp)
        self.now = datetime.now().replace(microsecond=0)
        scans = [self.at(hours=age) for age in sorted(self.ages, reverse=True)]
        self.device = FakeDevice(
            vessels=[vessel_record(first=scans[0], last=scans[-1])],
            scans=list(scans), channels=(1,))
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def at(self, **ago):
        """A scan time that many hours/days before now."""
        return stamp(self.now - timedelta(**ago))

    def add_scan(self, **ago):
        """Give the instrument one more scan and tell the vessel about it."""
        when = self.at(**ago)
        self.device.scans.append(when)
        self.device.scans.sort()
        self.device.vessels[0]["LastScanDateTime"] = max(self.device.scans)
        self.device.vessels[0]["FirstScanDateTime"] = min(self.device.scans)
        self.client.vessels(refresh=True)
        return when

    def options(self, **changes):
        base = dict(output=str(self.out), vessels=[38], channels="phase",
                    start_from="first", end_at="now")
        base.update(changes)
        return ExportOptions(**base)

    def watcher(self, **changes):
        return self.client.watch(self.options(**changes), start=False)

    def files(self):
        return sorted(p.name for p in self.out.glob("*.tif"))

    def ledger(self):
        path = self.out / STATE_FILENAME
        return path.read_text(encoding="utf-8") if path.exists() else ""


class NoBatchingTests(WatchTestCase):
    """Without a chunk size the watcher behaves exactly as it always did."""

    def test_a_poll_downloads_every_new_frame_on_sight(self):
        with patched(self.device):
            watcher = self.watcher()
            result = watcher.poll_once()
        # two scan times x two wells x Phase
        self.assertEqual(result.file_count, 4)
        self.assertEqual(watcher.file_count, 4)
        self.assertEqual(len(self.files()), 4)
        self.assertFalse(watcher.batches)
        self.assertFalse(watcher.is_holding)

    def test_a_second_poll_with_nothing_new_still_returns_a_result(self):
        with patched(self.device):
            watcher = self.watcher()
            watcher.poll_once()
            again = watcher.poll_once()
        self.assertIsNotNone(again)
        self.assertEqual(again.file_count, 0)
        self.assertEqual(watcher.file_count, 4)

    def test_a_stopped_watcher_reports_itself_as_stopped(self):
        with patched(self.device):
            watcher = self.watcher()
        self.assertFalse(watcher.is_running)
        watcher.stop()
        self.assertTrue(watcher.stop_event.is_set())


class FrameCountTests(WatchTestCase):
    """``batch_frames``: wait until there are enough frames to be worth it."""

    def test_a_chunk_short_of_its_count_is_held_and_nothing_is_written(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=4)
            result = watcher.poll_once()
        self.assertIsNone(result)
        self.assertEqual(self.files(), [])
        self.assertEqual(watcher.pending_frames, 2)
        self.assertTrue(watcher.is_holding)
        self.assertEqual(watcher.file_count, 0)

    def test_a_held_chunk_leaves_the_resume_ledger_alone(self):
        # Nothing may be recorded as downloaded, or a later run would skip it.
        with patched(self.device):
            self.watcher(batch_frames=4).poll_once()
        self.assertNotIn("VID38", self.ledger())

    def test_the_chunk_goes_the_moment_the_count_is_reached(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=4)
            self.assertIsNone(watcher.poll_once())
            self.add_scan(hours=1)
            self.assertIsNone(watcher.poll_once())
            self.add_scan(minutes=30)
            result = watcher.poll_once()
        self.assertIsNotNone(result)
        # four frames, held back and then fetched in one go
        self.assertEqual(result.file_count, 8)
        self.assertEqual(len(self.files()), 8)
        self.assertEqual(watcher.pending_frames, 0)
        self.assertFalse(watcher.is_holding)

    def test_a_frame_is_a_timepoint_not_a_file(self):
        # Two scan times over two wells is eight files but only two frames.
        self.device.wells = [(0, 0), (0, 1), (1, 0), (1, 1)]
        with patched(self.device):
            watcher = self.watcher(batch_frames=3)
            watcher.poll_once()
        self.assertEqual(watcher.pending_frames, 2)

    def test_the_count_starts_again_after_a_chunk_is_delivered(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=2)
            watcher.poll_once()
            self.assertEqual(watcher.pending_frames, 0)
            self.add_scan(hours=1)
            self.assertIsNone(watcher.poll_once())
        self.assertEqual(watcher.pending_frames, 1)


class TimeLimitTests(WatchTestCase):
    """``batch_after``: collect on a clock instead of a count."""

    def test_a_fresh_chunk_is_held_until_its_time_is_up(self):
        with patched(self.device):
            watcher = self.watcher(batch_after="7d")
            self.assertIsNone(watcher.poll_once())
        self.assertEqual(self.files(), [])
        self.assertEqual(watcher.pending_frames, 2)

    def test_frames_older_than_the_limit_go_on_the_first_poll(self):
        # The clock runs from the frame's own timestamp, so a watcher started
        # today on a week-old experiment does not wait another week.
        with patched(self.device):
            self.add_scan(days=8)
            watcher = self.watcher(batch_after="7d")
            result = watcher.poll_once()
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 6)

    def test_the_clock_measures_the_oldest_waiting_frame(self):
        with patched(self.device):
            self.add_scan(days=8)
            watcher = self.watcher(batch_after="30d")
            watcher.poll_once()
        self.assertEqual(watcher.pending_since,
                         datetime.strptime(self.at(days=8), "%Y-%m-%dT%H:%M:%S"))
        self.assertGreater(watcher.pending_age, timedelta(days=7))

    def test_a_minutes_limit_releases_frames_that_are_old_enough(self):
        with patched(self.device):
            watcher = self.watcher(batch_after="90m")
            result = watcher.poll_once()
        # the scans are two and three hours old, so both are overdue
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 4)


class WhicheverComesFirstTests(WatchTestCase):
    """With both conditions set, the first one satisfied wins."""

    def test_the_frame_count_can_release_a_chunk_early(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=2, batch_after="30d")
            result = watcher.poll_once()
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 4)

    def test_the_clock_can_release_a_chunk_that_never_fills(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=500, batch_after="90m")
            result = watcher.poll_once()
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 4)

    def test_neither_satisfied_means_the_chunk_waits(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=500, batch_after="30d")
            self.assertIsNone(watcher.poll_once())
        self.assertEqual(self.files(), [])


class TimeStackTests(WatchTestCase):
    """A time stack is rewritten whole, so counting its frames needs care."""

    def test_only_the_frames_the_folder_lacks_are_counted_as_new(self):
        with patched(self.device):
            watcher = self.watcher(layout="time_stack")
            watcher.poll_once()                 # writes both frames
            self.assertEqual(len(self.files()), 2)   # one stack per well
            self.add_scan(hours=1)
            held = self.watcher(layout="time_stack", batch_frames=5)
            self.assertIsNone(held.poll_once())
        # the rebuilt stack covers three frames, but only one of them is new
        self.assertEqual(held.pending_frames, 1)

    def test_a_first_run_counts_every_frame_in_the_stack(self):
        with patched(self.device):
            watcher = self.watcher(layout="time_stack", batch_frames=5)
            self.assertIsNone(watcher.poll_once())
        self.assertEqual(watcher.pending_frames, 2)


class FlushTests(WatchTestCase):
    """Collecting the tail: a chunk that will never fill because it is over."""

    def test_flush_takes_a_part_full_chunk(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=100)
            self.assertIsNone(watcher.poll_once())
            self.assertEqual(watcher.pending_frames, 2)
            result = watcher.flush()
        self.assertEqual(result.file_count, 4)
        self.assertEqual(len(self.files()), 4)
        self.assertEqual(watcher.pending_frames, 0)

    def test_flush_works_after_stop_has_already_been_called(self):
        # stop() sets the cancel event; a flush must not cancel itself with it.
        with patched(self.device):
            watcher = self.watcher(batch_frames=100)
            watcher.poll_once()
            watcher.stop()
            watcher.flush()
        self.assertEqual(len(self.files()), 4)

    def test_stop_can_take_the_tail_on_the_way_out(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=100)
            watcher.poll_once()
            watcher.stop(flush=True)
        self.assertEqual(len(self.files()), 4)

    def test_stop_without_flush_leaves_the_chunk_on_the_instrument(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=100)
            watcher.poll_once()
            watcher.stop()
        self.assertEqual(self.files(), [])
        self.assertEqual(watcher.pending_frames, 2)


class ReportingTests(WatchTestCase):
    """What the watcher tells a UI while it is holding."""

    def test_on_hold_is_called_with_the_watcher(self):
        seen = []
        with patched(self.device):
            watcher = self.client.watch(self.options(batch_frames=9),
                                        start=False, on_hold=seen.append)
            watcher.poll_once()
        self.assertEqual(seen, [watcher])

    def test_on_result_is_not_called_while_a_chunk_is_held(self):
        results = []
        with patched(self.device):
            watcher = self.client.watch(self.options(batch_frames=9),
                                        start=False, on_result=results.append)
            watcher.poll_once()
        self.assertEqual(results, [])

    def test_a_holding_progress_event_reports_frames_not_files(self):
        events = []
        with patched(self.device):
            watcher = self.client.watch(self.options(batch_frames=9),
                                        start=False, progress=events.append)
            watcher.poll_once()
        holds = [e for e in events if e.stage == "holding"]
        self.assertEqual(len(holds), 1)
        self.assertEqual((holds[0].done, holds[0].total, holds[0].unit),
                         (2, 9, "frames"))

    def test_the_hold_description_names_the_condition_and_the_wait(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=9, batch_after="7d")
            watcher.poll_once()
        text = watcher.hold_description
        self.assertIn("2 frames held", text)
        self.assertIn("9 frames have accumulated", text)
        self.assertIn("7 days have passed", text)
        self.assertIn("whichever comes first", text)
        self.assertIn("so far", text)

    def test_repr_shows_how_much_is_being_held(self):
        with patched(self.device):
            watcher = self.watcher(batch_frames=9)
            watcher.poll_once()
        self.assertIn("held=2", repr(watcher))


class FormatAgeTests(unittest.TestCase):
    def test_ages_read_in_the_units_that_matter(self):
        self.assertEqual(format_age(timedelta(seconds=20)), "just now")
        self.assertEqual(format_age(timedelta(minutes=12)), "12m")
        self.assertEqual(format_age(timedelta(hours=3, minutes=20)), "3h 20m")
        self.assertEqual(format_age(timedelta(days=2, hours=4)), "2d 4h")
        self.assertEqual(format_age(timedelta(seconds=-5)), "just now")


if __name__ == "__main__":
    unittest.main()
