"""Growing a time stack in place instead of rewriting it whole.

Two things have to hold, and the second matters more than the first: an
extended stack must be indistinguishable from one written whole, and anything
that casts doubt on that must fall back to writing it whole rather than guess.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import tifffile

from pyincucyte import ExportOptions, IncucyteClient, engine, tiffstack
from pyincucyte.errors import StackNotExtendable

from fakes import FakeDevice, logged_in_store, patched

SHAPE = (6, 8)


def frame(value, shape=SHAPE, dtype=np.uint16):
    return np.full(shape, value, dtype=dtype)


def write_stack(path, planes, axes="TYX", labels=None, **kwargs):
    """Write a stack the same way the engine does, to append to afterwards."""
    metadata = {"axes": axes, "mode": "grayscale"}
    if labels is not None:
        metadata["Labels"] = list(labels)
    tifffile.imwrite(str(path), np.stack(planes), imagej=True,
                     metadata=metadata, photometric="minisblack", **kwargs)


class StackSurgeryTests(unittest.TestCase):
    """The file-level operation: does it produce the same file?"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def stack(self, name, count, **kwargs):
        path = self.tmp / name
        write_stack(path, [frame(i) for i in range(count)], **kwargs)
        return path

    def test_the_pixels_already_there_never_move(self):
        path = self.stack("a.tif", 3)
        layout = tiffstack.read_layout(path)
        start, end = layout.first_offset, layout.end_of_pixels
        before = path.read_bytes()[start:end]

        tiffstack.append_planes(path, [frame(3), frame(4)])

        self.assertEqual(path.read_bytes()[start:end], before)

    def test_an_extended_stack_matches_one_written_whole(self):
        grown = self.stack("grown.tif", 3)
        tiffstack.append_planes(grown, [frame(3), frame(4)])
        whole = self.tmp / "whole.tif"
        write_stack(whole, [frame(i) for i in range(5)])

        self.assertTrue(np.array_equal(tifffile.imread(str(grown)),
                                       tifffile.imread(str(whole))))
        with tifffile.TiffFile(str(grown)) as extended:
            with tifffile.TiffFile(str(whole)) as rewritten:
                self.assertEqual(extended.imagej_metadata,
                                 rewritten.imagej_metadata)
                self.assertEqual(extended.series[0].shape,
                                 rewritten.series[0].shape)

    def test_a_two_channel_stack_grows_by_whole_timepoints(self):
        path = self.tmp / "hyper.tif"
        write_stack(path, [np.stack([frame(0), frame(1)]),
                           np.stack([frame(10), frame(11)])],
                    axes="TCYX", labels=["Phase", "Green"])

        tiffstack.append_planes(path, [frame(20), frame(21)],
                                labels=["Phase", "Green"])

        with tifffile.TiffFile(str(path)) as handle:
            self.assertEqual(handle.series[0].shape, (3, 2, *SHAPE))
            self.assertEqual(handle.series[0].axes, "TCYX")
            self.assertEqual(handle.imagej_metadata["Labels"],
                             ["Phase", "Green"])

    def test_half_a_timepoint_is_refused(self):
        path = self.tmp / "hyper.tif"
        write_stack(path, [np.stack([frame(0), frame(1)])],
                    axes="TCYX", labels=["Phase", "Green"])
        with self.assertRaises(StackNotExtendable):
            tiffstack.append_planes(path, [frame(20)])

    def test_the_pixel_size_survives(self):
        """A stack that lost its calibration would be worse than a rewrite."""
        path = self.tmp / "scaled.tif"
        write_stack(path, [frame(0), frame(1)],
                    resolution=(2.5, 2.5), resolutionunit="MICROMETER")
        tiffstack.append_planes(path, [frame(2)])
        with tifffile.TiffFile(str(path)) as handle:
            for page in handle.pages:
                self.assertEqual(page.tags[282].value, (5, 2))   # 2.5 as a ratio
                self.assertEqual(page.tags[296].value, 5)        # micrometer

    def test_a_frame_of_the_wrong_size_is_refused(self):
        path = self.stack("a.tif", 2)
        with self.assertRaises(StackNotExtendable):
            tiffstack.append_planes(path, [frame(9, shape=(7, 8))])

    def test_a_frame_of_the_wrong_type_is_refused(self):
        path = self.stack("a.tif", 2)
        with self.assertRaises(StackNotExtendable):
            tiffstack.append_planes(path, [frame(9, dtype=np.uint8)])

    def test_a_calibrated_float_stack_grows_the_same_way(self):
        path = self.tmp / "float.tif"
        write_stack(path, [frame(i, dtype=np.float32) for i in range(2)])
        tiffstack.append_planes(path, [frame(2, dtype=np.float32)])
        grown = tifffile.imread(str(path))
        self.assertEqual(grown.shape, (3, *SHAPE))
        self.assertEqual(grown.dtype, np.float32)

    def test_a_compressed_stack_is_refused(self):
        path = self.tmp / "zipped.tif"
        tifffile.imwrite(str(path), np.stack([frame(0), frame(1)]), imagej=True,
                         compression="zlib", metadata={"axes": "TYX"},
                         photometric="minisblack")
        with self.assertRaises(StackNotExtendable):
            tiffstack.read_layout(path)

    def test_a_file_that_is_not_there_is_refused(self):
        with self.assertRaises(StackNotExtendable):
            tiffstack.read_layout(self.tmp / "absent.tif")

    def test_a_truncated_file_is_refused(self):
        path = self.stack("a.tif", 3)
        path.write_bytes(path.read_bytes()[:40])
        with self.assertRaises(StackNotExtendable):
            tiffstack.read_layout(path)

    def test_more_than_one_buffered_write_is_refused(self):
        """A week collected at once is a rebuild, not an append."""
        path = self.stack("a.tif", 2)
        stride = tiffstack.read_layout(path).stride
        with self.assertRaises(StackNotExtendable):
            tiffstack.append_planes(path, [frame(i) for i in range(2, 6)],
                                    max_bytes=stride * 2)

    def test_appending_over_and_over_stays_readable(self):
        path = self.stack("a.tif", 1)
        for value in range(1, 12):
            tiffstack.append_planes(path, [frame(value)])
        grown = tifffile.imread(str(path))
        self.assertEqual(grown.shape, (12, *SHAPE))
        self.assertEqual([int(plane.flat[0]) for plane in grown], list(range(12)))


