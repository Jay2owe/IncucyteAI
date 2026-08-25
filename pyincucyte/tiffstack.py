"""Extend an ImageJ time stack in place instead of rebuilding it whole.

A time stack has to contain every frame, so the arrival of one new scan
invalidates the file.  Rebuilt the obvious way that is a full rewrite on every
poll, and it gets worse as the experiment runs: five days in, a watch loop
rewrites gigabytes every hour to add three megabytes.

It does not have to.  ``tifffile`` lays an ImageJ stack out as

    [header][ ......... all pixel data ......... ][directory entries]

with the directory *after* the pixels.  So extending the file is: write the new
planes over the directory block, lay a fresh directory down at the new end, and
repoint the four-byte pointer in the header.  The existing pixel bytes never
move.  Think of it as a book whose index is at the back — adding a chapter
means writing the pages and reprinting the index, not the whole book.

That last step is the commit.  Until the header pointer changes the file still
describes the old frames, so a crash can only leave an unreadable stack in the
moment around one four-byte write, and even then nothing is lost: the payload
cache still holds every source image, so recovery is a rebuild.

Every check in here answers one question — can these planes be appended to this
file without changing what it means?  A "no" is never fatal.  It raises
:class:`StackNotExtendable`, and the caller writes the file whole, which is
what it did before this module existed.

Imported *by* engine.py at call time, so it must never import engine.
"""

import struct
from dataclasses import dataclass, field

from .errors import ExportError, StackNotExtendable

#: TIFF tags that only ever belong to the first page of an ImageJ stack.
FIRST_PAGE_ONLY = (270, 50838, 50839)

#: TIFF tags copied onto page 0 but not onto later pages.  Tifffile writes its
#: Software tag once; preserving that layout makes an appended stack expose
#: the same per-page tag sets as one freshly written whole.
FIRST_PAGE_KEEP = (305,)

TAG_DESCRIPTION = 270
TAG_STRIP_OFFSETS = 273
TAG_STRIP_BYTE_COUNTS = 279

TYPE_LONG = 4

#: Largest amount of new pixel data we will buffer to append.  Beyond this the
#: caller rebuilds instead: appending is for the one-to-three frames a watch
#: loop finds each poll, not for collecting a week in one go.
MAX_APPEND_BYTES = 64 * 1024 * 1024


def _type_sizes():
    """Byte width of each TIFF value type, taken from tifffile's own table."""
    from tifffile import TIFF

    return {code: struct.calcsize(fmt)
            for code, fmt in TIFF.DATA_FORMATS.items()}


@dataclass
class StackLayout:
    """Everything needed to add planes to an existing ImageJ stack."""

    byteorder: str
    width: int
    height: int
    dtype: object
    planes: int
    channels: int
    axes: str
    first_offset: int
    stride: int
    metadata: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)

    @property
    def frames(self):
        """Time points, which is planes divided by channels."""
        return self.planes // self.channels

    @property
    def end_of_pixels(self):
        return self.first_offset + self.planes * self.stride

    @property
    def plane_shape(self):
        return (self.height, self.width)

    def check_plane(self, array):
        """Raise unless this array can be written as one plane of this stack."""
        if tuple(array.shape) != self.plane_shape:
            raise StackNotExtendable(
                f"frame is {tuple(array.shape)}, the stack holds "
                f"{self.plane_shape}")
        if array.dtype != self.dtype:
            raise StackNotExtendable(
                f"frame is {array.dtype}, the stack holds {self.dtype}")

    def room_for(self, plane_count, max_bytes=MAX_APPEND_BYTES):
        """Is appending this many planes worth doing in one buffered write?"""
        return 0 < plane_count * self.stride <= max_bytes


