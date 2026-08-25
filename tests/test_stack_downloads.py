import io
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
import tifffile

from pyincucyte import engine as downloader


def _tiff_bytes(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="TIFF")
    return buffer.getvalue()


class StackDownloadTests(unittest.TestCase):
    def _with_fake_payloads(self, payloads, callback):
        original_fetch = downloader._fetch_scan_vessel_image_bytes

        def fake_fetch(host, token, item, max_retries=3):
            return payloads[(item["scan_time"], item["img_type"])], None

        downloader._fetch_scan_vessel_image_bytes = fake_fetch
        try:
            callback()
        finally:
            downloader._fetch_scan_vessel_image_bytes = original_fetch

    def test_time_stack_download_writes_imagej_tyx(self):
        arrays = [
            np.full((3, 4), value, dtype=np.uint16)
            for value in (1, 2, 3)
        ]
        payloads = {
            (f"t{index}", 1): _tiff_bytes(array)
            for index, array in enumerate(arrays)
        }

        def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "stack.tif"
                item = {
                    "fname": "stack.tif",
                    "fpath": path,
                    "state_key": "stack",
                    "frames": [
                        {"scan_time": f"t{index}", "img_type": 1}
                        for index in range(len(arrays))
                    ],
                    "scan_times": [f"t{index}" for index in range(len(arrays))],
                    "labels": ["Phase"],
                    "channel_hyperstack": False,
                }
                progress = []

                fname, size = downloader._download_time_stack(
                    "host", "token", item, None, threading.Lock(),
                    unit_progress_callback=lambda label, byte_count: progress.append(label))

                self.assertEqual(fname, "stack.tif")
                self.assertGreater(size, 0)
                self.assertEqual(len(progress), len(arrays))
                with tifffile.TiffFile(path) as tif:
                    self.assertEqual(tif.series[0].shape, (3, 3, 4))
                    self.assertEqual(tif.series[0].axes, "TYX")
                    np.testing.assert_array_equal(tif.asarray(), np.stack(arrays, axis=0))

        self._with_fake_payloads(payloads, run_test)

    def test_time_stack_fetches_frames_lazily_while_writing(self):
        frame_count = 8
        events = []
        original_fetch = downloader._fetch_scan_vessel_image_bytes
        original_decode = downloader._tiff_bytes_to_array
        original_write = downloader._write_imagej_stack

        def fake_fetch(host, token, item, max_retries=3):
            frame_index = int(item["scan_time"][1:])
            events.append(("fetch", frame_index))
            return str(frame_index).encode("ascii"), None

        def fake_decode(payload):
            frame_index = int(payload.decode("ascii"))
            events.append(("decode", frame_index))
            return np.full((2, 3), frame_index, dtype=np.uint16)

        def fake_write(path, arrays, shape, dtype, axes, labels=None):
            self.assertEqual(shape, (frame_count, 2, 3))
            self.assertEqual(np.dtype(dtype), np.dtype(np.uint16))
            self.assertEqual(axes, "TYX")
            for array in arrays:
                frame_index = int(array[0, 0])
                events.append(("write", frame_index))
            Path(path).write_bytes(b"stack")

        downloader._fetch_scan_vessel_image_bytes = fake_fetch
        downloader._tiff_bytes_to_array = fake_decode
        downloader._write_imagej_stack = fake_write
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "streamed.tif"
                item = {
                    "fname": "streamed.tif",
                    "fpath": path,
                    "state_key": "streamed",
                    "frames": [
                        {"scan_time": f"t{index}", "img_type": 1}
                        for index in range(frame_count)
                    ],
                    "scan_times": [f"t{index}" for index in range(frame_count)],
                    "labels": ["Phase"],
                    "channel_hyperstack": False,
                }

                fname, size = downloader._download_time_stack(
                    "host", "token", item, None, threading.Lock())

                self.assertEqual(fname, "streamed.tif")
                self.assertEqual(size, len(b"stack"))
        finally:
            downloader._fetch_scan_vessel_image_bytes = original_fetch
            downloader._tiff_bytes_to_array = original_decode
            downloader._write_imagej_stack = original_write

        expected_events = []
        for frame_index in range(frame_count):
            expected_events.extend([
                ("fetch", frame_index),
                ("decode", frame_index),
                ("write", frame_index),
            ])
        self.assertEqual(events, expected_events)

    def test_time_stack_stop_event_cancels_between_frames(self):
        arrays = [
            np.full((3, 4), value, dtype=np.uint16)
            for value in range(4)
        ]
        payloads = {
            (f"t{index}", 1): _tiff_bytes(array)
            for index, array in enumerate(arrays)
        }

        def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "cancelled.tif"
                item = {
                    "fname": "cancelled.tif",
                    "fpath": path,
                    "state_key": "cancelled",
                    "frames": [
                        {"scan_time": f"t{index}", "img_type": 1}
                        for index in range(len(arrays))
                    ],
                    "scan_times": [f"t{index}" for index in range(len(arrays))],
                    "labels": ["Phase"],
                    "channel_hyperstack": False,
                }
                stop_event = threading.Event()
                progress = []

                def on_progress(label, byte_count):
                    progress.append(label)
                    stop_event.set()

                fname, error = downloader._download_time_stack(
                    "host", "token", item, None, threading.Lock(),
                    unit_progress_callback=on_progress,
                    stop_event=stop_event)

                self.assertIsNone(fname)
                self.assertIsNone(error)
                self.assertEqual(len(progress), 1)
                self.assertFalse(path.exists())

        self._with_fake_payloads(payloads, run_test)

    def test_time_hyperstack_download_writes_imagej_tcyx(self):
        scan_times = ["t0", "t1"]
        channels = [1, 2]
        payloads = {}
        expected_timepoints = []
        for time_index, scan_time in enumerate(scan_times):
            channel_arrays = []
            for channel_index, img_type in enumerate(channels):
                array = np.full((2, 3), time_index * 10 + channel_index,
                                dtype=np.uint16)
                payloads[(scan_time, img_type)] = _tiff_bytes(array)
                channel_arrays.append(array)
            expected_timepoints.append(np.stack(channel_arrays, axis=0))
        expected = np.stack(expected_timepoints, axis=0)

        def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "hyper.tif"
                item = {
                    "fname": "hyper.tif",
                    "fpath": path,
                    "state_key": "hyper",
                    "frames": [
                        {
                            "scan_time": scan_time,
                            "channels": [
                                {"scan_time": scan_time, "img_type": img_type}
                                for img_type in channels
                            ],
                        }
                        for scan_time in scan_times
                    ],
                    "scan_times": scan_times,
                    "labels": ["Phase", "Green"],
                    "channel_hyperstack": True,
                }
                progress = []

                fname, size = downloader._download_time_stack(
                    "host", "token", item, None, threading.Lock(),
                    unit_progress_callback=lambda label, byte_count: progress.append(label))

                self.assertEqual(fname, "hyper.tif")
                self.assertGreater(size, 0)
                self.assertEqual(len(progress), len(scan_times) * len(channels))
                with tifffile.TiffFile(path) as tif:
                    self.assertEqual(tif.series[0].shape, (2, 2, 2, 3))
                    self.assertEqual(tif.series[0].axes, "TCYX")
                    self.assertEqual(tif.imagej_metadata["Labels"],
                                     ["Phase", "Green"] * 2)
                    np.testing.assert_array_equal(tif.asarray(), expected)

        self._with_fake_payloads(payloads, run_test)

    def test_a_whole_time_hyperstack_matches_pylv200s_single_z_layout(self):
        """A singleton Z axis is omitted by ImageJ, so both writers should
        produce exactly the same TIFF when given the same planes and names."""
        data = np.arange(3 * 2 * 4 * 5, dtype=np.uint16).reshape(3, 2, 4, 5)
        labels = ["Phase", "GFP"]

        with tempfile.TemporaryDirectory() as tmpdir:
            pyincucyte_path = Path(tmpdir) / "pyincucyte.tif"
            pylv200_path = Path(tmpdir) / "pylv200.tif"
            downloader._write_imagej_stack(
                pyincucyte_path, iter(data), data.shape, data.dtype,
                "TCYX", labels)
            tifffile.imwrite(
                pylv200_path, data[:, None], imagej=True,
                metadata={
                    "axes": "TZCYX",
                    "mode": "grayscale",
                    "Labels": labels * data.shape[0],
                })

            self.assertEqual(pyincucyte_path.read_bytes(),
                             pylv200_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
