"""Reduced-resolution tiles from the Incucyte viewer's private routes.

Scientific export deliberately stays in :mod:`pyincucyte.engine`.  This module
only implements the read-only routes used by the installed Incucyte viewer:
``Images/phasesourcecompressed`` and ``Images/colorsourcecompressed``.  They
return compressed TIFF *tiles*, not complete TIFF files, so their metadata is
needed to reconstruct the native low-resolution array.

The routes are undocumented and vary between device releases.  Every public
operation therefore feature-detects them and raises :class:`PyramidUnavailable`
when the response cannot be proved safe.  Callers can then use the established
full-image export route without changing it.
"""

from __future__ import annotations

import base64
import gzip
import io
import logging
import os
import sys
import threading
import zlib
from dataclasses import dataclass
from math import ceil

from . import engine
from .errors import ApiError, TokenExpiredError

log = logging.getLogger("pyincucyte.pyramid")

PHASE_ROUTE = "Images/phasesourcecompressed"
COLOR_ROUTE = "Images/colorsourcecompressed"
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_EDGE = 512


class PyramidUnavailable(RuntimeError):
    """The connected device cannot safely provide reduced viewer tiles."""


class PyramidDecodeError(PyramidUnavailable):
    """A viewer tile response was present but could not be decoded."""


@dataclass(frozen=True)
class PyramidLevel:
    """One advertised resolution level, in native pixels."""

    level: int
    width: int
    height: int

    @property
    def longest_edge(self):
        return max(self.width, self.height)


@dataclass
class PyramidFrame:
    """One reconstructed reduced frame and its transfer accounting."""

    array: object = None
    source_bytes: int = 0
    level: int = 0
    tile_count: int = 0
    error: str = ""

    @property
    def ok(self):
        return self.array is not None and not self.error


@dataclass
class _DecodedTile:
    array: object
    source_bytes: int
    image_width: int
    image_height: int
    tile_width: int
    tile_height: int


