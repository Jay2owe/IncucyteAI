"""Optional preprocessing, matching what the Incucyte software offers.

The instrument's own Image Export wizard has two paths.  *As Displayed* bakes
in whatever is on screen and hands back an 8-bit picture; *As Stored* returns
the raw pixels and, in Sartorius's words, "user-specified settings are not
reflected in this type of export".  The REST payload PyIncucyte downloads is
the *As Stored* one, so nothing the device sends is ever preprocessed.

What the device does send - in the ``GetScanVessel`` response the planner
already reads - are the coefficients:

* ``ColorUnmixes``: ``{Recipient, Contributor, ValueRatio, BlurringSigma}`` for
  each ordered channel pair.  That is linear (spectral) unmixing: subtract a
  fixed fraction of one channel from another.
* per image, ``Scale`` and ``Bias``: raw camera counts to calibrated units, the
  difference between the wizard's "Green uncalibrated - Raw 16-bit image" and
  "Green calibrated - 32-bit floating point image in calibrated units".
* per image, ``ImageMedian``: the background level the device measured.

So the arithmetic happens here, with the instrument's own numbers.  Order is
fixed and deliberate::

    calibrate  ->  subtract background  ->  unmix  ->  clip at zero

Background before unmixing because the model is
``seen = true + ratio x other + offset``: each channel's own offset has to go
before the cross-talk term means anything.  Sartorius does not publish the
order it uses internally, so a preprocessed file is not guaranteed to be
bit-identical to one exported from the Incucyte software - which is why every
step is off by default and why the manifest records exactly what was applied.
"""

import logging
import re
from dataclasses import dataclass

from . import channels as ch

log = logging.getLogger("pyincucyte.processing")

#: ``ColorUnmixes`` numbers the two fluorescence channels 1 and 2; the device's
#: ImageType numbers them 2 and 3.
COLOR_INDEX_OFFSET = 1

#: ``green:8%red`` - take 8% of red out of green.  ``@2`` blurs the contributor
#: by that many pixels first, as the device's BlurringSigma does.  ``<`` reads
#: better but a shell eats it, so the colon is the spelling the CLI advertises.
_UNMIX_TERM = re.compile(
    r"^(?P<recipient>[a-z0-9_]+)\s*[:<]\s*(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<percent>%?)\s*\*?\s*(?P<contributor>[a-z0-9_]+)"
    r"(?:\s*@\s*(?P<sigma>\d+(?:\.\d+)?))?$", re.IGNORECASE)

UNMIX_HELP = ("'device' to use the vessel's own saved values, or terms like "
              "'green:8%red' - 8% of red taken out of green - optionally with "
              "'@2' to blur the contributor by 2 pixels first")

BACKGROUND_HELP = ("'device' for the level the instrument measured, or a "
                   "number of raw camera counts")

#: Values meaning "do not do this".
OFF = ("", "off", "none", "no", "false", "0")


def _is_off(value):
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in OFF
    return False


def _channel_number(name):
    """Turn ``green``/``color1``/``2`` into a device ImageType number."""
    text = str(name).strip().lower()
    if text.isdigit():
        number = int(text)
    else:
        number = ch.IMAGE_TYPE_MAP.get(text)
    if number not in (ch.COLOR1, ch.COLOR2):
        raise ValueError(
            f"{name!r} is not a fluorescence channel. Unmixing works between "
            f"green/color1 and red/color2 - Phase has no calibration and "
            f"nothing bleeds into it.")
    return number


# ---------------------------------------------------------------------------
# what the user asked for
# ---------------------------------------------------------------------------

@dataclass
class Recipe:
    """The preprocessing a download was asked to apply. Off by default."""

    calibrate: bool = False
    background: str = ""
    unmix: str = ""

    @classmethod
    def from_options(cls, options):
        """Build a recipe from an :class:`~pyincucyte.options.ExportOptions`."""
        if options is None:
            return cls()
        return cls(calibrate=bool(getattr(options, "calibrate", False)),
                   background=getattr(options, "background", "") or "",
                   unmix=getattr(options, "unmix", "") or "")

    # -- shape ------------------------------------------------------------

    @property
    def wants_calibration(self):
        return bool(self.calibrate)

    @property
    def wants_background(self):
        return not _is_off(self.background)

    @property
    def wants_unmixing(self):
        return not _is_off(self.unmix)

    @property
    def is_active(self):
        return (self.wants_calibration or self.wants_background
                or self.wants_unmixing)

    @property
    def uses_device_unmixing(self):
        return (self.wants_unmixing
                and str(self.unmix).strip().lower() == "device")

    def validate(self):
        """Return human-readable problems with this recipe."""
        problems = []
        if self.wants_unmixing and not self.uses_device_unmixing:
            try:
                parse_unmix(self.unmix)
            except ValueError as exc:
                problems.append(str(exc))
        if self.wants_background:
            text = str(self.background).strip().lower()
            if text != "device":
                try:
                    float(text)
                except ValueError:
                    problems.append(
                        f"background must be {BACKGROUND_HELP}; got "
                        f"{self.background!r}")
        return problems

    def describe(self):
        """One line naming what will be done to the pixels."""
        if not self.is_active:
            return "raw pixels, exactly as stored on the device"
        parts = []
        if self.wants_calibration:
            parts.append("calibrated units (32-bit float)")
        if self.wants_background:
            parts.append("device background removed"
                         if str(self.background).strip().lower() == "device"
                         else f"background {self.background} removed")
        if self.wants_unmixing:
            parts.append("unmixed with the vessel's saved values"
                         if self.uses_device_unmixing
                         else f"unmixed ({self.unmix})")
        return ", ".join(parts)

    def to_dict(self):
        return {"calibrate": self.calibrate, "background": self.background,
                "unmix": self.unmix, "description": self.describe(),
                "order": "calibrate, background, unmix, clip at zero"}


