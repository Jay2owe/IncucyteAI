"""The source-payload cache: what stops watch mode re-downloading everything."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte import ExportOptions, IncucyteClient
from pyincucyte.cache import (
    CACHE_DIRNAME, PayloadCache, cache_for_output, payload_key,
)

from fakes import FakeDevice, logged_in_store, patched


class KeyTests(unittest.TestCase):
    def test_the_key_identifies_one_payload_exactly(self):
        item = {"vessel_id": 38, "row": 1, "col": 2, "site": 0, "img_type": 2,
                "scan_time": "2026-03-01T09:00:00"}
        self.assertEqual(payload_key(item), "V38_r1_c2_s0_t2_20260301T090000.tif")

    def test_every_axis_changes_the_key(self):
        base = {"vessel_id": 38, "row": 0, "col": 0, "site": 0, "img_type": 1,
                "scan_time": "2026-03-01T09:00:00"}
        keys = {payload_key(base)}
        for field, value in (("row", 1), ("col", 1), ("site", 1),
                             ("img_type", 2), ("scan_time", "2026-03-01T12:00:00")):
            keys.add(payload_key({**base, field: value}))
        self.assertEqual(len(keys), 6)


class PolicyTests(unittest.TestCase):
    def test_auto_caches_only_the_layouts_that_rebuild_whole_files(self):
        self.assertIsNone(cache_for_output("o", "auto", "separate"))
        self.assertIsNone(cache_for_output("o", "auto", "channel_stack"))
        self.assertIsNotNone(cache_for_output("o", "auto", "time_stack"))
        self.assertIsNotNone(cache_for_output("o", "auto", "time_channel_stack"))

    def test_always_and_never_override_the_policy(self):
        self.assertIsNotNone(cache_for_output("o", "always", "separate"))
        self.assertIsNone(cache_for_output("o", "never", "time_stack"))

    def test_the_cache_sits_inside_the_output_folder(self):
        cache = cache_for_output("/data/run", "always", "separate")
        self.assertEqual(cache.root.name, CACHE_DIRNAME)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.cache = PayloadCache(Path(self._tmp.name) / "cache")
        self.item = {"vessel_id": 1, "row": 0, "col": 0, "site": 0,
                     "img_type": 1, "scan_time": "2026-03-01T09:00:00"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_stored_payload_comes_back_byte_for_byte(self):
        self.cache.put(self.item, b"II*\x00payload")
        self.assertEqual(self.cache.get(self.item), b"II*\x00payload")
        self.assertEqual(self.cache.hits, 1)

    def test_an_unknown_payload_is_a_miss_not_an_error(self):
        self.assertIsNone(self.cache.get(self.item))
        self.assertEqual(self.cache.misses, 1)

    def test_empty_payloads_are_not_stored(self):
        self.cache.put(self.item, b"")
        self.assertIsNone(self.cache.get(self.item))

    def test_an_unwritable_location_degrades_quietly(self):
        cache = PayloadCache(Path(self._tmp.name) / "file.txt" / "cache")
        Path(self._tmp.name, "file.txt").write_text("not a folder")
        cache.put(self.item, b"data")          # must not raise
        self.assertIsNone(cache.get(self.item))

    def test_sweeping_removes_stale_payloads(self):
        self.cache.put(self.item, b"data")
        self.assertEqual(self.cache.sweep(max_age_seconds=3600), 0)
        self.assertEqual(self.cache.sweep(max_age_seconds=0), 0)   # 0 disables
        self.assertEqual(self.cache.sweep(max_age_seconds=-1), 1)

    def test_clear_removes_everything(self):
        self.cache.put(self.item, b"data")
        self.cache.clear()
        self.assertEqual(self.cache.count(), 0)


class WatchEconomyTests(unittest.TestCase):
    """The behaviour that makes an unattended pipeline affordable."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.device = FakeDevice(scans=["2026-03-01T09:00:00",
                                        "2026-03-01T12:00:00"])
        self.client = IncucyteClient("10.0.0.1", store=logged_in_store(self.tmp))

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **changes):
        base = dict(output=str(self.tmp / "out"), vessels=[38], channels="phase",
                    layout="time_stack", start_from="2026-03-01",
                    end_at="2026-03-02")
        base.update(changes)
        return ExportOptions(**base)

    def test_a_new_scan_only_downloads_the_new_frames(self):
        with patched(self.device):
            self.client.download(self.options())
            first_pass = len(self.device.fetches)

            # A third scan lands; the stacks must be rebuilt to include it.
            self.device.scans.append("2026-03-02T09:00:00")
            self.device.fetches.clear()
            second = self.client.download(self.options())

        self.assertEqual(first_pass, 4)          # 2 wells x 2 scan times
        self.assertEqual(second.file_count, 2)   # both stacks rewritten
        # Only the two genuinely new frames crossed the network.
        self.assertEqual(len(self.device.fetches), 2)
        self.assertEqual(second.cache.hits, 4)

    def test_without_the_cache_every_frame_is_fetched_again(self):
        with patched(self.device):
            self.client.download(self.options(cache_payloads="never"))
            self.device.scans.append("2026-03-02T09:00:00")
            self.device.fetches.clear()
            self.client.download(self.options(cache_payloads="never"))
        self.assertEqual(len(self.device.fetches), 6)   # all of them, again

    def test_cached_bytes_produce_the_same_stack(self):
        import tifffile
        with patched(self.device):
            first = self.client.download(self.options())
            self.device.scans.append("2026-03-02T09:00:00")
            second = self.client.download(self.options())
        for written in second.files:
            with tifffile.TiffFile(written.path) as handle:
                self.assertEqual(handle.series[0].shape, (3, 6, 8))
        # Downloads finish in whatever order the threads land, so compare sets.
        self.assertEqual({f.path.name for f in first.files},
                         {f.path.name for f in second.files})

    def test_separate_layout_does_not_pay_for_a_cache_it_cannot_use(self):
        with patched(self.device):
            result = self.client.download(self.options(layout="separate"))
        self.assertIsNone(result.cache)
        self.assertFalse((self.tmp / "out" / CACHE_DIRNAME).exists())


if __name__ == "__main__":
    unittest.main()
