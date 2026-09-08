"""Command line parsing, including every flag the old script accepted."""

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from pyincucyte import ExportOptions
from pyincucyte import cli
from pyincucyte.cli import options_from_args, parse_args
from pyincucyte.errors import IncucyteError


def parse(argv):
    return parse_args(argv)


def resolve(argv):
    return options_from_args(parse(argv))


class LegacyFlagTests(unittest.TestCase):
    """Commands people already have in scripts and scheduled tasks."""

    def test_the_original_download_invocation_still_works(self):
        options = resolve(["download", "-v", "38", "-o", "./images",
                           "--start-from", "first"])
        self.assertEqual(options.vessels, [38])
        self.assertEqual(options.output, "./images")
        self.assertEqual(options.start_from, "first")
        self.assertEqual(options.layout, "separate")

    def test_login_accepts_a_friendly_device_name(self):
        args = parse(["--host", "10.0.0.2", "login", "--name", "Upstairs"])

        self.assertEqual(args.host, "10.0.0.2")
        self.assertEqual(args.name, "Upstairs")

    def test_hyperstack_and_time_stack_flags_map_onto_layouts(self):
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o", "--hyperstack"]).layout,
            "channel_stack")
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o", "--time-stack"]).layout,
            "time_stack")
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o",
                     "--hyperstack", "--time-stack"]).layout,
            "time_channel_stack")

    def test_date_is_shorthand_for_a_single_day(self):
        options = resolve(["download", "-v", "1", "-o", "o",
                           "-d", "2026-03-01"])
        self.assertEqual(options.start_from, "2026-03-01")
        self.assertEqual(options.end_at, "2026-03-01")

    def test_repeatable_filter_builds_per_vessel_well_lists(self):
        options = resolve(["watch", "-o", "o", "-f", "38:A1,B3", "-f", "39"])
        self.assertEqual(options.vessels, [38, 39])
        self.assertEqual(options.wells_for(38), {(0, 0), (1, 2)})
        self.assertIsNone(options.wells_for(39))

    def test_legacy_json_config_is_still_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vessels.json"
            path.write_text('{"vessels": [{"id": 38, "wells": ["A1", "A2"], '
                            '"channels": "phase"}]}')
            options = resolve(["watch", "-o", "o", "--config", str(path)])
        self.assertEqual(options.vessels, [38])
        self.assertEqual(options.wells_for(38), {(0, 0), (0, 1)})
        self.assertEqual(options.channels, "phase")


class BatchFlagTests(unittest.TestCase):
    """--batch-frames / --batch-after, the chunked watch."""

    def test_the_chunk_rule_reaches_the_options(self):
        options = resolve(["watch", "-v", "38", "-o", "o",
                           "--batch-frames", "50", "--batch-after", "7d"])
        self.assertEqual(options.batch_frames, 50)
        self.assertEqual(options.batch_after, "7d")
        self.assertTrue(options.batches)

    def test_either_condition_works_on_its_own(self):
        self.assertEqual(
            resolve(["watch", "-v", "1", "-o", "o", "--batch-after", "12h"]
                    ).batch_after, "12h")
        self.assertEqual(
            resolve(["watch", "-v", "1", "-o", "o", "--batch-frames", "24"]
                    ).batch_frames, 24)

    def test_a_plain_watch_does_not_batch(self):
        self.assertFalse(resolve(["watch", "-v", "1", "-o", "o"]).batches)

    def test_a_download_has_no_batch_flags(self):
        with self.assertRaises(SystemExit):
            parse(["download", "-v", "1", "-o", "o", "--batch-frames", "5"])


class NewFlagTests(unittest.TestCase):
    def test_several_vessels_can_be_given(self):
        options = resolve(["download", "-v", "38", "-v", "41", "-o", "o"])
        self.assertEqual(options.vessels, [38, 41])

    def test_layout_can_be_named_directly(self):
        options = resolve(["download", "-v", "1", "-o", "o",
                           "--layout", "time_channel_stack"])
        self.assertEqual(options.layout, "time_channel_stack")

    def test_per_vessel_wells_flag(self):
        options = resolve(["download", "-v", "38", "-o", "o",
                           "--vessel-wells", "38:C1-C4"])
        self.assertEqual(options.wells_for(38),
                         {(2, 0), (2, 1), (2, 2), (2, 3)})

    def test_manifest_can_be_switched_off(self):
        self.assertFalse(
            resolve(["download", "-v", "1", "-o", "o", "--no-manifest"])
            .write_manifest)

    def test_state_scope_is_selectable(self):
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o",
                     "--state-scope", "global"]).state_scope, "global")


class PresetTests(unittest.TestCase):
    def test_a_preset_supplies_the_defaults(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "preset.json"
            ExportOptions(output="X:/out", vessels=[38], channels="phase,green",
                          layout="time_stack", workers=8).save(path)
            options = resolve(["download", "--preset", str(path)])
        self.assertEqual(options.vessels, [38])
        self.assertEqual(options.layout, "time_stack")
        self.assertEqual(options.workers, 8)

    def test_command_line_flags_win_over_the_preset(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "preset.json"
            ExportOptions(output="X:/out", vessels=[38], channels="phase",
                          layout="time_stack", workers=8).save(path)
            options = resolve(["download", "--preset", str(path),
                               "-o", "Y:/other", "--workers", "2",
                               "--layout", "separate"])
        self.assertEqual(options.output, "Y:/other")
        self.assertEqual(options.workers, 2)
        self.assertEqual(options.layout, "separate")


class NegativeValueTests(unittest.TestCase):
    """argparse would otherwise read -48h as an unknown flag."""

    def test_a_relative_start_survives_a_space(self):
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o",
                     "--start-from", "-48h"]).start_from, "-48h")

    def test_a_frame_count_survives_a_space(self):
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o",
                     "-s", "-100f"]).start_from, "-100f")

    def test_the_equals_form_still_works(self):
        self.assertEqual(
            resolve(["download", "-v", "1", "-o", "o",
                     "--start-from=-7d"]).start_from, "-7d")

    def test_real_flags_after_the_option_are_left_alone(self):
        args = parse(["download", "-v", "1", "-o", "o", "--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertIsNone(args.start_from)


class JsonAutomationTests(unittest.TestCase):
    def test_runtime_errors_are_machine_readable(self):
        def unavailable(_args):
            raise IncucyteError("instrument is offline")

        stdout, stderr = StringIO(), StringIO()
        with mock.patch.dict(cli.COMMANDS, {"probe": unavailable}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--json", "probe"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "IncucyteError")
        self.assertEqual(payload["command"], "probe")
        self.assertIn("offline", stderr.getvalue())

    def test_usage_errors_are_machine_readable_and_still_exit_two(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--json", "download", "--not-an-option"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(payload["error"]["type"], "UsageError")
        self.assertEqual(payload["command"], "download")
        self.assertIn("unrecognized arguments", stderr.getvalue())


class ValidationTests(unittest.TestCase):
    def test_no_vessel_is_a_clear_error_not_a_traceback(self):
        with self.assertRaises(SystemExit):
            resolve(["download", "-o", "o"])

    def test_no_output_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            resolve(["download", "-v", "1"])


if __name__ == "__main__":
    unittest.main()