def parse_unmix(spec):
    """Parse ``"green<8%red,red<2%green"`` into unmix pairs.

    Returns a list of ``{"recipient", "contributor", "ratio", "sigma"}``.
    ``8%`` and ``0.08`` mean the same thing.
    """
    if _is_off(spec):
        return []
    text = str(spec).strip()
    if text.lower() == "device":
        raise ValueError(
            "'device' unmixing is resolved from the vessel, not parsed here.")
    pairs = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = _UNMIX_TERM.match(part)
        if not match:
            raise ValueError(
                f"Cannot read unmix term {part!r}. Use {UNMIX_HELP}.")
        amount = float(match.group("amount"))
        if match.group("percent"):
            amount /= 100.0
        if not 0 <= amount <= 1:
            raise ValueError(
                f"Unmix ratio in {part!r} is {amount:.3f}; it must be between "
                f"0 and 1 (0% and 100%).")
        recipient = _channel_number(match.group("recipient"))
        contributor = _channel_number(match.group("contributor"))
        if recipient == contributor:
            raise ValueError(
                f"{part!r} unmixes a channel from itself; the contributor must "
                f"be the other colour.")
        pairs.append({"recipient": recipient, "contributor": contributor,
                      "ratio": amount,
                      "sigma": float(match.group("sigma") or 0.0)})
    return pairs


# ---------------------------------------------------------------------------
# what the device says
# ---------------------------------------------------------------------------

def unmix_pairs_from_scan(scan):
    """Read the vessel's saved unmixing out of a ``GetScanVessel`` payload.

    Pairs whose ratio is zero are dropped - that is the device's way of saying
    nobody has configured unmixing for this vessel.
    """
    pairs = []
    entries = scan.get("ColorUnmixes") if isinstance(scan, dict) else None
    if isinstance(entries, dict):          # not yet unpacked from $values
        entries = entries.get("$values")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ratio = float(entry.get("ValueRatio") or 0.0)
            if ratio <= 0:
                continue
            pairs.append({
                "recipient": int(entry.get("Recipient", 1)) + COLOR_INDEX_OFFSET,
                "contributor": int(entry.get("Contributor", 2)) + COLOR_INDEX_OFFSET,
                "ratio": ratio,
                "sigma": float(entry.get("BlurringSigma") or 0.0),
            })
    if pairs or not isinstance(scan, dict):
        return pairs

    # Older software sends only a scalar per colour. The pairing is not stated
    # anywhere, so take the obvious reading and say so.
    for key, recipient, contributor in (("Color1Unmix", ch.COLOR1, ch.COLOR2),
                                        ("Color2Unmix", ch.COLOR2, ch.COLOR1)):
        ratio = float(scan.get(key) or 0.0)
        if ratio > 0:
            log.warning("using legacy %s=%.4f as %s unmixed from %s", key,
                        ratio, ch.image_type_label(recipient),
                        ch.image_type_label(contributor))
            pairs.append({"recipient": recipient, "contributor": contributor,
                          "ratio": ratio, "sigma": 0.0})
    return pairs


def coefficients_from_image(image_info):
    """Read ``Scale``/``Bias``/``ImageMedian`` off one ``ImageInfo`` entry.

    Phase carries none of them, and gets ``None`` - it is an 8-bit
    transmitted-light image with no calibration to apply.
    """
    if not isinstance(image_info, dict):
        return None
    scale = image_info.get("Scale")
    if scale in (None, 0):
        return None
    return {"scale": float(scale),
            "bias": float(image_info.get("Bias") or 0.0),
            "median": float(image_info.get("ImageMedian") or 0.0)}


