"""The GUI must never touch Tk from a worker thread."""

import queue
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyincucyte.gui import app as app_module
from pyincucyte.gui.app import EXPORT_PROMPTS, App
from pyincucyte.gui.dialogs import ModalDialog, PlanDialog
from pyincucyte.gui.preview import (
    PreviewWindow, TimelineWindow, preferred_preview_channel,
    preview_channel_options, preview_plane_images, preview_plate_shape,
    preview_site_options,
)
from pyincucyte.gui.widgets import WellPlate


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

    def test_timeline_worker_only_puts_results_on_its_queue(self):
        window = object.__new__(TimelineWindow)
        window.timeline = SimpleNamespace(size=64)
        window._cancel = threading.Event()
        window._results = queue.Queue()
        window.source = SimpleNamespace(
            render_frame=lambda *args, **kwargs: "pixels",
            frame_info=lambda *args: "info",
            neighbours=lambda index: (),
            prefetch=lambda *args, **kwargs: None,
        )
        worker = threading.Thread(
            target=window._load_frame,
            args=(4, 7, ("A1", 0, 1, "auto")))
        worker.start()
        worker.join()
        self.assertEqual(window._results.get_nowait(),
                         (4, 7, ("A1", 0, 1, "auto"),
                          "pixels", "info", ""))

    def test_late_timeline_result_is_ignored_after_selection_changes(self):
        window = object.__new__(TimelineWindow)
        window._closing = False
        window._generation = 2
        window._results = queue.Queue()
        window._results.put((1, 4, ("A1", 0, 1, "auto"),
                             "stale pixels", None, ""))
        window._selection = lambda: ("A2", 0, 1, "auto")
        window.after = lambda *_args: "next poll"
        TimelineWindow._poll_results(window)
        self.assertEqual(window._poll_job, "next poll")

    def test_parent_window_destruction_cancels_and_closes_the_timeline(self):
        window = object.__new__(TimelineWindow)
        window._closing = False
        window._cleanup_started = False
        window._cancel = threading.Event()
        shutdowns = []
        closes = []
        window._workers = SimpleNamespace(
            shutdown=lambda **kwargs: shutdowns.append(kwargs))
        window.timeline = SimpleNamespace(close=lambda: closes.append(True))

        started = []

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                started.append(True)
                self.target()

        with patch("pyincucyte.gui.preview.threading.Thread", ImmediateThread):
            TimelineWindow._on_destroy(
                window, SimpleNamespace(widget=window))

        self.assertTrue(window._cancel.is_set())
        self.assertEqual(started, [True])
        self.assertEqual(shutdowns, [{"wait": True, "cancel_futures": True}])
        self.assertEqual(closes, [True])


