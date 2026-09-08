"""The export recipe: layouts, well specs, round trips and the CLI mirror."""

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions
from pyincucyte.models import layout_flags, layout_from_flags, resolve_layout


class LayoutTests(unittest.TestCase):
    def test_aliases_resolve_to_canonical_names(self):
        for alias, expected in (
            ("hyperstack", "channel_stack"),
            ("time", "time_stack"),
            ("time_hyper", "time_channel_stack"),
            ("Time Channel Stack", "time_channel_stack"),
            ("separate", "separate"),
            (None, "separate"),
        ):
            self.assertEqual(resolve_layout(alias), expected, alias)

    def test_unknown_layout_names_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_layout("montage")

    def test_layout_maps_to_the_old_boolean_pair_and_back(self):
        for name in ("separate", "channel_stack", "time_stack",
                     "time_channel_stack"):
            hyperstack, time_stack = layout_flags(name)
            self.assertEqual(layout_from_flags(hyperstack, time_stack), name)


class OptionTests(unittest.TestCase):
    def test_bad_specs_fail_at_construction_not_mid_download(self):
        with self.assertRaises(ValueError):
            ExportOptions(output="o", vessels=[1], channels="ultraviolet")
        with self.assertRaises(ValueError):
            ExportOptions(output="o", vessels=[1], wells="??")
        with self.assertRaises(ValueError):
            ExportOptions(output="o", vessels=[1], start_from="March")

    def test_per_vessel_wells_override_the_default(self):
        options = ExportOptions(output="o", vessels=[1, 2], wells="A1",
                                wells_by_vessel={"2": "B1-B2"})
        self.assertEqual(options.wells_for(1), {(0, 0)})
        self.assertEqual(options.wells_for(2), {(1, 0), (1, 1)})

    def test_start_from_resolves_against_the_first_scan(self):
        options = ExportOptions(output="o", vessels=[1], start_from="first")
        self.assertEqual(
            options.resolve_start_date(first_scan=datetime(2026, 3, 1, 9, 0)),
            date(2026, 3, 1))
        today = ExportOptions(output="o", vessels=[1], start_from="today")
        self.assertEqual(today.resolve_start_date(today=date(2026, 5, 1)),
                         date(2026, 5, 1))
        fixed = ExportOptions(output="o", vessels=[1], start_from="2026-04-02")
        self.assertEqual(fixed.resolve_start_date(), date(2026, 4, 2))

    def test_validate_reports_every_problem_at_once(self):
        problems = ExportOptions().validate()
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("output" in p.lower() for p in problems))
        self.assertTrue(any("vessel" in p.lower() for p in problems))

    def test_empty_well_selection_is_flagged(self):
        options = ExportOptions(output="o", vessels=[1], wells="")
        self.assertTrue(any("no wells" in p.lower() for p in options.validate()))

    def test_workers_are_clamped_to_something_sane(self):
        self.assertEqual(ExportOptions(output="o", vessels=[1], workers=0).workers, 1)
        self.assertEqual(ExportOptions(output="o", vessels=[1], workers=99).workers, 32)