class AppendablePrefixTests(unittest.TestCase):
    """Deciding whether the file on disk is the start of the stack we want."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "stack.tif"
        write_stack(self.path, [frame(0), frame(1)])
        self.times = ["t1", "t2", "t3"]
        self.recorded = {"scan_times": ["t1", "t2"], "channels": ["Phase"],
                         "channel_hyperstack": False}

    def tearDown(self):
        self._tmp.cleanup()

    def prefix(self, **changes):
        info = dict(self.recorded)
        info.update(changes)
        return engine.appendable_prefix(self.path, info, self.times, ["Phase"])

    def test_a_matching_prefix_is_the_frames_already_written(self):
        self.assertEqual(self.prefix(), 2)

    def test_a_new_frame_in_front_of_the_old_ones_is_not_a_prefix(self):
        """A widened window puts frames before the ones on disk."""
        self.times = ["t0", "t1", "t2", "t3"]
        self.assertEqual(self.prefix(), 0)

    def test_a_different_channel_set_is_not_the_same_stack(self):
        self.assertEqual(self.prefix(channels=["Green"]), 0)

    def test_a_hyperstack_ledger_entry_is_not_a_plain_stack(self):
        self.assertEqual(self.prefix(channel_hyperstack=True), 0)

    def test_nothing_new_is_nothing_to_add(self):
        self.times = ["t1", "t2"]
        self.assertEqual(self.prefix(), 0)

    def test_a_ledger_that_disagrees_with_the_file_is_not_trusted(self):
        self.assertEqual(self.prefix(scan_times=["t1"]), 0)

    def test_a_missing_file_is_not_a_prefix(self):
        self.path.unlink()
        self.assertEqual(self.prefix(), 0)

    def test_an_unreadable_file_is_not_a_prefix(self):
        self.path.write_bytes(b"not a tiff at all")
        self.assertEqual(self.prefix(), 0)


class DownloadTests(unittest.TestCase):
    """End to end, through the client, against the fake device."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.device = FakeDevice(
            scans=["2026-03-01T09:00:00", "2026-03-01T12:00:00"],
            wells=((0, 0),), channels=(1,),
            # Every frame gets its own value, so the order is checkable.
            pixel_for=lambda img_type, scan: int(str(scan or "")[8:10] or 0) * 100
            + int(str(scan or "")[11:13] or 0))
        self.client = IncucyteClient("10.0.0.1", store=logged_in_store(self.tmp))
        self.out = self.tmp / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **changes):
        base = dict(output=str(self.out), vessels=[38], channels="phase",
                    layout="time_stack", start_from="2026-03-01",
                    end_at="2026-03-03")
        base.update(changes)
        return ExportOptions(**base)

    def stack_path(self):
        found = sorted(self.out.glob("*_timestack*.tif"))
        self.assertEqual(len(found), 1, f"expected one stack, got {found}")
        return found[0]

    def new_scan(self, when="2026-03-02T09:00:00"):
        self.device.scans.append(when)
        self.device.fetches.clear()

    def test_a_new_scan_is_added_to_the_file(self):
        with patched(self.device):
            self.client.download(self.options())
            self.new_scan()
            result = self.client.download(self.options())

        self.assertEqual(result.errors, [])
        grown = tifffile.imread(str(self.stack_path()))
        self.assertEqual(grown.shape, (3, 6, 8))
        self.assertEqual([int(plane.flat[0]) for plane in grown],
                         [109, 112, 209])
        self.assertEqual(len(self.device.fetches), 1)   # only the new frame

    def test_the_frames_already_written_are_not_rewritten(self):
        with patched(self.device):
            self.client.download(self.options())
            path = self.stack_path()
            layout = tiffstack.read_layout(path)
            start, end = layout.first_offset, layout.end_of_pixels
            before = path.read_bytes()[start:end]

            self.new_scan()
            self.client.download(self.options())

        self.assertEqual(path.read_bytes()[start:end], before)

    def test_a_two_channel_stack_gains_a_whole_timepoint(self):
        self.device.channels = [1, 2]
        with patched(self.device):
            self.client.download(self.options(layout="time_channel_stack",
                                              channels="phase,green"))
            self.new_scan()
            self.client.download(self.options(layout="time_channel_stack",
                                              channels="phase,green"))
        with tifffile.TiffFile(str(self.stack_path())) as handle:
            self.assertEqual(handle.series[0].shape, (3, 2, 6, 8))
            self.assertEqual(handle.imagej_metadata["Labels"],
                             ["Phase", "Green"])
        self.assertEqual(len(self.device.fetches), 2)   # both channels, once

    def test_turning_it_off_writes_the_stack_whole(self):
        with patched(self.device):
            self.client.download(self.options(append_stacks=False))
            path = self.stack_path()
            layout = tiffstack.read_layout(path)
            before = path.read_bytes()[layout.first_offset:layout.end_of_pixels]

            self.new_scan()
            self.client.download(self.options(append_stacks=False))

        self.assertEqual(tifffile.imread(str(path)).shape, (3, 6, 8))
        after = path.read_bytes()[layout.first_offset:
                                  layout.first_offset + len(before)]
        self.assertEqual(after, before, "the pixels should be the same values")
        self.assertEqual(len(self.device.fetches), 1, "the cache still covers it")

    def test_a_widened_window_writes_the_stack_whole(self):
        """New frames in front of the old ones cannot be appended."""
        with patched(self.device):
            self.client.download(self.options(start_from="2026-03-01T12:00:00"))
            # tifffile drops the T axis from a stack holding one frame.
            self.assertEqual(tifffile.imread(str(self.stack_path())).shape,
                             (6, 8))
            self.device.fetches.clear()
            self.client.download(self.options(start_from="2026-03-01"))

        grown = tifffile.imread(str(self.stack_path()))
        self.assertEqual(grown.shape, (2, 6, 8))
        self.assertEqual([int(plane.flat[0]) for plane in grown], [109, 112])
        self.assertEqual(self.device.fetches, ["VID38_A1_phase_timestack.tif:"
                                               "2026-03-01T09:00:00"])

    def test_a_different_recipe_writes_its_own_file(self):
        """A processed stack is a different file, so it is never appended to."""
        self.device.calibration = {1: (2.0, 5.0, 0.0)}
        with patched(self.device):
            self.client.download(self.options())
            self.new_scan()
            self.client.download(self.options(calibrate=True))

        names = sorted(path.name for path in self.out.glob("*.tif"))
        self.assertEqual(names, ["VID38_A1_phase_timestack.tif",
                                 "VID38_A1_phase_timestack_cal.tif"])
        self.assertEqual(tifffile.imread(str(self.out / names[1])).dtype,
                         np.float32)

    def test_a_stack_that_will_not_open_is_rebuilt(self):
        with patched(self.device):
            self.client.download(self.options())
            path = self.stack_path()
            path.write_bytes(b"II*\x00" + b"\x00" * 200)   # a ruined header

            self.new_scan()
            result = self.client.download(self.options())

        self.assertEqual(result.errors, [])
        self.assertEqual(tifffile.imread(str(path)).shape, (3, 6, 8))

    def test_a_download_that_fails_leaves_the_file_alone(self):
        """Nothing is written until every new plane is in hand."""
        with patched(self.device):
            self.client.download(self.options())
            path = self.stack_path()
            before = path.read_bytes()

            self.new_scan()
            self.device.missing_scans.add("2026-03-02T09:00:00")
            self.client.download(self.options())

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(tifffile.imread(str(path)).shape, (2, 6, 8))


if __name__ == "__main__":
    unittest.main()
