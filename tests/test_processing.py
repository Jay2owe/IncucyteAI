"""Preprocessing: the arithmetic, and that it reaches the written file.

The instrument never sends a preprocessed image - it sends raw pixels plus the
coefficients (Scale, Bias, ImageMedian, ColorUnmixes) for doing the work.  So
these tests check two things: that the arithmetic is what the coefficients say
it should be, and that switching it on actually changes the TIFF on disk while
switching it off leaves the download byte-for-byte as it was.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import tifffile

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte import processing
from pyincucyte.manifest import MANIFEST_FILENAME, load_manifest

from fakes import (FakeDevice, image_info, logged_in_store, patched, tiff_bytes,
                   unmix_pair)

#: What the real instrument reported for Jamie's plate: counts per calibrated
#: unit, no bias, and a measured background in the green channel.
GREEN = (46.8, 0.0, 90.0)
RED = (47.61905, 0.0, 0.0)


# ---------------------------------------------------------------------------
# reading the recipe
# ---------------------------------------------------------------------------

class UnmixSpecTests(unittest.TestCase):
    def test_a_percentage_and_a_fraction_mean_the_same_thing(self):
        self.assertEqual(processing.parse_unmix("green:8%red"),
                         processing.parse_unmix("green:0.08red"))

    def test_a_term_names_recipient_contributor_ratio_and_blur(self):
        self.assertEqual(processing.parse_unmix("green:8%red@2"),
                         [{"recipient": 2, "contributor": 3, "ratio": 0.08,
                           "sigma": 2.0}])

    def test_the_angle_bracket_spelling_still_reads(self):
        self.assertEqual(processing.parse_unmix("green<8%red"),
                         processing.parse_unmix("green:8%red"))

    def test_several_terms_are_comma_separated(self):
        pairs = processing.parse_unmix("green:8%red, red:2%green")
        self.assertEqual([(p["recipient"], p["contributor"]) for p in pairs],
                         [(2, 3), (3, 2)])

    def test_phase_is_refused_because_nothing_bleeds_into_it(self):
        with self.assertRaises(ValueError) as caught:
            processing.parse_unmix("phase:8%red")
        self.assertIn("fluorescence", str(caught.exception))

    def test_a_channel_cannot_be_unmixed_from_itself(self):
        with self.assertRaises(ValueError):
            processing.parse_unmix("green:8%green")

    def test_a_ratio_over_one_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            processing.parse_unmix("green:140%red")
        self.assertIn("between 0 and 1", str(caught.exception))

    def test_nonsense_names_the_thing_it_could_not_read(self):
        with self.assertRaises(ValueError) as caught:
            processing.parse_unmix("take some red out")
        self.assertIn("take some red out", str(caught.exception))


class DeviceValueTests(unittest.TestCase):
    def test_saved_unmixing_maps_colour_numbers_onto_channel_numbers(self):
        # ColorUnmixes counts colours 1,2; the device's ImageType counts 2,3.
        scan = {"ColorUnmixes": [unmix_pair(1, 2, 0.08, sigma=1.5)]}
        self.assertEqual(processing.unmix_pairs_from_scan(scan),
                         [{"recipient": 2, "contributor": 3, "ratio": 0.08,
                           "sigma": 1.5}])

    def test_a_zero_ratio_means_nobody_configured_unmixing(self):
        scan = {"ColorUnmixes": [unmix_pair(1, 2, 0.0), unmix_pair(2, 1, 0.0)]}
        self.assertEqual(processing.unmix_pairs_from_scan(scan), [])

    def test_the_dotnet_values_wrapper_is_accepted_unopened(self):
        scan = {"ColorUnmixes": {"$values": [unmix_pair(2, 1, 0.05)]}}
        self.assertEqual(processing.unmix_pairs_from_scan(scan)[0]["recipient"], 3)

    def test_older_software_sending_only_a_scalar_still_works(self):
        pairs = processing.unmix_pairs_from_scan(
            {"Color1Unmix": 0.06, "Color2Unmix": 0.0})
        self.assertEqual(pairs, [{"recipient": 2, "contributor": 3,
                                  "ratio": 0.06, "sigma": 0.0}])

    def test_phase_has_no_calibration_to_read(self):
        self.assertIsNone(processing.coefficients_from_image(
            image_info(0, 0, 1)))
        self.assertEqual(
            processing.coefficients_from_image(
                image_info(0, 0, 2, scale=46.8, median=90.0)),
            {"scale": 46.8, "bias": 0.0, "median": 90.0})


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def plan(calibrate=False, scale=46.8, bias=0.0, background=0.0, unmix=()):
    return {"calibrate": calibrate, "scale": scale, "bias": bias,
            "background": background, "unmix": list(unmix)}


class ArithmeticTests(unittest.TestCase):
    def test_calibration_is_counts_divided_by_scale(self):
        raw = np.full((4, 4), 468, dtype="uint16")
        out = processing.apply(raw, plan(calibrate=True, scale=46.8))
        self.assertEqual(out.dtype, np.float32)
        self.assertAlmostEqual(float(out[0, 0]), 10.0, places=4)

    def test_bias_comes_off_before_the_scale(self):
        raw = np.full((2, 2), 568, dtype="uint16")
        out = processing.apply(raw, plan(calibrate=True, scale=46.8, bias=100.0))
        self.assertAlmostEqual(float(out[0, 0]), 10.0, places=4)

    def test_background_is_removed_in_the_same_space(self):
        raw = np.full((2, 2), 500, dtype="uint16")
        self.assertEqual(int(processing.apply(raw, plan(background=90.0))[0, 0]),
                         410)
        calibrated = processing.apply(
            raw, plan(calibrate=True, scale=46.8, background=90.0))
        self.assertAlmostEqual(float(calibrated[0, 0]), (500 - 90) / 46.8, places=4)

    def test_unmixing_subtracts_a_fraction_of_the_other_channel(self):
        green = np.full((3, 3), 1000, dtype="uint16")
        red = np.full((3, 3), 400, dtype="uint16")
        out = processing.apply(
            green, plan(unmix=[{"contributor": 3, "ratio": 0.08, "sigma": 0.0,
                                "scale": 47.6, "bias": 0.0, "background": 0.0}]),
            fetch_contributor=lambda number: red)
        self.assertEqual(int(out[0, 0]), 1000 - 32)

    def test_each_channel_loses_its_own_background_before_unmixing(self):
        # seen = true + ratio * other + offset, so both offsets go first.
        green = np.full((2, 2), 1000, dtype="uint16")
        red = np.full((2, 2), 400, dtype="uint16")
        out = processing.apply(
            green, plan(background=90.0,
                        unmix=[{"contributor": 3, "ratio": 0.10, "sigma": 0.0,
                                "scale": 47.6, "bias": 0.0, "background": 50.0}]),
            fetch_contributor=lambda number: red)
        self.assertEqual(int(out[0, 0]), (1000 - 90) - round(0.10 * (400 - 50)))

    def test_a_negative_result_is_clipped_rather_than_wrapped(self):
        green = np.full((2, 2), 10, dtype="uint16")
        red = np.full((2, 2), 1000, dtype="uint16")
        out = processing.apply(
            green, plan(unmix=[{"contributor": 3, "ratio": 0.5, "sigma": 0.0,
                                "scale": 47.6, "bias": 0.0, "background": 0.0}]),
            fetch_contributor=lambda number: red)
        self.assertEqual(int(out.max()), 0)
        self.assertEqual(out.dtype, np.uint16)

    def test_without_calibration_the_original_dtype_survives(self):
        raw = np.full((2, 2), 500, dtype="uint16")
        self.assertEqual(processing.apply(raw, plan(background=90.0)).dtype,
                         np.uint16)

    def test_blurring_the_contributor_changes_nothing_when_it_is_flat(self):
        green = np.full((8, 8), 1000, dtype="uint16")
        red = np.full((8, 8), 200, dtype="uint16")
        out = processing.apply(
            green, plan(unmix=[{"contributor": 3, "ratio": 0.10, "sigma": 2.0,
                                "scale": 47.6, "bias": 0.0, "background": 0.0}]),
            fetch_contributor=lambda number: red)
        self.assertEqual(int(out[4, 4]), 980)

    def test_a_mismatched_contributor_is_refused_not_broadcast(self):
        with self.assertRaises(ValueError) as caught:
            processing.apply(
                np.zeros((4, 4), dtype="uint16"),
                plan(unmix=[{"contributor": 3, "ratio": 0.1, "sigma": 0.0,
                             "scale": 1.0, "bias": 0.0, "background": 0.0}]),
                fetch_contributor=lambda number: np.zeros((2, 2), dtype="uint16"))
        self.assertIn("must match", str(caught.exception))

    def test_no_plan_means_the_array_comes_back_untouched(self):
        raw = np.full((2, 2), 7, dtype="uint16")
        self.assertIs(processing.apply(raw, None), raw)


class RecipeTests(unittest.TestCase):
    def test_nothing_is_on_by_default(self):
        recipe = processing.Recipe.from_options(ExportOptions())
        self.assertFalse(recipe.is_active)
        self.assertIn("raw pixels", recipe.describe())

    def test_phase_gets_no_plan_because_it_has_no_calibration(self):
        recipe = processing.Recipe(calibrate=True)
        coefficients = {2: {"scale": 46.8, "bias": 0.0, "median": 90.0}}
        self.assertIsNone(processing.plan_for_image(recipe, 1, coefficients, []))
        self.assertIsNotNone(processing.plan_for_image(recipe, 2, coefficients, []))

    def test_an_unmix_term_without_its_contributor_is_dropped(self):
        recipe = processing.Recipe(unmix="green:8%red")
        only_green = {2: {"scale": 46.8, "bias": 0.0, "median": 0.0}}
        pairs = processing.parse_unmix("green:8%red")
        self.assertIsNone(
            processing.plan_for_image(recipe, 2, only_green, pairs))


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

class ProcessedDownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.store = logged_in_store(self.tmp)
        self.device = FakeDevice(
            wells=[(0, 0)], channels=(1, 2, 3),
            calibration={2: GREEN, 3: RED},
            unmixes=[unmix_pair(1, 2, 0.10), unmix_pair(2, 1, 0.0)],
            pixels={1: 120, 2: 1000, 3: 400})
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **changes):
        base = dict(output=str(self.out), vessels=[38], channels="green",
                    start_from="2026-03-01", end_at="2026-03-01",
                    scan_filter="09:00")
        base.update(changes)
        return ExportOptions(**base)

    def fetch(self, **changes):
        with patched(self.device):
            return self.client.fetch(self.options(**changes))

    def only_file(self, result):
        self.assertEqual(result.file_count, 1, result.errors)
        return tifffile.imread(str(result.files[0].path))

    # -- off by default ---------------------------------------------------

    def test_by_default_the_bytes_the_device_sent_are_the_bytes_written(self):
        result = self.fetch()
        self.assertEqual(result.files[0].path.read_bytes(), tiff_bytes(value=1000))
        self.assertFalse(result.files[0].processed)

    # -- calibration ------------------------------------------------------

    def test_calibrate_writes_calibrated_units_as_32_bit_float(self):
        result = self.fetch(calibrate=True)
        pixels = self.only_file(result)
        self.assertEqual(pixels.dtype, np.float32)
        self.assertAlmostEqual(float(pixels[0, 0]), 1000 / 46.8, places=3)
        self.assertTrue(result.files[0].processed)

    def test_phase_is_left_alone_even_when_calibration_is_on(self):
        result = self.fetch(calibrate=True, channels="phase")
        self.assertEqual(result.files[0].path.read_bytes(), tiff_bytes(value=120))
        self.assertFalse(result.files[0].processed)

    # -- background -------------------------------------------------------

    def test_the_device_background_is_the_one_the_instrument_measured(self):
        pixels = self.only_file(self.fetch(background="device"))
        self.assertEqual(int(pixels[0, 0]), 1000 - 90)

    def test_a_number_is_taken_as_raw_counts(self):
        pixels = self.only_file(self.fetch(background="250"))
        self.assertEqual(int(pixels[0, 0]), 750)

    # -- unmixing ---------------------------------------------------------

    def test_device_unmixing_uses_the_ratio_saved_on_the_vessel(self):
        pixels = self.only_file(self.fetch(unmix="device"))
        self.assertEqual(int(pixels[0, 0]), 1000 - int(0.10 * 400))

    def test_an_explicit_spec_overrides_what_the_vessel_says(self):
        pixels = self.only_file(self.fetch(unmix="green:25%red"))
        self.assertEqual(int(pixels[0, 0]), 1000 - 100)

    def test_the_contributor_is_fetched_even_though_it_was_not_selected(self):
        # --channels green, but unmixing green needs red.
        self.fetch(unmix="device")
        fetched = [name for name in self.device.fetches if ":unmix" in str(name)]
        self.assertEqual(len(fetched), 1)
        self.assertTrue(fetched[0].endswith(":unmix3"), fetched)

    def test_unmixing_and_calibration_compose(self):
        pixels = self.only_file(self.fetch(calibrate=True, unmix="green:25%red"))
        expected = 1000 / 46.8 - 0.25 * (400 / 47.61905)
        self.assertAlmostEqual(float(pixels[0, 0]), expected, places=3)

    # -- stacks -----------------------------------------------------------

    def test_a_calibrated_time_stack_is_a_float_stack(self):
        result = self.fetch(calibrate=True, layout="time_stack",
                            scan_filter=None)
        stack = self.only_file(result)
        self.assertEqual(stack.dtype, np.float32)
        self.assertEqual(stack.shape[0], 2)
        self.assertAlmostEqual(float(stack[0, 0, 0]), 1000 / 46.8, places=3)

    def test_a_channel_stack_processes_every_channel_it_holds(self):
        result = self.fetch(calibrate=True, channels="green,red",
                            layout="channel_stack")
        stack = self.only_file(result)
        self.assertEqual(stack.shape[0], 2)
        self.assertAlmostEqual(float(stack[0, 0, 0]), 1000 / 46.8, places=3)
        self.assertAlmostEqual(float(stack[1, 0, 0]), 400 / 47.61905, places=3)

    # -- keeping processed and raw apart ----------------------------------

    def test_a_processed_file_is_named_for_what_was_done_to_it(self):
        self.assertTrue(
            self.fetch(calibrate=True).files[0].path.name.endswith("_cal.tif"))
        self.assertTrue(
            self.fetch(calibrate=True, background="device", unmix="device")
            .files[0].path.name.endswith("_cal-bg-unmix.tif"))

    def test_raw_keeps_the_plain_name(self):
        self.assertTrue(self.fetch().files[0].path.name.endswith("_00d00h00m.tif"))

    def test_phase_keeps_the_plain_name_even_in_a_processed_run(self):
        result = self.fetch(calibrate=True, channels="phase,green")
        names = sorted(f.path.name for f in result.files)
        self.assertTrue(names[0].endswith("_00d00h00m.tif"), names)
        self.assertTrue(names[1].endswith("_00d00h00m_cal.tif"), names)

    def test_switching_processing_on_does_not_count_as_already_downloaded(self):
        # Same wells, same scan: without the tag the resume ledger would skip
        # the second run and leave raw pixels where calibrated ones belong.
        first = self.fetch()
        second = self.fetch(calibrate=True)
        self.assertEqual(second.file_count, 1, second.errors)
        self.assertNotEqual(first.files[0].path, second.files[0].path)
        self.assertTrue(first.files[0].path.exists())

    def test_the_same_recipe_twice_still_resumes(self):
        self.fetch(calibrate=True)
        self.assertEqual(self.fetch(calibrate=True).file_count, 0)

    # -- saying so --------------------------------------------------------

    def test_the_manifest_records_what_was_done_to_the_pixels(self):
        result = self.fetch(calibrate=True, unmix="device")
        manifest = load_manifest(self.out / MANIFEST_FILENAME)
        self.assertEqual(manifest["options"]["calibrate"], True)
        self.assertEqual(manifest["options"]["unmix"], "device")
        self.assertIn("calibrated units",
                      manifest["options"]["processing"]["description"])
        entry = manifest["files"][0]
        self.assertTrue(entry["processed"])
        self.assertIn("unmixed", entry["processing"])

    def test_the_plan_says_so_before_anything_is_downloaded(self):
        with patched(self.device):
            plan = self.client.plan(self.options(calibrate=True))
        self.assertIn("Pixels: calibrated units", plan.summary())

    def test_copy_cli_command_carries_the_recipe(self):
        # The GUI's "Copy CLI command" is how a settings screen becomes a line
        # in a pipeline script; a recipe it silently dropped would download
        # raw pixels under a processed name.
        from pyincucyte.cli import options_from_args, parse_args

        options = self.options(calibrate=True, unmix="green:8%red",
                               background="device")
        command = options.cli_command()
        self.assertIn("--calibrate", command)
        restored = options_from_args(parse_args(command.split()[1:]))
        self.assertEqual(
            (restored.calibrate, restored.unmix, restored.background),
            (True, "green:8%red", "device"))

    def test_a_recipe_round_trips_through_a_preset(self):
        options = self.options(calibrate=True, unmix="green:8%red",
                               background="device")
        restored = ExportOptions.from_dict(
            json.loads(json.dumps(options.to_dict())))
        self.assertEqual((restored.calibrate, restored.unmix, restored.background),
                         (True, "green:8%red", "device"))


if __name__ == "__main__":
    unittest.main()
