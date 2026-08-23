"""The GUI must never touch Tk from a worker thread."""

import queue
import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
