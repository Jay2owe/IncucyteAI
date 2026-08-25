"""The acquisition protocol, drawn from the device's own metadata.

Four things here are load-bearing and each has its own gate:

* **an exposure belongs to a channel, not to a slot.** The vessel record names
  the colours and the scan payload times them, and the two are lined up by the
  device's own image type - never by position in a list, because a vessel that
  switched Color 1 off would then hand Color 2's exposure to Phase.
* **requested and achieved are different facts.** The schedule asks for a
  count; the scan times say what happened. Both are on the page, labelled.
* **nothing reads a pixel.** The whole drawing is metadata, and a change that
  fetched an image to draw a box would still be instant against the fake and
  ruinous over the site network.
* **the address never reaches the page.** A drawing is a file somebody emails.

Mirrors PyLV200's ``tests/test_protocol.py`` gate for gate, so a change to
either package's drawing shows up as a difference in the other's tests.
"""

import json
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from pyincucyte import IncucyteClient, cli, protocol as pt
from pyincucyte.errors import IncucyteError
from pyincucyte.models import Vessel

from fakes import FakeDevice, logged_in_store, patched, vessel_record

SCANS = ["2026-03-01T09:00:00", "2026-03-01T12:00:00", "2026-03-01T15:00:00",
         "2026-03-02T09:00:00"]
WELLS = [(0, 0), (0, 1), (1, 0)]


class ProtocolTestCase(unittest.TestCase):
    """One vessel, three channels, three wells, four scans three hours apart."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = logged_in_store(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def device(self, **kwargs):
        kwargs.setdefault("scans", list(SCANS))
        kwargs.setdefault("wells", list(WELLS))
        kwargs.setdefault("channels", [1, 2, 3])
        kwargs.setdefault("sites_per_well", 2)
        kwargs.setdefault("stop_after_scan_count", 100)
        return FakeDevice(**kwargs)

    def read(self, device=None, **kwargs):
        device = device or self.device()
        client = IncucyteClient("10.0.0.1", store=self.store)
        with patched(device):
            return client.protocol(38, **kwargs)

    def run_cli(self, argv, device=None):
        """Run one command against the fake, returning (exit code, stdout)."""
        import io
        from contextlib import redirect_stdout

        device = device or self.device()
        store = self.store
        buffer = io.StringIO()
        with patched(device), mock.patch.object(
                cli, "make_client",
                lambda args: IncucyteClient("10.0.0.1", store=store)):
            with redirect_stdout(buffer):
                code = cli.main(argv)
        return code, buffer.getvalue()


# ---------------------------------------------------------------------------
# gate 1: what it read
# ---------------------------------------------------------------------------

class WhatItReadTests(ProtocolTestCase):
    def test_the_channels_are_the_device_s_own_in_acquisition_order(self):
        found = self.read()
        self.assertEqual([s.label for s in found.steps],
                         ["Phase", "GFP", "mCherry"])
        self.assertEqual([s.image_type for s in found.steps], [1, 2, 3])
        self.assertEqual([s.name_source for s in found.steps], ["vessel"] * 3)

    def test_each_colour_carries_its_own_exposure_stare_and_band(self):
        gfp, cherry = self.read().steps[1:]
        self.assertEqual(gfp.exposure_s, 0.3)
        self.assertEqual(gfp.exposure, "300 ms")
        self.assertEqual(cherry.exposure, "400 ms")
        self.assertEqual(gfp.stare_text, "stare 180 ms")
        self.assertEqual(gfp.filter_text, "524 nm GCU")
        self.assertEqual(cherry.filter_text, "635 nm RCU")

    def test_an_exposure_is_paired_by_image_type_and_not_by_slot(self):
        """The one that matters. A vessel with Color 1 switched off must not
        hand Color 2's 400 ms to Phase - nothing downstream could tell."""
        device = self.device(channels=[1, 3])
        found = self.read(device)
        self.assertEqual([s.image_type for s in found.steps], [1, 3])
        phase, cherry = found.steps
        self.assertIsNone(phase.exposure_s)
        self.assertEqual(cherry.exposure, "400 ms")
        self.assertEqual(cherry.filter_text, "635 nm RCU")

    def test_phase_states_no_exposure_rather_than_inventing_one(self):
        phase = self.read().steps[0]
        self.assertIsNone(phase.exposure_s)
        self.assertEqual(phase.detail, ())
        self.assertEqual(phase.label, "Phase")

    def test_a_name_given_on_the_command_line_wins_and_says_so(self):
        found = self.read(names=["Phase", "Cry1-GFP", "Per2-mCherry"],
                          name_source="the command line")
        self.assertEqual([s.label for s in found.steps],
                         ["Phase", "Cry1-GFP", "Per2-mCherry"])
        self.assertEqual(found.sources["channel_names"], "the command line")
        self.assertIn("channel names the command line", pt._provenance(found))
        # ...and the device's own name is still recorded beside it.
        self.assertEqual(found.steps[1].plan_name, "Green")

    def test_the_wells_are_the_scan_pattern_s_and_the_sites_are_counted(self):
        found = self.read()
        self.assertEqual(list(found.wells), ["A1", "A2", "B1"])
        self.assertEqual(found.sites_per_well, 2)
        self.assertEqual(found.frames_per_cycle, 3 * 3 * 2)

    def test_the_instrument_and_the_plate_are_on_the_page(self):
        found = self.read()
        self.assertEqual(found.objective, "4x")
        self.assertIn("Optical Module", found.optics)
        self.assertEqual(found.plate, "24-well Sarstedt")
        self.assertAlmostEqual(found.pixel_size_um, 2.824051)
        self.assertEqual(found.started, datetime(2026, 3, 1, 9, 0))


