"""Watch mode: keep an output folder in step with the instrument.

The instrument scans on a schedule, so "download everything" is never finished
- it is a standing order.  A :class:`Watcher` re-plans on each poll and
downloads only what is new, then calls you back with the files it wrote.

It runs in its own daemon thread, so a pipeline can start one and get on with
analysing what has already landed::

    watcher = client.watch(options, on_result=lambda r: analyse(r.paths))
    ...
    watcher.stop(flush=True)

Pass ``on_file=`` instead of (or as well as) ``on_result=`` to be handed each
file the moment it lands rather than once per poll with the lot.  A poll that
collects 96 wells otherwise delivers nothing until all 96 have arrived, and the
step after this one is about thirty seconds of single-threaded work per field -
so starting well A1 while B1 is still downloading is most of what makes the
pipeline feel continuous rather than batched.

**Chunking.**  By default a frame is downloaded the moment it appears.  Set
``batch_frames`` and/or ``batch_after`` on the options and the watcher instead
*holds* new frames until the chunk is worth fetching - so an experiment can be
started on a Monday and collected a week later, in one go::

    client.watch(options, vessel=38, output="./run-01",
                 start_from="first", batch_after="7d")

The two conditions are an OR: whichever comes first wins, so a fast experiment
does not wait out the clock and a stalled one still delivers what it has.  The
clock runs from the *oldest waiting frame's own timestamp*, not from when the
watcher started, so restarting the watcher does not restart the wait.

Polling still costs one metadata pass per poll whether or not a chunk is due,
so a week-long chunk wants a lazy ``interval_minutes`` (an hour, say) rather
than the default ten minutes.
"""

import logging
import threading
from datetime import datetime, timedelta

from .engine import parse_scan_datetime
from .models import ProgressEvent

log = logging.getLogger("pyincucyte.watch")

#: How long ``stop(flush=True)`` waits for the poll thread before flushing, so
#: the two never download into the same folder at once.
STOP_TIMEOUT = 300


def format_age(delta):
    """Say an elapsed time roughly: ``"2d 4h"``, ``"3h 20m"``, ``"12m"``."""
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "just now"


