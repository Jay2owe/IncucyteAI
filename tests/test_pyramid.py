"""Private viewer tiles: request shape, decoding, batching, and failure safety."""

import base64
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from pyincucyte.errors import TokenExpiredError
from pyincucyte.pyramid import (
    COLOR_ROUTE, PHASE_ROUTE, PyramidLevel, PyramidTransport,
    PyramidUnavailable, choose_level, compare_arrays, decode_tile,
    parse_pyramid_levels,
    request_spec, response_tiles,
)

from fakes import tiff_bytes


def tile_record(value=7, shape=(6, 8), dtype=np.uint16):
    raw = tiff_bytes(value=value, shape=shape, dtype=dtype)
    return {
        "Bytes": base64.b64encode(raw).decode("ascii"),
        "BitsPerSample": np.dtype(dtype).itemsize * 8,
        "ImageLength": shape[0], "ImageWidth": shape[1],
        "TileLength": shape[0], "TileWidth": shape[1],
    }


class PyramidMetadataTests(unittest.TestCase):
    def test_real_vendor_level_shape_is_parsed(self):
        metadata = {"Data": {"ImagePyramidLevels": {"$values": [
            {"Level0": 0, "Size_pixels": {"Width": 1536, "Height": 1152}},
            {"Level0": 2, "Size_pixels": {"Width": 384, "Height": 288}},
        ]}}}
        self.assertEqual(parse_pyramid_levels(metadata), [
            PyramidLevel(0, 1536, 1152), PyramidLevel(2, 384, 288)])

    def test_level_choice_uses_dimensions_not_numbering(self):
        levels = [PyramidLevel(9, 1536, 1152),
                  PyramidLevel(3, 480, 360), PyramidLevel(1, 240, 180)]
        self.assertEqual(choose_level(levels, 512).level, 3)

    def test_request_matches_the_vendor_dv_scan_vessel_spec(self):
        request = {"vessel_id": 38, "scan_time": "2026-08-24T12:34:00",
                   "row": 0, "col": 1, "site": 2, "img_type": 3}
        spec = request_spec(request, PyramidLevel(4, 320, 240), tile_id=5)
        self.assertEqual(spec["ImageTypeCode"], 3)
        self.assertEqual(spec["PyramidLevel"], 4)
        self.assertEqual(spec["TileId"], 5)
        self.assertEqual(spec["Identifier"]["JobID"], 0)
        identifier = spec["Identifier"]["ScanVesselIdentifier"]
        self.assertEqual(identifier["VesselID"], 38)
        self.assertEqual(identifier["Swell"]["ColumnZeroBased"], 1)
        self.assertTrue(spec["DataRequestType"]["IsScannedVesselRequest"])


class PyramidDecodeTests(unittest.TestCase):
    def test_complete_tiff_fixture_decodes_without_vendor_assemblies(self):
        decoded = decode_tile(tile_record(value=19))
        self.assertEqual(decoded.array.shape, (6, 8))
        self.assertEqual(decoded.array.dtype, np.uint16)
        self.assertTrue(np.all(decoded.array == 19))
        self.assertGreater(decoded.source_bytes, 0)

    def test_response_order_keeps_a_missing_tile_in_place(self):
        items = response_tiles({"Data": {"$values": [tile_record(1), None,
                                                        tile_record(3)]}}, 3)
        self.assertIsNone(items[1])

    def test_wrong_response_count_is_not_silently_reassigned(self):
        with self.assertRaises(PyramidUnavailable):
            response_tiles({"Data": [tile_record()]}, expected=2)

    def test_orientation_comparison_detects_a_horizontal_flip(self):
        reference = np.arange(48, dtype=np.float32).reshape(6, 8)
        report = compare_arrays(np.fliplr(reference), reference)
        self.assertEqual(report["orientation"], "flip horizontal")
        self.assertAlmostEqual(report["correlation"], 1.0)


class RecordingTileClient:
    def __init__(self, refuse=False):
        self.calls = []
        self.refuse = refuse

    def call(self, route, payload, unpack=False):
        self.calls.append((route, payload))
        if self.refuse:
            raise RuntimeError("404 route not found")
        return {"Data": {"$values": [
            tile_record(value=spec["Identifier"]["ScanVesselIdentifier"]
                        ["Swell"]["ColumnZeroBased"] + 1)
            for spec in payload]}}


class PyramidTransportTests(unittest.TestCase):
    @staticmethod
    def requests(count, image_type=1):
        return [
            {"vessel_id": 38, "scan_time": f"2026-08-24T{i:02d}:00:00",
             "row": 0, "col": i % 4, "site": 0, "img_type": image_type}
            for i in range(count)
        ]

    def test_initial_tiles_are_batched_in_groups_of_32(self):
        client = RecordingTileClient()
        transport = PyramidTransport(client, batch_size=32)
        frames = transport.fetch_many(self.requests(65), PyramidLevel(2, 8, 6))
        self.assertEqual(len(frames), 65)
        self.assertEqual([len(payload) for _route, payload in client.calls],
                         [32, 32, 1])
        self.assertTrue(all(route == PHASE_ROUTE for route, _ in client.calls))
        self.assertTrue(all(frame.ok for frame in frames))

    def test_fluorescence_uses_the_colour_route(self):
        client = RecordingTileClient()
        PyramidTransport(client).fetch_many(
            self.requests(1, image_type=2), PyramidLevel(2, 8, 6))
        self.assertEqual(client.calls[0][0], COLOR_ROUTE)

    def test_unsupported_route_is_remembered_for_the_session(self):
        client = RecordingTileClient(refuse=True)
        transport = PyramidTransport(client)
        with self.assertRaises(PyramidUnavailable):
            transport.fetch_many(self.requests(1), PyramidLevel(2, 8, 6))
        with self.assertRaises(PyramidUnavailable):
            transport.fetch_many(self.requests(1), PyramidLevel(2, 8, 6))
        self.assertEqual(len(client.calls), 1)

    def test_unauthorised_route_is_also_a_fallback_capability_result(self):
        class UnauthorisedClient(RecordingTileClient):
            def call(self, route, payload, unpack=False):
                self.calls.append((route, payload))
                raise TokenExpiredError("Token expired or invalid")

        client = UnauthorisedClient()
        transport = PyramidTransport(client)
        with self.assertRaises(PyramidUnavailable):
            transport.fetch_many(self.requests(1), PyramidLevel(2, 8, 6))
        with self.assertRaises(PyramidUnavailable):
            transport.fetch_many(self.requests(1), PyramidLevel(2, 8, 6))
        self.assertEqual(len(client.calls), 1)

    def test_concurrent_workers_probe_an_unsupported_route_only_once(self):
        class SlowRefusal(RecordingTileClient):
            def call(self, route, payload, unpack=False):
                self.calls.append((route, payload))
                time.sleep(0.03)
                raise RuntimeError("404 route not found")

        client = SlowRefusal()
        transport = PyramidTransport(client)

        def fetch(_value):
            with self.assertRaises(PyramidUnavailable):
                transport.fetch_many(self.requests(1), PyramidLevel(2, 8, 6))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, range(4)))
        self.assertEqual(len(client.calls), 1)

    def test_cancelled_batch_sends_nothing(self):
        client = RecordingTileClient()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(PyramidUnavailable):
            PyramidTransport(client).fetch_many(
                self.requests(1), PyramidLevel(2, 8, 6), cancel=cancel)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