# ---------------------------------------------------------------------------
# gate 2: requested against achieved
# ---------------------------------------------------------------------------

class RequestedAgainstAchievedTests(ProtocolTestCase):
    def test_a_requested_count_and_an_achieved_cadence_are_kept_apart(self):
        found = self.read()
        self.assertEqual(found.repeat_times, 100)          # asked for
        self.assertEqual(found.interval_s, 10800.0)        # median gap: 3 h
        self.assertEqual(found.acquired, 4)
        self.assertAlmostEqual(found.progress, 0.04)

    def test_the_cadence_is_the_median_gap_not_the_mean(self):
        """An instrument that paused overnight leaves one enormous gap, and a
        mean over it describes no cadence the run ever used. The manifest
        derives it the same way, so the two must agree."""
        paused = ["2026-03-01T09:00:00", "2026-03-01T10:00:00",
                  "2026-03-01T11:00:00", "2026-03-02T09:00:00"]
        found = self.read(self.device(scans=paused))
        self.assertEqual(found.interval_s, 3600.0)

    def test_a_schedule_the_scan_times_disagree_with_is_reported(self):
        found = self.read(self.device(schedule_job={"IntervalMinutes": 30}))
        self.assertEqual(found.cycle_s, 1800.0)
        self.assertGreater(found.drift, 1.0)
        self.assertTrue(any("achieved interval is the one that is true" in n
                            for n in found.notes))

    def test_no_schedule_says_so_rather_than_inventing_a_cadence(self):
        found = self.read(self.device(stop_after_scan_count=None))
        self.assertIsNone(found.cycle_s)
        self.assertIsNone(found.repeat_times)
        self.assertIsNone(found.progress)
        self.assertTrue(any("no stop-after schedule" in n
                            for n in found.notes))

    def test_a_device_wide_cadence_admits_that_it_is_device_wide(self):
        """Scan times come from a route that takes a date, not a vessel."""
        found = self.read()
        self.assertTrue(any("device-wide" in n for n in found.notes))

    def test_without_the_scan_progress_is_absent_rather_than_guessed(self):
        found = self.read(scan=False)
        self.assertIsNone(found.acquired)
        self.assertIsNone(found.interval_s)
        self.assertIsNone(found.live)
        self.assertEqual(found.steps[1].exposure, "300 ms")   # the plan remains

    def test_a_well_the_scan_missed_is_named_as_missing(self):
        device = self.device(
            wells_for={SCANS[-1]: [(0, 0)]})
        found = self.read(device)
        self.assertEqual(list(found.wells), ["A1", "A2", "B1"])
        self.assertEqual(list(found.imaged_wells), ["A1"])
        self.assertTrue(any("skipped the rest" in n for n in found.notes))

    def test_the_instrument_s_own_activity_is_reported_but_not_attributed(self):
        found = self.read(self.device(activity="Scanning"))
        self.assertTrue(found.live)
        self.assertEqual(found.activity, "Scanning")
        self.assertTrue(any("does not say which vessel" in n
                            for n in found.notes))