class PreviewPlateViewerTests(unittest.TestCase):
    """Image preview is one selectable plane laid out like the real plate."""

    def setUp(self):
        vessel = SimpleNamespace(
            rows=4, cols=6,
            channel_labels={1: "Phase", 2: "GFP", 3: "mCherry"})
        self.scan = SimpleNamespace(
            vessel=vessel, channels={1, 2, 3}, sites={0, 1, 2}, client=None,
            label="Vessel 38 - example - 2026-09-03 17:28")
        self.images = [
            SimpleNamespace(row=0, col=0, well="A1", img_type=1, site=0),
            SimpleNamespace(row=0, col=0, well="A1", img_type=2, site=0),
            SimpleNamespace(row=1, col=2, well="B3", img_type=1, site=0),
            SimpleNamespace(row=1, col=2, well="B3", img_type=2, site=0),
            SimpleNamespace(row=0, col=0, well="A1", img_type=1, site=1),
        ]
        self.preview = SimpleNamespace(images=self.images, scans=[self.scan])

    def test_one_channel_keeps_wells_in_physical_plate_positions(self):
        visible = preview_plane_images(self.images, channel=1, site=0)

        self.assertEqual(preview_plate_shape(self.preview), (4, 6))
        self.assertEqual([(image.row, image.col) for image in visible],
                         [(0, 0), (1, 2)])
        self.assertTrue(all(image.img_type == 1 for image in visible))

    def test_channel_and_z_stack_controls_cover_scan_metadata(self):
        self.assertEqual(preview_channel_options(self.preview),
                         [(1, "Phase"), (2, "Green (GFP)"),
                          (3, "Red (mCherry)")])
        self.assertEqual(preview_site_options(self.preview), [0, 1, 2])
        self.assertEqual(preferred_preview_channel([1, 2, 3]), 1)
        self.assertEqual(preferred_preview_channel([2, 3]), 2)
        self.assertEqual(preferred_preview_channel([3], [1, 2]), 1)

    def test_lazy_plane_load_requests_one_channel_and_z_position(self):
        result = SimpleNamespace(images=[])
        client = Mock(preview=Mock(return_value=result))
        self.scan.client = client
        window = object.__new__(PreviewWindow)
        window.preview = SimpleNamespace(
            scans=[self.scan], recipe=None, size=80, contrast="auto")
        window._selected_wells = [(0, 0), (1, 2)]
        window._results = queue.Queue()
        cancel = threading.Event()

        PreviewWindow._load_plane_worker(window, 7, (3, 2), cancel)

        client.preview.assert_called_once_with(
            self.scan, wells={(0, 0), (1, 2)}, channels=[3], site=2,
            size=80, contrast="auto", max_images=2, cancel=cancel,
            calibrate=False, background="", unmix="")
        self.assertEqual(window._results.get_nowait(),
                         (7, (3, 2), result, ""))

    def test_main_preview_initially_fetches_one_channel_for_every_well(self):
        result = SimpleNamespace(
            images=[], skipped=0, errors=[], summary=lambda: "24 wells")
        client = Mock()
        client.find_scans.return_value = [self.scan]
        client.preview.return_value = result
        app = object.__new__(App)
        app.client = client
        app.cancel_event = threading.Event()
        app.say = Mock()
        app._progress = Mock()
        app._post = Mock()

        App._view_images_worker(
            app, 38, {(row, col) for row in range(4) for col in range(6)},
            [1, 2, 3], {})

        self.assertEqual(client.preview.call_args.kwargs["channels"], [1])
        self.assertEqual(client.preview.call_args.kwargs["max_images"], 24)


class VesselAndWellsPanelTests(unittest.TestCase):
    """The plate must never hide the vessel needed to populate it."""

    def panel_app(self, selected):
        app = object.__new__(App)
        app.active_vessel = 38
        app._selected_vessel_ids = lambda: list(selected)
        app._refresh_summary = Mock()
        app.wells_card = Mock()
        app.left_column = Mock()
        app.well_count_var = Mock()
        return app

    def test_no_vessel_selection_folds_wells_and_leaves_vessels_expanded(self):
        app = self.panel_app([])

        App._on_vessel_select(app)

        self.assertIsNone(app.active_vessel)
        app.wells_card.set_body_visible.assert_called_once_with(False)
        app.left_column.rowconfigure.assert_any_call(0, weight=1)
        app.left_column.rowconfigure.assert_any_call(1, weight=0)
        app.wells_card.grid_configure.assert_called_once_with(sticky="new")
        app._refresh_summary.assert_called_once_with()

    def test_folded_wells_header_is_pinned_below_the_vessel_list(self):
        app = self.panel_app([])

        App._set_wells_expanded(app, False)

        app.wells_card.grid_configure.assert_called_once_with(sticky="new")

    def test_selecting_a_vessel_expands_wells(self):
        app = self.panel_app([38])
        vessel = SimpleNamespace(
            id=38, rows=8, cols=12, plate_format="96-well")
        app._vessel = lambda _vessel_id: vessel
        app.plate = Mock()
        app.selected_wells = {}
        app.scanned_wells = {38: set()}
        app._sync_channel_checks = Mock()

        App._on_vessel_select(app)

        self.assertEqual(app.active_vessel, 38)
        app.wells_card.set_body_visible.assert_called_once_with(True)
        app.left_column.rowconfigure.assert_any_call(0, weight=0)
        app.left_column.rowconfigure.assert_any_call(1, weight=1)
        app.wells_card.grid_configure.assert_called_once_with(sticky="nsew")
        app.plate.configure_plate.assert_called_once()