def read_layout(path):
    """Describe an existing ImageJ stack, or say why it cannot be extended.

    Raises :class:`StackNotExtendable` for anything unexpected, including a
    file that is not there.  The caller always has the same answer to that:
    write the stack whole.
    """
    import tifffile

    try:
        handle = tifffile.TiffFile(str(path))
    except (OSError, ValueError, tifffile.TiffFileError) as exc:
        raise StackNotExtendable(f"cannot read {path}: {exc}") from exc

    with handle as tif:
        if tif.is_bigtiff:
            raise StackNotExtendable("BigTIFF offsets are 8 bytes wide")
        if not tif.is_imagej:
            raise StackNotExtendable("not an ImageJ stack")
        if len(tif.series) != 1:
            raise StackNotExtendable(f"{len(tif.series)} series, expected one")

        pages = list(tif.pages)
        if not pages:
            raise StackNotExtendable("no pages")

        first = pages[0]
        for page in pages:
            if page.compression != 1:
                raise StackNotExtendable("compressed pages")
            if page.samplesperpixel != 1:
                raise StackNotExtendable("more than one sample per pixel")
            if len(page.dataoffsets) != 1:
                raise StackNotExtendable("pages are split into strips")
            if (page.imagewidth, page.imagelength) != (first.imagewidth,
                                                       first.imagelength):
                raise StackNotExtendable("pages differ in size")

        stride = first.databytecounts[0]
        offsets = [page.dataoffsets[0] for page in pages]
        if any(page.databytecounts[0] != stride for page in pages):
            raise StackNotExtendable("planes differ in size")
        expected = [offsets[0] + i * stride for i in range(len(pages))]
        if offsets != expected:
            raise StackNotExtendable("pixel data is not one contiguous block")

        series = tif.series[0]
        axes = series.axes
        if "C" in axes:
            channels = series.shape[axes.index("C")]
        else:
            channels = 1
        if channels < 1 or len(pages) % channels:
            raise StackNotExtendable(
                f"{len(pages)} planes do not divide into {channels} channels")

        return StackLayout(
            byteorder="<" if tif.byteorder == "<" else ">",
            width=first.imagewidth,
            height=first.imagelength,
            dtype=first.dtype,
            planes=len(pages),
            channels=channels,
            axes=axes,
            first_offset=offsets[0],
            stride=stride,
            metadata=dict(tif.imagej_metadata or {}),
            tags=_copy_tags(tif, first),
        )


def _copy_tags(tif, page):
    """Read page 0's tags as raw bytes, so nothing tifffile wrote is lost.

    Copying verbatim rather than rebuilding a hand-picked set keeps the pixel
    size (``XResolution``), the writing software and anything else the original
    carried — a stack that loses its spatial calibration on the first append
    would be worse than one rewritten whole.
    """
    sizes = _type_sizes()
    handle = tif.filehandle
    copied = []
    for tag in page.tags:
        width = sizes.get(int(tag.dtype))
        if width is None:
            raise StackNotExtendable(f"unknown tag type in tag {tag.code}")
        length = width * tag.count
        handle.seek(tag.valueoffset)
        raw = handle.read(length)
        if len(raw) != length:
            raise StackNotExtendable(f"truncated value for tag {tag.code}")
        copied.append((int(tag.code), int(tag.dtype), int(tag.count), raw))
    return copied


# -- writing ---------------------------------------------------------------


def _pack_ifd(byteorder, entries, ifd_offset, next_offset):
    """Serialise one image file directory at a known offset."""
    entries = sorted(entries, key=lambda entry: entry[0])
    header_size = 2 + 12 * len(entries) + 4
    body = bytearray()
    trailing = bytearray()
    body += struct.pack(byteorder + "H", len(entries))
    for code, dtype, count, raw in entries:
        body += struct.pack(byteorder + "HHI", code, dtype, count)
        if len(raw) <= 4:
            body += raw.ljust(4, b"\0")
        else:
            body += struct.pack(byteorder + "I",
                                ifd_offset + header_size + len(trailing))
            trailing += raw
            if len(trailing) % 2:
                trailing += b"\0"
    body += struct.pack(byteorder + "I", next_offset)
    return bytes(body) + bytes(trailing)


def _ifd_size(entries):
    size = 2 + 12 * len(entries) + 4
    for _, _, _, raw in entries:
        if len(raw) > 4:
            size += len(raw) + (len(raw) % 2)
    return size


def _long(byteorder, value):
    return struct.pack(byteorder + "I", int(value))


def _describe(layout, planes, channels):
    """Build the ImageJ description string for the extended stack."""
    from tifffile import imagej_description

    frames = planes // channels
    shape = (frames, channels, layout.height, layout.width)
    extra = {key: value for key, value in layout.metadata.items()
             if key in ("mode", "unit", "spacing", "finterval", "fps", "loop")}
    extra.pop("loop", None)
    return imagej_description(shape, axes="TCYX", **extra)


