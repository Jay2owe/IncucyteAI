"""PyIncucyte - download Incucyte live-cell images from Python.

Two ways in.  A desktop app::

    pyincucyte gui                  # or: pyincucyte-gui, python -m pyincucyte.gui

...and an importable API for an automated pipeline::

    from pyincucyte import IncucyteClient

    with IncucyteClient.from_saved() as incucyte:
        result = incucyte.fetch(
            vessel=38, output="./run-01", wells="A1-D6",
            channels="phase,green", layout="time_channel_stack",
            start_from="first")

    for image in result.files:
        segment(image.path, well=image.well, channels=image.channel_names)

``result.files`` carries the well, channel names, timepoints and axis order for
every file, and the same information is written to ``pyincucyte-manifest.json``
next to the images so a later stage can pick up without re-parsing filenames.
"""

import logging

__version__ = "0.3.1"

# A library should not configure logging for its host application; this only
# stops "No handlers could be found" warnings when nobody has set one up.
logging.getLogger("pyincucyte").addHandler(logging.NullHandler())

from .errors import (  # noqa: E402
    ApiError,
    AuthenticationError,
    ConfirmationRequiredError,
    DeviceBusyError,
    DeviceUnreachableError,
    HostNotSetError,
    EncryptionUnavailableError,
    ExportCancelled,
    ExportError,
    StackNotExtendable,
    IncucyteError,
    NotLoggedInError,
    TokenExpiredError,
    VesselNotFoundError,
)
from .models import (  # noqa: E402
    DownloadResult,
    ExportPlan,
    LAYOUTS,
    LAYOUT_AXES,
    LAYOUT_DESCRIPTIONS,
    LAYOUT_LABELS,
    OutputFile,
    ProgressEvent,
    Vessel,
    VesselScan,
    human_bytes,
    resolve_layout,
)
from .device import DeviceState, ScanPattern  # noqa: E402
from .options import ExportOptions  # noqa: E402
from .preview import PreviewImage, PreviewSet  # noqa: E402
from .protocol import Protocol, Step, read_protocol  # noqa: E402
from .timeline import (  # noqa: E402
    HybridTimelineSource,
    TimelinePreview,
    TimelineSource,
    choose_frames,
)
from .processing import Recipe, Unmixing  # noqa: E402
from .config import ConfigStore, Credentials  # noqa: E402
from .state import StateStore  # noqa: E402
from .client import IncucyteClient  # noqa: E402
from .watch import Watcher  # noqa: E402
from .manifest import load_manifest, write_manifest  # noqa: E402
from .engine import DEFAULT_HOST, APP_DIR  # noqa: E402

# Import names retired in 0.3 - see pyincucyte.compat.
from . import compat  # noqa: E402

compat.install()


def connect(host=None, username=None, password=None, *, store=None, save=True,
            device_name=""):
    """Return a ready client - saved login by default, or fresh credentials."""
    if username and password:
        return IncucyteClient.connect(
            host or DEFAULT_HOST, username, password, store=store, save=save,
            device_name=device_name)
    return IncucyteClient.from_saved(host, store=store)


def pull(target=None, *, host=None, username=None, password=None, store=None,
         options=None, out=None, **kwargs):
    """Pull one vessel through a short-lived saved or fresh connection."""
    with connect(host, username, password, store=store) as client:
        return client.pull(target, options, out=out, **kwargs)


def watch_once(target=None, *, host=None, username=None, password=None,
               store=None, options=None, out=None, flush=False, **kwargs):
    """Run one resumable watcher poll without leaving a process resident."""
    with connect(host, username, password, store=store) as client:
        return client.watch_once(
            target, options, out=out, flush=flush, **kwargs)


__all__ = [
    "__version__",
    "connect",
    "pull",
    "watch_once",
    "IncucyteClient",
    "ExportOptions",
    "ExportPlan",
    "DownloadResult",
    "OutputFile",
    "ProgressEvent",
    "Vessel",
    "VesselScan",
    "DeviceState",
    "ScanPattern",
    "PreviewImage",
    "PreviewSet",
    "Protocol",
    "Step",
    "read_protocol",
    "TimelineSource",
    "HybridTimelineSource",
    "TimelinePreview",
    "choose_frames",
    "Recipe",
    "Unmixing",
    "Watcher",
    "StateStore",
    "ConfigStore",
    "Credentials",
    "LAYOUTS",
    "LAYOUT_AXES",
    "LAYOUT_LABELS",
    "LAYOUT_DESCRIPTIONS",
    "resolve_layout",
    "human_bytes",
    "load_manifest",
    "write_manifest",
    "DEFAULT_HOST",
    "APP_DIR",
    "IncucyteError",
    "ApiError",
    "AuthenticationError",
    "NotLoggedInError",
    "TokenExpiredError",
    "DeviceUnreachableError",
    "HostNotSetError",
    "EncryptionUnavailableError",
    "VesselNotFoundError",
    "ExportError",
    "ExportCancelled",
    "StackNotExtendable",
    "ConfirmationRequiredError",
    "DeviceBusyError",
]