def plan_for_image(recipe, img_type, coefficients, pairs):
    """Resolve one image's processing, or ``None`` if there is nothing to do.

    ``coefficients`` maps channel number to the dict from
    :func:`coefficients_from_image`; ``pairs`` is the resolved unmix list.
    Everything the download path needs ends up in the returned dict, so it can
    be carried on a work item and applied without going back to the device.
    """
    if recipe is None or not recipe.is_active:
        return None
    mine = (coefficients or {}).get(img_type)
    if mine is None:
        return None                 # Phase, or a channel with no calibration

    calibrate = bool(recipe.wants_calibration)
    background = _background_for(recipe, mine)

    terms = []
    if recipe.wants_unmixing:
        for pair in pairs or []:
            if pair["recipient"] != img_type:
                continue
            theirs = (coefficients or {}).get(pair["contributor"])
            if theirs is None:
                log.debug("no calibration for contributor %s; skipping unmix",
                          pair["contributor"])
                continue
            terms.append({
                "contributor": pair["contributor"],
                "ratio": float(pair["ratio"]),
                "sigma": float(pair.get("sigma") or 0.0),
                "scale": theirs["scale"], "bias": theirs["bias"],
                "background": _background_for(recipe, theirs),
            })

    if not (calibrate or background or terms):
        return None
    return {"calibrate": calibrate,
            "scale": mine["scale"], "bias": mine["bias"],
            "background": background, "unmix": terms}


def _background_for(recipe, coefficients):
    """The background level to remove from one channel, in raw counts."""
    if not recipe.wants_background:
        return 0.0
    text = str(recipe.background).strip().lower()
    if text == "device":
        return float(coefficients.get("median") or 0.0)
    try:
        return float(text)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def decode(tif_bytes):
    """Decode payload bytes to a 2-D array.

    Deliberately a copy of the engine's decode rather than an import of it:
    this module is imported *by* the engine, and a cycle helps nobody.
    """
    import io

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(tif_bytes)) as image:
        array = np.array(image)
    if array.ndim == 3:
        array = array[..., 0]
    return array


def _calibrated(array, scale, bias):
    """Raw camera counts to calibrated units (GCU/RCU)."""
    import numpy as np

    return (np.asarray(array, dtype="float32") - float(bias)) / float(scale)


def _blur(array, sigma):
    """Gaussian blur of a float image, separably, in plain NumPy.

    Pillow's GaussianBlur refuses 32-bit float images and SciPy is not a
    dependency, so this does the two 1-D passes itself: a handful of shifted
    adds per axis, edges held rather than darkened.
    """
    import numpy as np

    sigma = float(sigma)
    if sigma <= 0:
        return array
    data = np.asarray(array, dtype="float32")
    radius = max(1, int(round(3.0 * sigma)))
    offsets = np.arange(-radius, radius + 1, dtype="float32")
    kernel = np.exp(-(offsets ** 2) / (2.0 * sigma * sigma))
    kernel /= kernel.sum()

    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius, radius)
        padded = np.pad(data, pad, mode="edge")
        blurred = np.zeros_like(data)
        for index, weight in enumerate(kernel):
            window = [slice(None), slice(None)]
            window[axis] = slice(index, index + data.shape[axis])
            blurred += weight * padded[tuple(window)]
        data = blurred
    return data


def apply(array, plan, fetch_contributor=None):
    """Apply one image's processing plan and return the new array.

    ``fetch_contributor(img_type)`` supplies the other channel's raw array, and
    is only called when a plan actually has an unmix term - unmixing is the one
    step that cannot be done with the image alone.
    """
    import numpy as np

    if not plan:
        return array

    scale, bias = plan["scale"], plan["bias"]
    calibrate = bool(plan.get("calibrate"))
    working = (_calibrated(array, scale, bias) if calibrate
               else np.asarray(array, dtype="float32"))

    background = float(plan.get("background") or 0.0)
    if background:
        working = working - (_calibrated(background, scale, bias)
                             if calibrate else background)

    for term in plan.get("unmix") or ():
        if fetch_contributor is None:
            raise ValueError(
                "Unmixing needs the other channel, and no way to fetch it was "
                "given.")
        other = fetch_contributor(term["contributor"])
        if other is None:
            raise ValueError(
                f"Could not read channel {term['contributor']} to unmix it out.")
        other = np.asarray(other)
        if other.shape != np.asarray(working).shape:
            raise ValueError(
                f"Contributor channel is {other.shape}, recipient is "
                f"{np.asarray(working).shape} - they must match to unmix.")
        contributor = (_calibrated(other, term["scale"], term["bias"])
                       if calibrate else other.astype("float32"))
        their_background = float(term.get("background") or 0.0)
        if their_background:
            contributor = contributor - (
                _calibrated(their_background, term["scale"], term["bias"])
                if calibrate else their_background)
        working = working - term["ratio"] * _blur(contributor, term["sigma"])

    # A negative pixel means a coefficient was too big; it is never signal.
    working = np.clip(working, 0, None)

    if calibrate:
        return working.astype("float32", copy=False)
    original = np.asarray(array).dtype
    if np.issubdtype(original, np.integer):
        ceiling = np.iinfo(original).max
        return np.clip(working, 0, ceiling).astype(original, copy=False)
    return working.astype(original, copy=False)


__all__ = [
    "Recipe", "UNMIX_HELP", "BACKGROUND_HELP", "COLOR_INDEX_OFFSET",
    "parse_unmix", "unmix_pairs_from_scan", "coefficients_from_image",
    "plan_for_image", "apply", "decode",
]