# ---------------------------------------------------------------------------
# gate 3: the read budget
# ---------------------------------------------------------------------------

class ReadBudgetTests(ProtocolTestCase):
    def test_drawing_a_protocol_fetches_no_image(self):
        """Blunt on purpose. The whole page is metadata; a change that fetched
        a tile to draw a box would still be instant against the fake."""
        device = self.device()

        def refuse(*args, **kwargs):
            raise AssertionError("a protocol drawing fetched an image")

        device.fetch_image = refuse
        found = self.read(device)
        self.assertEqual(found.acquired, 4)
        self.assertTrue(found.svg())
        self.assertEqual(device.fetches, [])

    def test_no_scan_sweeps_less_and_reads_no_status(self):
        """``scan=False`` cannot avoid finding a scan - a payload needs a
        moment to ask for - but it does avoid sweeping the run's whole
        lifetime, and it never asks the instrument what it is doing."""
        cheap, full = self.device(), self.device()
        self.read(cheap, scan=False)
        self.read(full)

        def days(device):
            return sum(1 for route, _ in device.calls
                       if route == "Scans/AllScanTimes")

        self.assertLess(days(cheap), days(full))
        self.assertNotIn("Device/Status/GetDeviceStatusUpdate",
                         [route for route, _ in cheap.calls])

    def test_a_status_route_that_is_unwell_still_yields_a_drawing(self):
        """The one call that reports faults must not be able to hide the plan."""
        device = self.device(
            refuse={"Device/Status/GetDeviceStatusUpdate": "DeviceOffline"})
        found = self.read(device)
        self.assertIsNone(found.live)
        self.assertEqual(len(found.steps), 3)


# ---------------------------------------------------------------------------
# gate 4: the drawings
# ---------------------------------------------------------------------------