class SerialisationTests(unittest.TestCase):
    def test_round_trip_through_json_is_lossless(self):
        options = ExportOptions(
            output="X:/out", vessels=[38, 41], wells="A1-B3",
            wells_by_vessel={"41": "C1"}, channels="phase,green",
            layout="time_channel_stack", start_from="first",
            end_at="2026-04-01", scan_filter="T12:", workers=8,
            interval_minutes=15, host="10.0.0.1", name="nightly")
        self.assertEqual(ExportOptions.from_dict(options.to_dict()), options)

    def test_saved_preset_reloads_identically(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "preset.json"
            options = ExportOptions(output="X:/out", vessels=[38],
                                    layout="time_stack")
            options.save(path)
            self.assertEqual(ExportOptions.load(path), options)
            self.assertEqual(json.loads(path.read_text())["preset_version"], 1)

    def test_old_boolean_flags_are_understood(self):
        options = ExportOptions.from_dict({
            "output": "o", "vessels": [1],
            "hyperstack": True, "time_stack": True,
            "max_workers": 6,
        })
        self.assertEqual(options.layout, "time_channel_stack")
        self.assertEqual(options.workers, 6)


class CommandLineMirrorTests(unittest.TestCase):
    def test_command_reproduces_the_recipe(self):
        options = ExportOptions(
            output="X:/out", vessels=[38], wells="A1-B3", channels="phase,green",
            layout="time_stack", start_from="first", workers=8, host="10.0.0.1")
        command = options.cli_command()
        for fragment in ("pyincucyte", "--host 10.0.0.1", "download", "-v 38",
                         "-o X:/out", "-w A1-B3", "-c phase,green",
                         "--layout time_stack", "--start-from first",
                         "--workers 8"):
            self.assertIn(fragment, command)

    def test_defaults_are_left_off_the_command(self):
        options = ExportOptions(output="out", vessels=[1])
        command = options.cli_command()
        self.assertNotIn("--layout", command)
        self.assertNotIn("--workers", command)
        self.assertNotIn("--start-from", command)

    def test_paths_with_spaces_are_quoted(self):
        options = ExportOptions(output="C:/My Data/run 1", vessels=[1])
        self.assertIn('"C:/My Data/run 1"', options.cli_command())
        self.assertIn("C:/My Data/run 1", options.cli_args())
        self.assertNotIn('"C:/My Data/run 1"', options.cli_args())

    def test_watch_command_carries_the_interval(self):
        options = ExportOptions(output="out", vessels=[1], interval_minutes=20)
        self.assertIn("-i 20", options.cli_command("watch"))

    def test_watch_command_carries_the_chunk_rule(self):
        options = ExportOptions(output="out", vessels=[1],
                                batch_frames=50, batch_after="7d")
        command = options.cli_command("watch")
        self.assertIn("--batch-frames 50", command)
        self.assertIn("--batch-after 7d", command)

    def test_a_download_is_never_asked_to_batch(self):
        # Chunking is a watch idea: a one-shot download has nothing to wait for.
        options = ExportOptions(output="out", vessels=[1], batch_frames=50)
        self.assertNotIn("--batch-frames", options.cli_command("download"))


class PythonMirrorTests(unittest.TestCase):
    def test_python_code_reconstructs_the_exact_download_recipe(self):
        options = ExportOptions(
            output=r"C:\My Data\run 1", vessels=[38], wells="A1-B3",
            wells_by_vessel={"38": "A1-B3"}, channels="phase,green",
            layout="time_stack", start_from="first", end_at="+48h",
            workers=8, host="10.0.0.1", calibrate=True)

        code = options.python_code()
        compile(code, "<copied PyIncucyte Python>", "exec")
        setup = code.split("\n\nwith IncucyteClient", 1)[0]
        namespace = {}
        exec(setup, namespace)

        self.assertEqual(namespace["options"], options)
        self.assertIn("plan = incucyte.plan(options)", code)
        self.assertIn("result = incucyte.download(plan)", code)

    def test_watch_python_code_runs_a_continuous_watcher(self):
        options = ExportOptions(
            output="out", vessels=[1], interval_minutes=20,
            batch_frames=50, batch_after="7d")

        code = options.python_code("watch")

        compile(code, "<copied PyIncucyte Sync Python>", "exec")
        self.assertIn("watcher = incucyte.watch(", code)
        self.assertIn("watcher.run_forever()", code)
        self.assertIn("interval_minutes=20", code)
        self.assertIn("batch_frames=50", code)
        self.assertIn("batch_after='7d'", code)


class BatchTests(unittest.TestCase):
    """Holding new frames back into chunks, for a watcher left running."""

    def test_batching_is_off_unless_asked_for(self):
        options = ExportOptions(output="out", vessels=[1])
        self.assertFalse(options.batches)
        self.assertEqual(options.batch_frames, 0)
        self.assertEqual(options.batch_after, "")
        self.assertIsNone(options.batch_delay)
        self.assertEqual(options.batch_description, "")

    def test_a_delay_is_read_as_a_length_of_time(self):
        options = ExportOptions(output="out", vessels=[1], batch_after="7d")
        self.assertEqual(options.batch_delay, timedelta(days=7))
        self.assertTrue(options.batches)

    def test_a_timedelta_is_stored_as_the_text_a_preset_can_hold(self):
        options = ExportOptions(output="out", vessels=[1],
                                batch_after=timedelta(hours=48))
        self.assertEqual(options.batch_after, "2d")
        self.assertIsInstance(json.dumps(options.to_dict()), str)

    def test_something_that_is_not_a_length_of_time_is_refused_at_load(self):
        with self.assertRaises(ValueError):
            ExportOptions(output="out", vessels=[1], batch_after="next Tuesday")
        with self.assertRaises(ValueError):
            ExportOptions(output="out", vessels=[1], batch_after="7")

    def test_the_rule_round_trips_through_a_preset(self):
        options = ExportOptions(output="out", vessels=[1],
                                batch_frames=24, batch_after="12h")
        again = ExportOptions.from_dict(json.loads(json.dumps(options.to_dict())))
        self.assertEqual(again.batch_frames, 24)
        self.assertEqual(again.batch_after, "12h")

    def test_the_condition_is_spelled_out_for_a_ui(self):
        options = ExportOptions(output="out", vessels=[1],
                                batch_frames=50, batch_after="7d")
        self.assertEqual(
            options.batch_description,
            "50 frames have accumulated or 7 days have passed, "
            "whichever comes first")

    def test_a_span_is_described_in_the_unit_it_was_written_in(self):
        options = ExportOptions(output="out", vessels=[1], batch_after="48h")
        self.assertIn("48 hours", options.batch_description)


if __name__ == "__main__":
    unittest.main()