def _first(mapping, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(str(name).lower())
        if value is not None:
            return value
    return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _find_named(obj, name):
    """Return the first value under ``name`` in a nested response."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() == name.lower():
                return value
        for value in obj.values():
            found = _find_named(value, name)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_named(value, name)
            if found is not None:
                return found
    return None


def parse_pyramid_levels(metadata):
    """Parse ``ImagePyramidLevels`` from known Incucyte response variants.

    The 2021C and 2022B clients serialise each level as ``Level0`` plus a
    ``Size_pixels`` block.  More descriptive aliases are accepted so a later
    server does not need a PyIncucyte release merely for renamed JSON fields.
    Invalid or zero-sized entries are ignored.
    """
    raw = _find_named(metadata, "ImagePyramidLevels")
    if raw is None:
        raw = metadata
    raw = engine.unpack_values(raw)
    if isinstance(raw, dict):
        for key in ("Levels", "Items", "Values"):
            candidate = _first(raw, key)
            if isinstance(candidate, list):
                raw = candidate
                break
    if not isinstance(raw, list):
        return []

    levels = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        level = _as_int(_first(entry, "Level0", "PyramidLevel", "Level"), -1)
        size = _first(entry, "Size_pixels", "SizePixels", "ImageSize", "Size",
                      default={})
        if not isinstance(size, dict):
            size = {}
        width = _as_int(_first(size, "Width", "ImageWidth",
                              default=_first(entry, "Width", "ImageWidth")))
        height = _as_int(_first(size, "Height", "ImageLength",
                               default=_first(entry, "Height", "ImageLength")))
        if level >= 0 and width > 0 and height > 0:
            levels.append(PyramidLevel(level, width, height))
    unique = {level.level: level for level in levels}
    return [unique[number] for number in sorted(unique)]


def choose_level(levels, max_edge=DEFAULT_MAX_EDGE):
    """Choose the least data that remains close to ``max_edge`` pixels.

    Dimensions, rather than level numbering, decide.  That keeps the choice
    correct if a future device numbers its pyramid in the opposite direction.
    """
    levels = list(levels or ())
    if not levels:
        raise PyramidUnavailable("the scan advertises no image pyramid levels")
    limit = max(1, int(max_edge))
    fitting = [level for level in levels if level.longest_edge <= limit]
    if fitting:
        return max(fitting, key=lambda level: (level.longest_edge,
                                               level.width * level.height))
    return min(levels, key=lambda level: (level.longest_edge,
                                          level.width * level.height))


def route_for(image_type):
    """Return the private route for an Incucyte image type number."""
    return PHASE_ROUTE if int(image_type) == 1 else COLOR_ROUTE


def request_spec(request, pyramid_level, tile_id=0):
    """Serialise one vendor ``DvScanVesselImageRequestSpec`` request."""
    return {
        "ImageTypeCode": int(request["img_type"]),
        "Identifier": {
            "ScanVesselIdentifier": {
                "VesselID": int(request["vessel_id"]),
                "ScanDateTime": str(request["scan_time"]),
                "Swell": {
                    "RowZeroBased": int(request["row"]),
                    "ColumnZeroBased": int(request["col"]),
                },
                "SwellSite": {"ValueZeroBased": int(request.get("site", 0))},
            },
            "JobID": 0,
        },
        "PyramidLevel": int(getattr(pyramid_level, "level", pyramid_level)),
        "TileId": int(tile_id),
        "DataRequestType": {"IsScannedVesselRequest": True},
    }


def _looks_like_tile(value):
    if not isinstance(value, dict):
        return False
    names = {str(key).lower() for key in value}
    return "bytes" in names and bool(
        names & {"bitspersample", "imagewidth", "tilewidth", "imagelength"})


def response_tiles(response, expected=None):
    """Return ordered tile records from known Web API response wrappers."""
    value = engine.unpack_values(response)
    if isinstance(value, dict) and "Data" in value:
        value = value["Data"]
    value = engine.unpack_values(value)

    if _looks_like_tile(value):
        items = [value]
    elif isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = None
        for key in ("CompressedTiffTileData", "CompressedTiffTileDatas",
                    "Tiles", "Results", "Items", "Values"):
            candidate = _first(value, key)
            candidate = engine.unpack_values(candidate)
            if isinstance(candidate, list):
                items = candidate
                break
            if _looks_like_tile(candidate):
                items = [candidate]
                break
        if items is None:
            raise PyramidUnavailable("viewer response contains no tile list")
    else:
        raise PyramidUnavailable("viewer response contains no tile data")

    # A missing frame is often represented by null in the ordered list.  Keep
    # it in place; silently compacting the list would attach later pixels to
    # the wrong scan time.
    if expected is not None and len(items) != int(expected):
        raise PyramidUnavailable(
            f"viewer returned {len(items)} tiles for {int(expected)} requests")
    return items


def _byte_payload(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        encoded = value.split(",", 1)[-1] if value.startswith("data:") else value
        try:
            return base64.b64decode(encoded, validate=False)
        except Exception as exc:
            raise PyramidDecodeError(f"tile Bytes is not valid base64: {exc}") from exc
    value = engine.unpack_values(value)
    if isinstance(value, list):
        try:
            return bytes(int(part) & 0xFF for part in value)
        except (TypeError, ValueError) as exc:
            raise PyramidDecodeError("tile Bytes contains non-byte values") from exc
    raise PyramidDecodeError("tile response has no byte payload")


def _decode_image_file(raw):
    """Decode a complete TIFF/PNG/JPEG fixture, or return None."""
    signatures = (b"II", b"MM", b"\x89PNG", b"\xff\xd8")
    if not raw.startswith(signatures):
        return None
    try:
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            array = np.asarray(image)
        if array.ndim > 2:
            array = array[..., 0]
        return array.copy()
    except Exception as exc:
        raise PyramidDecodeError(f"could not decode image tile: {exc}") from exc


def _raw_array(raw, width, height, bits):
    """Decode uncompressed bytes with known dimensions, or return None."""
    import numpy as np

    dtype = np.uint8 if bits <= 8 else np.dtype("<u2")
    needed = int(width) * int(height) * dtype.itemsize
    if width <= 0 or height <= 0 or len(raw) != needed:
        return None
    return np.frombuffer(raw, dtype=dtype).reshape(height, width).copy()


_VENDOR_LOCK = threading.Lock()


def _vendor_decode(raw, *, bits, image_width, image_height,
                   tile_width, tile_height):
    """Use the installed viewer's own TIFF-tile decoder when available."""
    install = engine.find_incucyte_install()
    if install is None:
        raise PyramidDecodeError(
            "compressed tile needs the installed Incucyte client decoder")

    with _VENDOR_LOCK:
        try:
            import clr
            from System import Array, Byte, UInt16
            from System.Reflection import Assembly

            roots = [install]
            dlls = install / "Dlls"
            if dlls.is_dir():
                roots.extend(path for path, _dirs, _files in os.walk(dlls))
            for root in roots:
                root = str(root)
                if root not in sys.path:
                    sys.path.append(root)
                if root not in os.environ.get("PATH", "").split(os.pathsep):
                    os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")

            libtiff = dlls / "LibTiff.Net" / "BitMiracle.LibTiff.NET.dll"
            if libtiff.is_file():
                Assembly.LoadFrom(str(libtiff))
            Assembly.LoadFrom(str(install / "Essen.dll"))
            from Essen.Drawing.Tiffs import CompressedTiffTileData, TiffTileReader

            payload = CompressedTiffTileData(
                Array[Byte](raw), Byte(int(bits)),
                UInt16(int(image_height)), UInt16(int(image_width)),
                UInt16(int(tile_height)), UInt16(int(tile_width)))
            decoded = TiffTileReader.DecompressTiffTileData(payload)
            data = bytes(decoded.Bytes)
            rows, cols = int(decoded.NRows), int(decoded.NCols)
            step = int(decoded.StepBytes)
        except Exception as exc:
            raise PyramidDecodeError(f"vendor tile decoder failed: {exc}") from exc

    import numpy as np

    dtype = np.uint8 if bits <= 8 else np.dtype("<u2")
    if rows <= 0 or cols <= 0 or step < cols * dtype.itemsize:
        raise PyramidDecodeError("vendor tile decoder returned invalid dimensions")
    if len(data) < rows * step:
        raise PyramidDecodeError("vendor tile decoder returned truncated pixels")
    stride = step // dtype.itemsize
    return np.frombuffer(data, dtype=dtype, count=rows * stride).reshape(
        rows, stride)[:, :cols].copy()


def decode_tile(record):
    """Decode one ``CompressedTiffTileData`` record into a native array."""
    if not isinstance(record, dict):
        raise PyramidDecodeError("viewer returned a missing tile")
    nested = _first(record, "CompressedTiffTileData")
    if isinstance(nested, dict):
        record = nested
    raw = _byte_payload(_first(record, "Bytes"))
    bits = _as_int(_first(record, "BitsPerSample"), 8)
    image_width = _as_int(_first(record, "ImageWidth", "Width"))
    image_height = _as_int(_first(record, "ImageLength", "ImageHeight", "Height"))
    tile_width = _as_int(_first(record, "TileWidth"), image_width)
    tile_height = _as_int(_first(record, "TileLength", "TileHeight"), image_height)
    if bits not in (8, 16):
        raise PyramidDecodeError(f"unsupported tile bit depth {bits}")
    if min(image_width, image_height, tile_width, tile_height) <= 0:
        raise PyramidDecodeError("tile response has invalid dimensions")

    array = _decode_image_file(raw)
    if array is None:
        array = _raw_array(raw, tile_width, tile_height, bits)
    if array is None:
        for decompress in (zlib.decompress, gzip.decompress):
            try:
                unpacked = decompress(raw)
            except Exception:
                continue
            array = _decode_image_file(unpacked)
            if array is None:
                array = _raw_array(unpacked, tile_width, tile_height, bits)
            if array is not None:
                break
    if array is None:
        array = _vendor_decode(
            raw, bits=bits, image_width=image_width, image_height=image_height,
            tile_width=tile_width, tile_height=tile_height)

    return _DecodedTile(
        array=array, source_bytes=len(raw), image_width=image_width,
        image_height=image_height, tile_width=tile_width,
        tile_height=tile_height)


def _stitch(tiles):
    """Stitch row-major tiles, cropping padding at the image boundary."""
    import numpy as np

    if not tiles:
        raise PyramidDecodeError("no tiles to stitch")
    first = tiles[0]
    columns = max(1, ceil(first.image_width / first.tile_width))
    rows = max(1, ceil(first.image_height / first.tile_height))
    if len(tiles) != rows * columns:
        raise PyramidDecodeError(
            f"need {rows * columns} tiles for this level, got {len(tiles)}")
    canvas = np.zeros((rows * first.tile_height, columns * first.tile_width),
                      dtype=first.array.dtype)
    for tile_id, tile in enumerate(tiles):
        row, col = divmod(tile_id, columns)
        height = min(tile.array.shape[0], first.tile_height)
        width = min(tile.array.shape[1], first.tile_width)
        canvas[row * first.tile_height:row * first.tile_height + height,
               col * first.tile_width:col * first.tile_width + width] = (
                   tile.array[:height, :width])
    return canvas[:first.image_height, :first.image_width]


def compare_arrays(candidate, reference):
    """Report which simple orientation best matches a full-image reference."""
    import numpy as np

    candidate = np.asarray(candidate)
    reference = np.asarray(reference)
    if candidate.ndim > 2:
        candidate = candidate[..., 0]
    if reference.ndim > 2:
        reference = reference[..., 0]
    rows = np.linspace(0, reference.shape[0] - 1, candidate.shape[0]).astype(int)
    cols = np.linspace(0, reference.shape[1] - 1, candidate.shape[1]).astype(int)
    sampled = reference[np.ix_(rows, cols)]
    variants = {
        "native": candidate,
        "flip vertical": np.flipud(candidate),
        "flip horizontal": np.fliplr(candidate),
        "rotate 180": np.flipud(np.fliplr(candidate)),
    }

    def correlation(left, right):
        left = left.astype("float64", copy=False).ravel()
        right = right.astype("float64", copy=False).ravel()
        left -= left.mean()
        right -= right.mean()
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        return float(np.dot(left, right) / denominator) if denominator else 1.0

    scores = {name: correlation(array, sampled)
              for name, array in variants.items()}
    orientation = max(scores, key=scores.get)
    return {"orientation": orientation,
            "correlation": round(scores[orientation], 6)}


def _unsupported(exc):
    # The shared API layer represents HTTP 401 as TokenExpiredError before the
    # route-specific transport sees the status code.  The full-image fallback
    # can still distinguish a genuinely expired session from a viewer route
    # that this user is not authorised to call.
    if isinstance(exc, TokenExpiredError):
        return True
    if isinstance(exc, ApiError):
        return exc.status_code in (400, 401, 403, 404, 405)
    message = str(exc).lower()
    return any(token in message for token in (
        "not found", "unauthor", "forbidden", "unknown route", "404", "405"))


class PyramidTransport:
    """Session-scoped, capability-detecting viewer tile transport."""

    def __init__(self, client, batch_size=DEFAULT_BATCH_SIZE):
        self.client = client
        self.batch_size = max(1, int(batch_size))
        self._capabilities = {PHASE_ROUTE: None, COLOR_ROUTE: None}
        self._lock = threading.Lock()

    @property
    def capabilities(self):
        return dict(self._capabilities)

    def _send(self, route, specs):
        try:
            response = self.client.call(route, specs, unpack=False)
        except Exception as exc:
            if _unsupported(exc):
                self._capabilities[route] = False
                raise PyramidUnavailable(f"{route} is not supported: {exc}") from exc
            raise
        self._capabilities[route] = True
        return response

    def _post(self, route, specs):
        state = self._capabilities.get(route)
        if state is False:
            raise PyramidUnavailable(f"{route} is unavailable in this session")
        if state is None:
            # Static well previews may start several workers together.  Only
            # one of them should probe a route that may not exist; after a
            # successful first response, normal tile calls remain concurrent.
            with self._lock:
                state = self._capabilities.get(route)
                if state is False:
                    raise PyramidUnavailable(
                        f"{route} is unavailable in this session")
                if state is None:
                    return self._send(route, specs)
        return self._send(route, specs)

    def _fetch_specs(self, route, specs, cancel=None):
        records = []
        for start in range(0, len(specs), self.batch_size):
            if cancel is not None and cancel.is_set():
                raise PyramidUnavailable("viewer tile request was cancelled")
            batch = specs[start:start + self.batch_size]
            response = self._post(route, batch)
            records.extend(response_tiles(response, expected=len(batch)))
        return records

    def fetch_frame(self, request, level, *, cancel=None):
        """Fetch one reduced frame, including every tile when needed."""
        route = route_for(request["img_type"])
        first_record = self._fetch_specs(
            route, [request_spec(request, level, 0)], cancel=cancel)[0]
        if first_record is None:
            return PyramidFrame(level=level.level, error="viewer returned no tile")
        first = decode_tile(first_record)
        columns = max(1, ceil(first.image_width / first.tile_width))
        rows = max(1, ceil(first.image_height / first.tile_height))
        count = rows * columns
        tiles = [first]
        if count > 1:
            specs = [request_spec(request, level, tile_id)
                     for tile_id in range(1, count)]
            for record in self._fetch_specs(route, specs, cancel=cancel):
                tiles.append(decode_tile(record))
        return PyramidFrame(
            array=_stitch(tiles), source_bytes=sum(tile.source_bytes for tile in tiles),
            level=level.level, tile_count=count)

    def fetch_many(self, requests, levels, *, cancel=None):
        """Fetch frames in order; errors remain attached to their own frame."""
        requests = list(requests)
        if isinstance(levels, PyramidLevel):
            levels = [levels] * len(requests)
        else:
            levels = list(levels)
        if len(levels) != len(requests):
            raise ValueError("one pyramid level is required per frame request")
        results = [None] * len(requests)
        grouped = {PHASE_ROUTE: [], COLOR_ROUTE: []}
        for index, request in enumerate(requests):
            grouped[route_for(request["img_type"])].append(index)

        for route, indices in grouped.items():
            if not indices:
                continue
            specs = [request_spec(requests[index], levels[index], 0)
                     for index in indices]
            records = self._fetch_specs(route, specs, cancel=cancel)
            for index, record in zip(indices, records):
                level = levels[index]
                if record is None:
                    results[index] = PyramidFrame(
                        level=level.level, error="viewer returned no tile")
                    continue
                try:
                    first = decode_tile(record)
                    columns = max(1, ceil(first.image_width / first.tile_width))
                    rows = max(1, ceil(first.image_height / first.tile_height))
                    count = rows * columns
                    tiles = [first]
                    if count > 1:
                        more = [request_spec(requests[index], level, tile_id)
                                for tile_id in range(1, count)]
                        tiles.extend(decode_tile(item) for item in self._fetch_specs(
                            route, more, cancel=cancel))
                    results[index] = PyramidFrame(
                        array=_stitch(tiles),
                        source_bytes=sum(tile.source_bytes for tile in tiles),
                        level=level.level, tile_count=count)
                except PyramidUnavailable:
                    raise
                except Exception as exc:
                    results[index] = PyramidFrame(level=level.level,
                                                  error=str(exc))
        return [result for result in results if result is not None]

    def close(self):
        """Release session-local state; pooled HTTPS ownership stays with client."""
        with self._lock:
            self._capabilities = {PHASE_ROUTE: None, COLOR_ROUTE: None}


__all__ = [
    "PHASE_ROUTE", "COLOR_ROUTE", "DEFAULT_BATCH_SIZE", "DEFAULT_MAX_EDGE",
    "PyramidUnavailable", "PyramidDecodeError", "PyramidLevel", "PyramidFrame",
    "PyramidTransport", "parse_pyramid_levels", "choose_level", "route_for",
    "request_spec", "response_tiles", "decode_tile",
    "compare_arrays",
]