def _metadata_tags(byteorder, labels):
    """The IJMetadata pair carrying the channel labels, or nothing."""
    if not labels:
        return []
    from tifffile import imagej_metadata_tag

    tags = imagej_metadata_tag({"Labels": list(labels)}, byteorder)
    return [(int(code), int(dtype), int(count), bytes(value))
            for code, dtype, count, value, _writeonce in tags]


def _page_entries(layout, offset, description=None, metadata_tags=()):
    """Tags for one page: page 0's set, with what changes per page replaced."""
    byteorder = layout.byteorder
    entries = []
    is_first = description is not None
    for code, dtype, count, raw in layout.tags:
        if code in FIRST_PAGE_ONLY:
            continue
        if code in FIRST_PAGE_KEEP and not is_first:
            continue
        if code == TAG_STRIP_OFFSETS:
            entries.append((code, TYPE_LONG, 1, _long(byteorder, offset)))
        elif code == TAG_STRIP_BYTE_COUNTS:
            entries.append((code, TYPE_LONG, 1, _long(byteorder, layout.stride)))
        else:
            entries.append((code, dtype, count, raw))
    if description is not None:
        text = description.encode("ascii", "replace") + b"\0"
        entries.append((TAG_DESCRIPTION, 2, len(text), text))
        entries.extend(metadata_tags)
    return entries


def append_planes(path, planes, labels=None, max_bytes=MAX_APPEND_BYTES):
    """Add planes to an existing ImageJ stack without moving its pixel data.

    ``planes`` is an iterable of 2-D arrays in file order — for a two-channel
    stack that is both channels of one time point, then both of the next.
    Returns the number of planes added.

    Everything that could refuse is checked before a byte is written.  Once
    writing starts a failure raises :class:`ExportError`, and the file should
    be treated as suspect: delete it and write it whole.
    """
    import numpy as np

    layout = read_layout(path)

    buffered = []
    for array in planes:
        array = np.ascontiguousarray(array)
        layout.check_plane(array)
        buffered.append(array)
        if not layout.room_for(len(buffered), max_bytes):
            raise StackNotExtendable(
                f"{len(buffered)} new planes is more than one buffered write "
                f"({max_bytes // (1024 * 1024)} MB); rebuild instead")
    if not buffered:
        raise StackNotExtendable("no new planes")

    byteorder = layout.byteorder
    total = layout.planes + len(buffered)
    channels = layout.channels
    if total % channels:
        raise StackNotExtendable(
            f"{len(buffered)} new planes do not complete a time point "
            f"of {channels} channels")

    description = _describe(layout, total, channels)
    metadata_tags = _metadata_tags(byteorder, labels or layout.metadata.get("Labels"))

    first_entries = _page_entries(layout, layout.first_offset,
                                  description=description,
                                  metadata_tags=metadata_tags)
    other_entries = _page_entries(layout, layout.first_offset)
    first_size = _ifd_size(first_entries)
    other_size = _ifd_size(other_entries)

    try:
        with open(path, "r+b") as handle:
            handle.seek(layout.end_of_pixels)
            for array in buffered:
                data = array.tobytes()
                if len(data) != layout.stride:
                    raise ExportError(
                        f"frame is {len(data)} bytes, the stack holds "
                        f"{layout.stride}")
                handle.write(data)

            directory_start = handle.tell()
            if directory_start % 2:
                handle.write(b"\0")
                directory_start += 1

            blob = bytearray()
            cursor = directory_start
            for index in range(total):
                size = first_size if index == 0 else other_size
                offset = layout.first_offset + index * layout.stride
                entries = _page_entries(
                    layout, offset,
                    description=description if index == 0 else None,
                    metadata_tags=metadata_tags)
                nxt = 0 if index == total - 1 else cursor + size
                blob += _pack_ifd(byteorder, entries, cursor, nxt)
                cursor += size
            handle.seek(directory_start)
            handle.write(bytes(blob))
            handle.truncate(handle.tell())
            handle.flush()

            # Nothing above is visible to a reader: the header still points at
            # the old directory.  This four-byte write is the commit.
            handle.seek(4)
            handle.write(_long(byteorder, directory_start))
            handle.flush()
    except OSError as exc:
        raise ExportError(f"could not extend {path}: {exc}") from exc

    return len(buffered)


__all__ = ["StackLayout", "StackNotExtendable", "read_layout", "append_planes",
           "MAX_APPEND_BYTES"]