class DrawingTests(ProtocolTestCase):
    def test_the_terminal_drawing_is_pure_ascii(self):
        """A Windows console still on a code page raises UnicodeEncodeError on
        a box-drawing character, which turns a diagram into a traceback beside
        the instrument."""
        text = "\n".join(self.read().lines())
        text.encode("ascii")                     # raises if anything crept in
        self.assertIn("+--", text)
        self.assertIn("->", text)
        for name in ("Phase", "GFP", "mCherry"):
            self.assertIn(name, text)

    def test_the_terminal_drawing_stays_inside_the_width_it_is_given(self):
        found = self.read()
        for width in (70, 96, 140):
            self.assertLessEqual(max(len(line) for line in
                                     found.lines(width=width)), width)

    def test_a_chain_too_wide_for_the_terminal_becomes_a_list(self):
        """Trimming a chain to fit says something other than what the run does."""
        lines = "\n".join(self.read().lines(width=52))
        self.assertIn("1. Phase", lines)

    def test_the_svg_is_well_formed_and_carries_every_channel(self):
        root = ET.fromstring(self.read().svg())
        self.assertTrue(root.tag.endswith("svg"))
        texts = [node.text or "" for node in root.iter()
                 if node.tag.endswith("text")]
        for name in ("Phase", "GFP", "mCherry"):
            self.assertIn(name, texts)
        self.assertTrue(any("Time loop" in t for t in texts))

    def test_every_well_names_itself_without_a_legend(self):
        """Twenty-four identical squares are useless; twenty-four that say
        their own name on hover answer "which one did it skip"."""
        found = self.read(self.device(wells_for={SCANS[-1]: [(0, 0)]}))
        titles = [node.text or "" for node in ET.fromstring(found.svg()).iter()
                  if node.tag.endswith("title")]
        self.assertTrue(any(t.startswith("A2") for t in titles))
        self.assertTrue(any("no image in this scan" in t for t in titles))

    def test_both_themes_draw_and_differ(self):
        found = self.read()
        light, dark = found.svg("light"), found.svg("dark")
        ET.fromstring(light), ET.fromstring(dark)
        self.assertNotEqual(light, dark)
        self.assertIn(pt.LIGHT["page"].lower(), light.lower())
        self.assertIn(pt.DARK["page"].lower(), dark.lower())

    def test_the_svg_needs_no_plotting_stack(self):
        """The packaged app excludes matplotlib outright, so an SVG that needed
        it would be a drawing the desktop app could never produce."""
        found = self.read()
        with mock.patch.dict(sys.modules, {"matplotlib": None,
                                           "matplotlib.pyplot": None}):
            self.assertIn("<svg", found.svg())

    def test_a_name_too_long_for_its_box_never_leaves_it(self):
        long_names = ["Phase", "Cry1-GFP long reporter line", "mCherry"]
        found = self.read(names=long_names)
        width, _height, shapes = pt.layout(found)
        for text in [s for s in shapes if s["kind"] == "text"]:
            run = pt._text_width(text["text"], text["size"],
                                 text["weight"] == "bold")
            left = {"start": text["x"], "middle": text["x"] - run / 2,
                    "end": text["x"] - run}[text["anchor"]]
            self.assertGreaterEqual(left, 0, text["text"])
            self.assertLessEqual(left + run, width, text["text"])
        boxes = [s for s in shapes if s["kind"] == "rect"]
        self.assertLessEqual(max(b["x"] + b["w"] for b in boxes), width)

    def test_a_colour_comes_from_the_name_and_falls_back_to_the_channel(self):
        self.assertEqual(pt.channel_colour("Cry1-GFP"),
                         pt.CHANNEL_COLOURS["light"]["green"])
        self.assertEqual(pt.channel_colour("Per2-mCherry"),
                         pt.CHANNEL_COLOURS["light"]["red"])
        # A name that says nothing falls back to the device's own numbering.
        self.assertEqual(pt.channel_colour("C2", image_type=3),
                         pt.CHANNEL_COLOURS["light"]["red"])

    def test_saving_picks_the_format_from_the_extension(self):
        found = self.read()
        svg = found.save(self.tmp / "run.svg")
        self.assertTrue(svg.read_text(encoding="utf-8").startswith("<svg"))
        with self.assertRaises(IncucyteError):
            found.save(self.tmp / "run.tif")

    def test_a_folder_gets_a_file_named_after_the_vessel(self):
        written = self.read().save(self.tmp / "figures")
        self.assertEqual(written.parent.name, "figures")
        self.assertTrue(written.name.endswith("-protocol.svg"))
        self.assertIn("38", written.name)

    def test_a_png_goes_through_matplotlib(self):
        try:
            import matplotlib                                   # noqa: F401
        except ImportError:
            self.skipTest("matplotlib is the optional figure extra")
        written = self.read().save(self.tmp / "run.png")
        self.assertTrue(written.exists())
        self.assertGreater(written.stat().st_size, 0)

    def test_a_run_with_no_channel_still_draws_and_keeps_its_notes(self):
        """The run that most needs its notes read is the one with nothing on
        it. The note marker used to be built from the chain loop's own counter,
        which vanished silently here and raised NameError with no chain."""
        vessel = Vessel.from_record(vessel_record())
        found = pt.read_protocol(vessel, {}, scan=False)
        self.assertEqual(found.steps, ())
        texts = [node.text or "" for node in ET.fromstring(found.svg()).iter()
                 if node.tag.endswith("text")]
        self.assertTrue(any("not known" in t for t in texts))
        self.assertTrue(any(t.startswith("** ") for t in texts))

    def test_a_note_is_marked_on_the_page_not_only_coloured(self):
        found = self.read()
        texts = [node.text or "" for node in ET.fromstring(found.svg()).iter()
                 if node.tag.endswith("text")]
        self.assertTrue(any(t.startswith("** ") for t in texts),
                        "every note starts with the marker")

    def test_the_address_is_never_drawn(self):
        """A drawing is a file somebody emails to a collaborator."""
        found = self.read()
        page = found.svg() + "\n".join(found.lines())
        self.assertNotIn("10.0.0.1", page)


# ---------------------------------------------------------------------------
# gate 5: all three front ends
# ---------------------------------------------------------------------------

