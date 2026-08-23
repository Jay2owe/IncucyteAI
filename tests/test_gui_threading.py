"""The GUI must never touch Tk from a worker thread."""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pyincucyte.gui import app as app_module
from pyincucyte.gui.app import App


class GuiThreadingTests(unittest.TestCase):
    def test_post_defers_worker_callbacks_to_the_tk_thread(self):
        app = object.__new__(App)          # no window needed for this
        app.ui_queue = queue.Queue()
        calls = []

        worker = threading.Thread(target=app._post, args=(calls.append, "from worker"))
        worker.start()
        worker.join()

        self.assertEqual(calls, [], "the worker must not have run the callback")
        callback, args, kwargs = app.ui_queue.get_nowait()
        callback(*args, **kwargs)
        self.assertEqual(calls, ["from worker"])


class FakeWatcher:
    """Just enough Watcher for the stop path: it is holding a chunk."""

    def __init__(self, pending=3):
        self.is_running = False
        self.pending_frames = pending
        self.hold_description = f"{pending} frames held"
        self.flushed = False

    def flush(self):
        self.flushed = True
        return None


class HeldChunkOnStopTests(unittest.TestCase):
    """Stopping mid-chunk must ask, not silently abandon a week of frames."""

    def app_with(self, watcher):
        app = object.__new__(App)          # no window needed for this
        app.ui_queue = queue.Queue()
        app.said = []
        app.finished = []
        app.say = lambda message, level="info": app.said.append(message)
        app._finish_work = lambda: app.finished.append(True)
        app.watcher = watcher
        app.root = None                    # only ever passed as a dialog parent
        return app

    def run_pending(self, app):
        """Drain what the worker thread posted back to the Tk thread."""
        while not app.ui_queue.empty():
            callback, args, kwargs = app.ui_queue.get_nowait()
            callback(*args, **kwargs)

    def test_saying_yes_collects_the_held_frames(self):
        watcher = FakeWatcher()
        app = self.app_with(watcher)
        with patch.object(app_module.messagebox, "askyesno", return_value=True):
            App._finish_watch(app)
        app.worker.join(timeout=5)
        self.run_pending(app)
        self.assertTrue(watcher.flushed)
        self.assertIsNone(app.watcher)
        self.assertTrue(app.finished)

    def test_saying_no_leaves_them_on_the_instrument(self):
        watcher = FakeWatcher()
        app = self.app_with(watcher)
        with patch.object(app_module.messagebox, "askyesno", return_value=False):
            App._finish_watch(app)
        self.assertFalse(watcher.flushed)
        self.assertIn("3 frames held", " ".join(app.said))
        self.assertTrue(app.finished)

    def test_nothing_held_means_nothing_to_ask(self):
        watcher = FakeWatcher(pending=0)
        app = self.app_with(watcher)
        with patch.object(app_module.messagebox, "askyesno",
                          side_effect=AssertionError("should not ask")):
            App._finish_watch(app)
        self.assertFalse(watcher.flushed)
        self.assertTrue(app.finished)


class StateMigrationTests(unittest.TestCase):
    def test_a_pre_0_2_settings_file_keeps_its_settings(self):
        migrated = App._migrate_state({
            "host": "10.0.0.1",
            "output": "X:/out",
            "interval": 20,
            "phase": True, "color1": True, "color2": False,
            "max_workers": 8,
            "green_phase": True,
            "hyperstack": True, "time_stack": True,
            "start_from": "First scan",
            "selected_vessels": [38],
            "wells": {"38": [[0, 0], [0, 1]]},
        })
        options = migrated["options"]
        self.assertEqual(migrated["host"], "10.0.0.1")
        self.assertEqual(options["output"], "X:/out")
        self.assertEqual(options["channels"], "phase,green")
        self.assertTrue(options["hyperstack"] and options["time_stack"])
        self.assertEqual(options["workers"], 8)
        self.assertEqual(options["interval_minutes"], 20)
        self.assertEqual(options["start_from"], "first")
        self.assertEqual(options["vessels"], [38])
        self.assertEqual(migrated["wells"], {"38": [[0, 0], [0, 1]]})
        self.assertEqual(migrated["recent_outputs"], ["X:/out"])

    def test_a_custom_date_survives_the_migration(self):
        migrated = App._migrate_state({
            "start_from": "Custom date...", "custom_date": "2026-04-02"})
        self.assertEqual(migrated["options"]["start_from"], "2026-04-02")

    def test_a_current_settings_file_is_left_alone(self):
        current = {"options": {"output": "keep"}, "dark": True}
        self.assertIs(App._migrate_state(current), current)