class VesselSortingTests(unittest.TestCase):
    """The most recently scanned vessels must be easiest to find."""

    def test_first_population_puts_the_latest_scan_first(self):
        app = object.__new__(App)
        App._set_initial_vessel_sort(app)
        app.vessels = [
            SimpleNamespace(
                id=1, name="No scan", owner="", type_name="96-well",
                channel_summary="", well_count=96, plate_format="96-well",
                last_scan=None),
            SimpleNamespace(
                id=2, name="Latest", owner="", type_name="96-well",
                channel_summary="", well_count=96, plate_format="96-well",
                last_scan=datetime(2026, 9, 3, 12, 0)),
            SimpleNamespace(
                id=3, name="Earlier", owner="", type_name="96-well",
                channel_summary="", well_count=96, plate_format="96-well",
                last_scan=datetime(2026, 9, 2, 12, 0)),
        ]
        app.vessel_search = SimpleNamespace(value="")
        app.vessel_tree = Mock()
        app.vessel_tree.selection.return_value = ()
        app.vessel_tree.get_children.return_value = ()
        app.vessel_hint = Mock()
        app._refresh_summary = Mock()

        App._populate_vessels(app)

        self.assertEqual((app._sort_column, app._sort_reverse),
                         ("last", True))
        self.assertEqual([vessel.id for vessel in app.filtered_vessels],
                         [2, 3, 1])

    def test_every_sortable_heading_shows_an_arrow_and_clicks_update_it(self):
        app = object.__new__(App)
        App._set_initial_vessel_sort(app)
        app._vessel_heading_labels = {
            "id": "ID", "name": "Name", "owner": "Owner",
            "plate": "Plate", "last": "Last scan", "channels": "Channels",
        }
        app.vessel_tree = Mock()
        app._populate_vessels = Mock()

        App._update_vessel_headings(app)
        labels = {
            call.args[0]: call.kwargs["text"]
            for call in app.vessel_tree.heading.call_args_list
        }
        self.assertEqual(labels["last"], "Last scan ↓")
        self.assertTrue(all(text.endswith((" ↕", " ↑", " ↓"))
                            for text in labels.values()))

        app.vessel_tree.heading.reset_mock()
        App._sort_vessels(app, "name")
        labels = {
            call.args[0]: call.kwargs["text"]
            for call in app.vessel_tree.heading.call_args_list
        }
        self.assertEqual(labels["name"], "Name ↑")
        self.assertEqual(labels["last"], "Last scan ↕")

        app.vessel_tree.heading.reset_mock()
        App._sort_vessels(app, "name")
        labels = {
            call.args[0]: call.kwargs["text"]
            for call in app.vessel_tree.heading.call_args_list
        }
        self.assertEqual(labels["name"], "Name ↓")


class WellPlateGeometryTests(unittest.TestCase):
    """Every well must remain visible when the main workspace is compact."""

    def test_plate_cells_shrink_to_fit_the_available_height(self):
        plate = object.__new__(WellPlate)
        plate.theme = SimpleNamespace(scale=1.0)
        plate.rows = 16
        plate.cols = 24
        plate.min_cell = 13
        plate.max_cell = 30

        cell, gap, _left, top = WellPlate._compute_geometry(
            plate, width=1050, height=220)

        drawn_height = top + plate.rows * (cell + gap) + gap
        self.assertLessEqual(drawn_height, 220)


class DarkModeTests(unittest.TestCase):
    """The visible control must restyle and remember the whole window."""

    class Theme:
        dark = False

        def toggle_dark(self):
            self.dark = not self.dark

        def __getitem__(self, _key):
            return "#101418"

    def test_header_choice_applies_dark_palette_and_saves_it(self):
        app = object.__new__(App)
        app.dark_mode_var = Mock(get=Mock(return_value=True))
        app.theme = self.Theme()
        app.root = Mock()
        app.plate = Mock()
        app.log = Mock()
        app.vessel_tree = Mock()
        app._save_state = Mock()

        App._apply_dark_mode(app)

        self.assertTrue(app.theme.dark)
        app.root.configure.assert_called_once_with(background="#101418")
        app.plate.apply_theme.assert_called_once_with(app.theme)
        app.log.apply_theme.assert_called_once_with(app.theme)
        app._save_state.assert_called_once_with()


class ConnectionCountdownTests(unittest.TestCase):
    """The signed-in countdown must identify the credential it measures."""

    def test_valid_connection_names_token_before_expiry_countdown(self):
        app = object.__new__(App)
        app.client = SimpleNamespace(credentials=SimpleNamespace(
            token_valid=True,
            token_seconds_left=23.4 * 3600,
            username="Jamie",
        ))
        app.conn_var = Mock()
        app.conn_pill = Mock()
        app.login_btn = Mock()

        App._update_connection(app)

        app.conn_var.set.assert_called_once_with(
            "Jamie · token expires in 23.4h")


