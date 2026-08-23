"""Well specs, plate geometry, and the resume ledger."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import engine
from pyincucyte.state import STATE_FILENAME, NullStateStore, StateStore
from pyincucyte.wells import (
    all_wells, format_wells, guess_plate_size, normalise_wells, parse_wells,
    well_name, well_spec,
)


class PlateTests(unittest.TestCase):
    def test_plate_geometry_is_read_from_the_vessel_type(self):
        for name, expected in (("Sarstedt 24-well", (4, 6)),
                               ("Corning 96 well", (8, 12)),
                               ("Greiner 384-Well", (16, 24)),
                               ("Something unlabelled", (8, 12))):
            self.assertEqual(guess_plate_size(name), expected, name)

    def test_well_names_are_one_based_letters_and_numbers(self):
        self.assertEqual(well_name(0, 0), "A1")
        self.assertEqual(well_name(3, 11), "D12")


class WellSpecTests(unittest.TestCase):
    def test_runs_collapse_to_ranges_and_parse_back(self):
        wells = {(0, c) for c in range(6)} | {(1, 2)}
        spec = well_spec(wells)
        self.assertEqual(spec, "A1-A6,B3")
        self.assertEqual(parse_wells(spec), wells)

    def test_scattered_wells_round_trip_exactly(self):
        wells = {(0, 0), (0, 2), (0, 4), (3, 1)}
        self.assertEqual(parse_wells(well_spec(wells)), wells)

    def test_all_and_none_have_their_own_spellings(self):
        self.assertEqual(well_spec(None), "all")
        self.assertEqual(well_spec(set()), "")
        self.assertIsNone(parse_wells("all"))

    def test_a_full_plate_normalises_to_no_filter(self):
        self.assertIsNone(normalise_wells(all_wells(4, 6), rows=4, cols=6))
        self.assertEqual(normalise_wells({(0, 0)}, rows=4, cols=6), {(0, 0)})

    def test_names_and_tuples_are_both_accepted(self):
        self.assertEqual(normalise_wells(["A1", (1, 1)]), {(0, 0), (1, 1)})

    def test_format_wells_stays_short_for_big_selections(self):
        self.assertEqual(format_wells(None), "All wells")
        self.assertEqual(format_wells(set()), "No wells")
        text = format_wells(all_wells(8, 12))
        self.assertIn("96 wells", text)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ledger_lives_beside_the_images(self):
        store = StateStore.for_output(self.tmp, scope="folder")
        self.assertEqual(store.path, self.tmp / STATE_FILENAME)

    def test_writes_are_batched_until_flushed(self):
        store = StateStore(self.tmp / "ledger.json", flush_interval=999)
        store.as_dict()["downloaded"]["k"] = {"file": "a.tif"}
        store.mark_dirty()
        self.assertFalse(store.path.exists())     # still batched
        store.flush(force=True)
        self.assertEqual(json.loads(store.path.read_text())["downloaded"]["k"],
                         {"file": "a.tif"})

    def test_the_engine_persists_through_the_attached_store(self):
        store = StateStore(self.tmp / "ledger.json", flush_interval=999)
        state = store.as_dict()
        state["downloaded"]["k"] = {"file": "a.tif"}
        engine.persist_state(state)               # what the download loop calls
        store.flush(force=True)
        self.assertIn("k", json.loads(store.path.read_text())["downloaded"])

    def test_the_store_handle_is_never_written_to_disk(self):
        store = StateStore(self.tmp / "ledger.json")
        store.as_dict()["downloaded"]["k"] = {"file": "a.tif"}
        store.flush(force=True)
        self.assertEqual(set(json.loads(store.path.read_text())), {"downloaded"})

    def test_two_output_folders_keep_separate_ledgers(self):
        first = StateStore.for_output(self.tmp / "a", scope="folder")
        second = StateStore.for_output(self.tmp / "b", scope="folder")
        first.as_dict()["downloaded"]["k"] = {"file": "a.tif"}
        first.flush(force=True)
        self.assertEqual(len(second), 0)

    def test_auto_scope_carries_over_matching_global_entries(self):
        output = self.tmp / "out"
        output.mkdir()
        inside = output / "VID38_A1_1.tif"
        original_state_file = engine.STATE_FILE
        engine.STATE_FILE = self.tmp / "global.json"
        try:
            engine.STATE_FILE.write_text(json.dumps({"downloaded": {
                "mine": {"file": str(inside)},
                "elsewhere": {"file": str(self.tmp / "other" / "x.tif")},
            }}))
            store = StateStore.for_output(output, scope="auto")
        finally:
            engine.STATE_FILE = original_state_file
        self.assertIn("mine", store.entries)
        self.assertNotIn("elsewhere", store.entries)

    def test_pruning_forgets_files_that_have_been_deleted(self):
        store = StateStore(self.tmp / "ledger.json")
        present = self.tmp / "present.tif"
        present.write_bytes(b"x")
        store.data["downloaded"] = {
            "a": {"file": str(present)},
            "b": {"file": str(self.tmp / "gone.tif")},
        }
        self.assertEqual(store.prune_missing(), 1)
        self.assertEqual(list(store.entries), ["a"])

    def test_a_corrupt_ledger_is_ignored_rather_than_fatal(self):
        path = self.tmp / "ledger.json"
        path.write_text("{not json")
        self.assertEqual(len(StateStore(path)), 0)

    def test_scope_none_disables_resume_tracking(self):
        store = StateStore.for_output(self.tmp, scope="none")
        self.assertIsInstance(store, NullStateStore)
        store.as_dict()["downloaded"]["k"] = {}
        store.flush(force=True)
        self.assertFalse((self.tmp / STATE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