class RecordingClient:
    """Records the writes the app asks for, and how they were confirmed."""

    def __init__(self):
        self.scans = []
        self.unmixes = []

    def begin_scan(self, vessel_id, **kwargs):
        self.scans.append((vessel_id, kwargs))
        return SimpleNamespace(summary=lambda: "Idle")

    def save_unmix(self, vessel_id, mixing, **kwargs):
        self.unmixes.append((vessel_id, str(mixing), kwargs))


class DeviceWriteTests(unittest.TestCase):
    """Nothing reaches the shared instrument without a dialog first."""

    def app_with(self, selected, options_unmix="green:8%red"):
        app = object.__new__(App)
        app.root = None                    # only ever passed as a dialog parent
        app.vessels = []
        app.client = RecordingClient()
        app.started = []
        app._selected_vessel_ids = lambda: list(selected)
        app._run_worker = lambda target, *args: app.started.append((target, args))
        app._current_options = lambda: SimpleNamespace(unmix=options_unmix)
        return app

    def test_scanning_needs_exactly_one_vessel(self):
        app = self.app_with([38, 39])
        with patch.object(app_module.messagebox, "showinfo") as told:
            app._scan_now()
        self.assertTrue(told.called)
        self.assertEqual(app.started, [], "nothing should have been started")

    def test_declining_the_dialog_starts_no_work(self):
        app = self.app_with([38])
        with patch.object(app_module.messagebox, "askyesno", return_value=False):
            app._scan_now()
        self.assertEqual(app.started, [])

    def test_accepting_the_dialog_hands_the_vessel_to_a_worker(self):
        app = self.app_with([38])
        with patch.object(app_module.messagebox, "askyesno", return_value=True):
            app._scan_now()
        self.assertEqual(len(app.started), 1)
        target, args = app.started[0]
        self.assertEqual(target, app._scan_now_worker)
        self.assertEqual(args, (38,))

    def test_the_worker_confirms_the_write(self):
        app = self.app_with([38])
        app.status = SimpleNamespace(set_message=lambda *_: None)
        app._post = lambda callback, *args, **kwargs: None
        app.say = lambda *args, **kwargs: None
        app._scan_now_worker(38)
        vessel_id, kwargs = app.client.scans[0]
        self.assertEqual(vessel_id, 38)
        self.assertTrue(kwargs["confirm"])

    def test_saving_unmixing_asks_first_and_passes_the_current_recipe(self):
        app = self.app_with([38])
        with patch.object(app_module.messagebox, "askyesno", return_value=True):
            app._save_unmix()
        target, args = app.started[0]
        self.assertEqual(target, app._save_unmix_worker)
        self.assertEqual(str(args[1]), "green:8%red")

    def test_a_bad_unmix_spec_is_reported_not_raised(self):
        app = self.app_with([38], options_unmix="not a spec")

        def explode():
            raise ValueError("that is not an unmixing")

        app._current_options = explode
        with patch.object(app_module.messagebox, "showerror") as told:
            app._save_unmix()
        self.assertTrue(told.called)
        self.assertEqual(app.started, [])


if __name__ == "__main__":
    unittest.main()