class DeviceChooserTests(unittest.TestCase):
    """Friendly device names must still resolve to the correct address."""

    def test_named_device_label_keeps_the_address_visible(self):
        credentials = SimpleNamespace(
            device_name="Upstairs Incucyte", host="10.0.0.1")

        self.assertEqual(
            App._device_label(credentials),
            "Upstairs Incucyte — 10.0.0.1")

    def test_header_choice_resolves_back_to_its_device_address(self):
        app = object.__new__(App)
        app.host_var = Mock(get=Mock(
            return_value="Upstairs Incucyte — 10.0.0.1"))
        app._device_hosts = {
            "Upstairs Incucyte — 10.0.0.1": "10.0.0.1"}

        self.assertEqual(App._active_host(app), "10.0.0.1")


class ActionMeaningTests(unittest.TestCase):
    """Visible names must keep matching the action a person expects."""

    def test_visual_and_download_previews_have_distinct_names(self):
        app = object.__new__(App)

        actions = App._summary_action_specs(app)

        self.assertEqual(actions["Preview images"].__name__, "_view_images")
        self.assertEqual(actions["Preview download"].__name__, "_preview")
        self.assertEqual(actions["Sync"].__name__, "_start_sync")
        self.assertEqual(actions["Schedule..."].__name__,
                         "_schedule_download")


class ExportSettingsCheckpointTests(unittest.TestCase):
    """Export settings belong to each action, not the permanent workspace."""

    def test_main_workspace_contains_only_selection_and_action_panels(self):
        app = object.__new__(App)

        regions = App._main_panel_builders(app)
        panels = {
            region: [builder.__name__ for builder in builders]
            for region, builders in regions.items()
        }

        self.assertEqual(
            panels,
            {"top": ["_build_vessels"],
             "bottom_left": ["_build_wells"],
             "bottom_right": ["_build_summary"]})

    def test_every_export_action_has_an_explicit_confirmation_label(self):
        self.assertEqual(set(EXPORT_PROMPTS),
                         {"download", "expected", "sync", "schedule"})
        self.assertEqual(EXPORT_PROMPTS["download"]["confirm_text"],
                         "Review files")
        self.assertEqual(EXPORT_PROMPTS["expected"]["title"],
                         "Preview this download")
        self.assertEqual(EXPORT_PROMPTS["expected"]["mode_label"],
                         "PREVIEW DOWNLOAD")
        self.assertEqual(EXPORT_PROMPTS["expected"]["confirm_text"],
                         "Preview download")
        self.assertEqual(EXPORT_PROMPTS["sync"]["confirm_text"],
                         "Start live sync")
        self.assertEqual(EXPORT_PROMPTS["schedule"]["confirm_text"],
                         "Continue to schedule")

    def test_each_export_action_has_its_own_relevant_run_controls(self):
        run_sections = {"workers", "interval", "batching"}
        visible = {
            action: tuple(section for section in prompt["sections"]
                          if section in run_sections)
            for action, prompt in EXPORT_PROMPTS.items()
        }

        self.assertEqual(
            visible,
            {"download": ("workers",),
             "expected": (),
             "sync": ("workers", "interval", "batching"),
             "schedule": ("workers", "batching")})
        self.assertEqual(
            len({prompt["mode_label"] for prompt in EXPORT_PROMPTS.values()}),
            len(EXPORT_PROMPTS))

    def test_export_dialog_stays_on_the_parents_negative_coordinate_monitor(self):
        position = ModalDialog._centred_position(
            -1607, 128, 1137, 832, 1050, 820)

        self.assertEqual(position, (-1564, 132))

    def test_export_actions_request_their_settings_before_work(self):
        app = object.__new__(App)
        app.watcher = None
        app._request_export_options = Mock(return_value=None)

        for method, action in (
                (App._download, "download"),
                (App._preview, "expected"),
                (App._start_sync, "sync"),
                (App._schedule_download, "schedule")):
            with self.subTest(action=action):
                app._request_export_options.reset_mock()
                method(app)
                app._request_export_options.assert_called_once_with(action)


class ExportCodeCopyTests(unittest.TestCase):
    """The final review must offer Python and command-line equivalents."""

    def test_plan_dialog_copies_python_and_cli_from_the_same_options(self):
        dialog = object.__new__(PlanDialog)
        dialog.options = Mock()
        dialog.options.python_code.return_value = "python program"
        dialog.options.cli_command.return_value = "cli command"
        dialog._copy_text = Mock()

        PlanDialog._copy_python(dialog)
        PlanDialog._copy_cli(dialog)

        dialog._copy_text.assert_any_call("python program", "Python copied")
        dialog._copy_text.assert_any_call("cli command", "CLI copied")


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