class Watcher:
    """Polls the device on an interval and downloads whatever is new.

    With ``batch_frames`` or ``batch_after`` set on the options it downloads in
    chunks instead: see the module docstring.
    """

    def __init__(self, client, options, *, on_result=None, on_file=None,
                 on_error=None, on_poll=None, on_hold=None, progress=None,
                 name="pyincucyte-watch"):
        self.client = client
        self.options = options
        self.on_result = on_result
        #: Called with each OutputFile as it lands, rather than once per poll.
        self.on_file = on_file
        self.on_error = on_error
        self.on_poll = on_poll
        self.on_hold = on_hold
        self.progress = progress
        self.name = name

        self.stop_event = threading.Event()
        self._thread = None
        self._poll_count = 0
        self._file_count = 0
        self._last_poll = None
        self._last_error = None
        self._next_poll_at = None
        self._pending_frames = 0
        self._pending_since = None

    # -- state ------------------------------------------------------------

    @property
    def interval_seconds(self):
        return max(1, int(self.options.interval_minutes)) * 60

    @property
    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def poll_count(self):
        return self._poll_count

    @property
    def file_count(self):
        """Total files downloaded since this watcher started."""
        return self._file_count

    @property
    def last_poll(self):
        return self._last_poll

    @property
    def last_error(self):
        return self._last_error

    def seconds_until_next_poll(self):
        """Seconds remaining before the next poll, or 0 when polling now."""
        if not self._next_poll_at:
            return 0
        return max(0, int((self._next_poll_at - datetime.now()).total_seconds()))

    # -- the held chunk ---------------------------------------------------

    @property
    def batches(self):
        """True when this watcher holds frames back rather than downloading them."""
        return self.options.batches

    @property
    def batch_description(self):
        """What a held chunk is waiting for, in words. "" when not batching."""
        return self.options.batch_description

    @property
    def pending_frames(self):
        """New frames waiting on the instrument, as of the last poll."""
        return self._pending_frames

    @property
    def pending_since(self):
        """Timestamp of the oldest waiting frame, or None when none are."""
        return self._pending_since

    @property
    def pending_age(self):
        """How long the oldest waiting frame has waited, or None."""
        if self._pending_since is None:
            return None
        return datetime.now() - self._pending_since

    @property
    def is_holding(self):
        """True when frames are waiting because the chunk is not ready yet."""
        return bool(self._pending_frames) and not self._chunk_is_ready()

    @property
    def hold_description(self):
        """e.g. ``"12 frames held, waiting until 50 frames ... (2d 4h so far)"``."""
        frames = self._pending_frames
        text = f"{frames} frame{'' if frames == 1 else 's'} held"
        if not self.batches:
            return text
        text += f", waiting until {self.batch_description}"
        age = self.pending_age
        if age is not None:
            text += f" ({format_age(age)} so far)"
        return text

    def _chunk_is_ready(self):
        """Has the held chunk hit its frame count, or waited long enough?"""
        options = self.options
        if not options.batches:
            return True
        if options.batch_frames and self._pending_frames >= options.batch_frames:
            return True
        delay = options.batch_delay
        if delay is not None and self._pending_since is not None:
            return datetime.now() - self._pending_since >= delay
        return False

    # -- lifecycle --------------------------------------------------------

    def start(self):
        """Begin polling in a background daemon thread."""
        if self.is_running:
            return self
        self.stop_event.clear()
        self._thread = threading.Thread(target=self.run_forever, name=self.name,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, wait=None, flush=False):
        """Ask the watcher to stop; optionally wait for it to finish.

        ``flush=True`` downloads the held chunk on the way out - otherwise a
        part-full chunk simply stays on the instrument until the next run.  It
        waits for the poll thread first, so the two never download at once.
        """
        self.stop_event.set()
        if (wait or flush) and self._thread:
            timeout = None if wait is True else (wait or STOP_TIMEOUT)
            self._thread.join(timeout=timeout)
        if flush:
            self.flush()
        return self

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)
        return self

    def __enter__(self):
        if not self.is_running:
            self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop(wait=5)
        return False

    # -- the loop ---------------------------------------------------------

    def poll_once(self):
        """Plan, then download the chunk if it is ready.

        Returns a DownloadResult, or None when frames are being held back for a
        later chunk.
        """
        self._poll_count += 1
        self._last_poll = datetime.now()
        self._emit_poll()
        plan = self.client.plan(self.options, progress=self.progress,
                                cancel=self.stop_event)
        return self._collect(plan)

    def flush(self, final=None):
        """Download whatever is being held right now, chunk ready or not.

        This is how the tail of an experiment is collected: a part-full chunk
        that will never reach its frame count because the instrument has
        stopped scanning.

        A flush after :meth:`stop` is the end of the run, so what it writes is
        marked ``complete`` in the manifest - the difference between a stack
        still filling and one a downstream step can safely outline.  Pass
        ``final`` to say so explicitly either way.
        """
        # A flush is usually the last thing that happens after stop(), so it
        # cannot share the already-set stop event or it would cancel itself.
        stopped = self.stop_event.is_set()
        cancel = threading.Event() if stopped else self.stop_event
        if final is None:
            final = stopped
        plan = self.client.plan(self.options, progress=self.progress,
                                cancel=cancel)
        return self._collect(plan, force=True, cancel=cancel,
                             complete=bool(final))

    def _collect(self, plan, force=False, cancel=None, complete=False):
        """Decide whether this plan's frames go now or wait for a bigger chunk."""
        pending = plan.new_scan_times
        self._pending_frames = len(pending)
        self._pending_since = _first_moment(pending)

        if pending and not force and not self._chunk_is_ready():
            self._emit_hold()
            return None

        # A watcher exists to keep adding frames, so what it writes mid-run is
        # explicitly unfinished; only the flush that ends the run says done.
        result = self.client.download(
            plan, progress=self.progress, on_file=self.on_file,
            complete=complete,
            cancel=self.stop_event if cancel is None else cancel)
        self._pending_frames = 0
        self._pending_since = None
        self._file_count += result.file_count
        if result.errors:
            self._last_error = result.errors[-1]
        if result.files and self.on_result:
            self._safe(self.on_result, result)
        return result

    def run_forever(self):
        """Run the poll loop until :meth:`stop` is called (blocking)."""
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("watch poll failed: %s", exc)
                if self.on_error:
                    self._safe(self.on_error, exc)
            if self.stop_event.is_set():
                break
            self._sleep_until_next_poll()
        self._next_poll_at = None

    def _sleep_until_next_poll(self):
        """Wait out the interval, ticking a countdown, but wake on stop()."""
        interval = self.interval_seconds
        self._next_poll_at = datetime.now() + timedelta(seconds=interval)
        for elapsed in range(interval):
            if self.stop_event.wait(1):
                return
            remaining = self.seconds_until_next_poll()
            if self.progress and (remaining % 5 == 0 or remaining < 5):
                minutes, seconds = divmod(remaining, 60)
                self._safe(self.progress, ProgressEvent(
                    stage="waiting",
                    detail=f"Next poll in {minutes}m {seconds:02d}s",
                    done=elapsed + 1, total=interval, unit="seconds"))

    def _emit_poll(self):
        if self.on_poll:
            self._safe(self.on_poll, self._poll_count)
        if self.progress:
            self._safe(self.progress, ProgressEvent(
                stage="polling", detail=f"Poll {self._poll_count}"))

    def _emit_hold(self):
        detail = self.hold_description
        log.info("watch %s", detail)
        if self.on_hold:
            self._safe(self.on_hold, self)
        if self.progress:
            self._safe(self.progress, ProgressEvent(
                stage="holding", detail=detail, done=self._pending_frames,
                total=self.options.batch_frames or 0, unit="frames"))

    @staticmethod
    def _safe(callback, argument):
        try:
            callback(argument)
        except Exception:
            log.debug("watch callback raised", exc_info=True)

    def __repr__(self):
        state = "running" if self.is_running else "stopped"
        held = f" held={self._pending_frames}" if self._pending_frames else ""
        return (f"<Watcher {state} polls={self._poll_count} "
                f"files={self._file_count}{held} "
                f"every {self.options.interval_minutes}m>")


def _first_moment(scan_times):
    """The datetime of the earliest scan time in a sorted list, or None."""
    for scan_time in scan_times:
        try:
            return parse_scan_datetime(str(scan_time))
        except (ValueError, TypeError):
            continue
    return None


__all__ = ["Watcher", "format_age", "STOP_TIMEOUT"]
