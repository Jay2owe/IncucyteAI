"""Channel identity: the Incucyte's three acquisition channels.

The device numbers its channels 1/2/3 ("ImageType").  Channel 1 is the
transmitted-light Phase image; channels 2 and 3 are the two fluorescence
"Colors", which the instrument reports as Green and Red respectively but which
an experiment can rename (e.g. "GFP", "mCherry").
"""

import re

from .engine import (
    CHANNEL_HELP,
    IMAGE_TYPE_LABELS,
    IMAGE_TYPE_MAP,
    IMAGE_TYPE_SHORT_LABELS,
    channel_name_from_channels,
    channel_tag,
    image_type_label,
    image_type_sort_key,
    parse_channels,
)

#: Symbolic channel numbers, matching the device's ImageType field.
PHASE = 1
COLOR1 = GREEN = 2
COLOR2 = RED = 3

#: Every channel, in acquisition order.
ALL_CHANNELS = (PHASE, COLOR1, COLOR2)


def channel_token(label):
    """Return a filename-safe token for a channel display name."""
    token = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    return token or "channel"


def format_channels(channels, labels=None):
    """Return a human-readable channel list, e.g. ``"Phase + GFP"``."""
    if channels is None:
        return "all channels"
    labels = labels or IMAGE_TYPE_LABELS
    names = [labels.get(c, image_type_label(c))
             for c in sorted(channels, key=image_type_sort_key)]
    return " + ".join(names) if names else "no channels"


def channel_spec(channels):
    """Return the CLI ``--channels`` spec string for a set of channel numbers."""
    if channels is None:
        return "all"
    names = [IMAGE_TYPE_SHORT_LABELS.get(c, f"type{c}")
             for c in sorted(channels, key=image_type_sort_key)]
    return ",".join(names) or "all"


def labels_for_vessel(vessel_channels):
    """Return ``{channel_number: display name}`` for one vessel's metadata."""
    labels = dict(IMAGE_TYPE_LABELS)
    if isinstance(vessel_channels, dict):
        labels[COLOR1] = channel_name_from_channels(vessel_channels, COLOR1)
        labels[COLOR2] = channel_name_from_channels(vessel_channels, COLOR2)
    return labels


def active_channels(vessel_channels):
    """Return the channel numbers actually switched on for a vessel."""
    if not isinstance(vessel_channels, dict):
        return set()
    active = set()
    if (vessel_channels.get("Phase") or {}).get("On"):
        active.add(PHASE)
    colors = vessel_channels.get("Colors") or {}
    if isinstance(colors, dict):
        if (colors.get("Color1") or {}).get("On"):
            active.add(COLOR1)
        if (colors.get("Color2") or {}).get("On"):
            active.add(COLOR2)
    return active


__all__ = [
    "PHASE", "COLOR1", "COLOR2", "GREEN", "RED", "ALL_CHANNELS",
    "CHANNEL_HELP", "IMAGE_TYPE_LABELS", "IMAGE_TYPE_MAP",
    "IMAGE_TYPE_SHORT_LABELS", "parse_channels", "channel_name_from_channels",
    "channel_tag", "image_type_label", "image_type_sort_key",
    "channel_token", "format_channels", "channel_spec",
    "labels_for_vessel", "active_channels",
]