class FrontEndTests(ProtocolTestCase):
    def test_the_command_prints_the_drawing_and_writes_a_file(self):
        out = self.tmp / "figures"
        code, said = self.run_cli(["protocol", "-v", "38", "-o", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("Time loop", said)
        self.assertIn("mCherry", said)
        self.assertEqual(len(list(out.glob("*-protocol.svg"))), 1)

    def test_the_json_carries_where_every_value_came_from(self):
        code, said = self.run_cli(["--json", "protocol", "-v", "38"])
        self.assertEqual(code, 0)
        payload = json.loads(said)
        self.assertEqual(payload["vessel_id"], 38)
        self.assertEqual(payload["sources"]["channel_names"], "vessel")
        self.assertEqual(payload["sources"]["exposure"], "scan payload")
        self.assertEqual(payload["interval_s"], 10800.0)
        self.assertEqual([s["name"] for s in payload["steps"]],
                         ["Phase", "GFP", "mCherry"])
        self.assertEqual(payload["steps"][1]["exposure"], "300 ms")

    def test_the_dark_flag_reaches_the_file(self):
        target = self.tmp / "dark.svg"
        code, _said = self.run_cli(["protocol", "-v", "38", "-o", str(target),
                                    "--dark"])
        self.assertEqual(code, 0)
        self.assertIn(pt.DARK["page"].lower(),
                      target.read_text(encoding="utf-8").lower())

    def test_channel_names_typed_on_the_command_line_reach_the_drawing(self):
        code, said = self.run_cli(["--json", "protocol", "-v", "38",
                                   "--channel-names", "Phase,Cry1,Per2"])
        self.assertEqual(code, 0)
        payload = json.loads(said)
        self.assertEqual([s["name"] for s in payload["steps"]],
                         ["Phase", "Cry1", "Per2"])
        self.assertEqual(payload["sources"]["channel_names"], "the command line")

    def test_no_scan_on_the_command_line_reaches_the_client(self):
        device = self.device()
        code, said = self.run_cli(["--json", "protocol", "-v", "38",
                                   "--no-scan"], device=device)
        self.assertEqual(code, 0)
        payload = json.loads(said)
        self.assertIsNone(payload["acquired"])
        self.assertIsNone(payload["interval_s"])
        self.assertNotIn("Device/Status/GetDeviceStatusUpdate",
                         [route for route, _ in device.calls])

    def test_the_api_and_the_command_line_reach_the_same_drawing(self):
        """House rule 2: anything user-visible is reachable three ways, and the
        three must agree."""
        found = self.read()
        code, said = self.run_cli(["--json", "protocol", "-v", "38"])
        self.assertEqual(code, 0)
        payload = json.loads(said)
        self.assertEqual(payload["steps"], [s.to_dict() for s in found.steps])
        self.assertEqual(payload["wells"], list(found.wells))
        self.assertEqual(payload["frames_per_cycle"], found.frames_per_cycle)

    def test_the_desktop_app_reaches_it_too(self):
        """The GUI's third way in: a menu action, a worker, and a window."""
        from pyincucyte.gui import app as app_mod

        self.assertTrue(hasattr(app_mod.App, "_view_protocol"))
        self.assertTrue(hasattr(app_mod.App, "_protocol_worker"))
        self.assertTrue(hasattr(app_mod.App, "_save_protocol_worker"))
        # The worker must go through the client, so cancelling works and the
        # window is only ever opened via _post.
        source = (Path(app_mod.__file__)).read_text(encoding="utf-8")
        self.assertIn("self.client.protocol(", source)
        self.assertIn("self._post(self._open_protocol_window", source)


# ---------------------------------------------------------------------------
# parity with PyLV200
# ---------------------------------------------------------------------------

class ParityTests(ProtocolTestCase):
    """The two packages draw the same picture, so the names must match.

    Round one and two of the parity work went the other way - PyLV200 catching
    up to this package.  This one reverses it, and these are the spellings a
    script ported between them relies on.
    """

    def test_the_module_exposes_pylv200_s_names(self):
        for name in ("Protocol", "Step", "read_protocol", "layout", "to_svg",
                     "save_figure", "ascii_drawing", "channel_colour",
                     "palette", "LIGHT", "DARK", "THEMES", "CYCLE_TOLERANCE"):
            self.assertIn(name, pt.__all__, name)
            self.assertTrue(hasattr(pt, name), name)

    def test_a_well_answers_to_position_as_well(self):
        """An Incucyte has wells where an LV200 has fields of view; a script
        written against one still reads against the other."""
        found = self.read()
        self.assertEqual(found.positions, found.wells)
        self.assertEqual(found.n_positions, found.n_wells)

    def test_the_shared_shape_vocabulary_is_the_same(self):
        _width, _height, shapes = pt.layout(self.read())
        kinds = {s["kind"] for s in shapes}
        self.assertLessEqual(kinds, {"rect", "text", "line", "poly",
                                     "polyline", "circle"})

    def test_the_protocol_prints_itself(self):
        found = self.read()
        self.assertEqual(str(found), "\n".join(found.lines()))
        self.assertIn("3 channel(s)", repr(found))


if __name__ == "__main__":
    unittest.main()
