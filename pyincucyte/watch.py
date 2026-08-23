"""Watch mode: keep an output folder in step with the instrument.

The instrument scans on a schedule, so "download everything" is never finished
- it is a standing order.  A :class:`Watcher` re-plans on each poll and
downloads only what is new, then calls you back with the files it wrote.

It runs in its own daemon thread, so a pipeline can start one and get on with
analysing what has already landed:

    watcher = client.watch(options, on_result=lambda r: analyse(r.paths))
    ...
    watcher.stop()
"""

import logging
import threading
from datetime import datetime, timedelta

from .models import ProgressEvent

log = logging.getLogger("pyincucyte.watch")


class Watcher:
    """Polls the device on an interval and downloads whatever is new."""

    def __init__(self, client, options, *, on_result=None, on_error=None,
                 on_poll=None, progress=None, name="pyincucyte-watch"):
        self.client = client
        self.options = options
        self.on_result = on_result
        self.on_error = on_error
        self.on_poll = on_poll
        self.progress = progress
        self.name = name

        self.stop_event = threading.Event()
        self._thread = None
        self._poll_count = 0
        self._file_count = 0
        self._last_poll = None
        self._last_error = None
        self._next_poll_at = None

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

    def stop(self, wait=None):
        """Ask the watcher to stop; optionally wait for it to finish."""
        self.stop_event.set()
        if wait and self._thread:
            self._thread.join(timeout=wait if wait is not True else None)
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
        """Do one plan-and-download pass. Returns a DownloadResult or None."""
        self._poll_count += 1
        self._last_poll = datetime.now()
        self._emit_poll()
        result = self.client.fetch(self.options, progress=self.progress,
                                   cancel=self.stop_event)
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

    @staticmethod
    def _safe(callback, argument):
        try:
            callback(argument)
        except Exception:
            log.debug("watch callback raised", exc_info=True)

    def __repr__(self):
        state = "running" if self.is_running else "stopped"
        return (f"<Watcher {state} polls={self._poll_count} "
                f"files={self._file_count} every {self.options.interval_minutes}m>")


__all__ = ["Watcher"]
