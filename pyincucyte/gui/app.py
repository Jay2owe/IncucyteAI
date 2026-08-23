"""The PyIncucyte desktop window.

Layout in one line: pick a vessel on the left, describe the export on the
right, watch it happen along the bottom.  Long work always runs on a worker
thread and reports back through a queue, so the window never freezes and the
status bar can offer a working Cancel.
"""

import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..channels import COLOR1, COLOR2, PHASE
from ..client import IncucyteClient
from ..config import ConfigStore
from ..engine import APP_DIR, DEFAULT_HOST, api_post, unpack_values
from ..errors import IncucyteError, NotLoggedInError
from ..manifest import MANIFEST_FILENAME
from ..models import (
    LAYOUT_DESCRIPTIONS, LAYOUT_LABELS, human_bytes, resolve_layout,
)
from ..options import (
    END_NOW, ExportOptions, MOMENT_HELP, SPAN_HELP, START_FIRST, START_TODAY,
)
from ..preview import DEFAULT_MAX_IMAGES
from ..processing import Unmixing
from ..wells import well_name, well_spec
from . import theme as theme_mod
from .dialogs import AboutDialog, LoginDialog, PlanDialog
from .preview import PreviewWindow
from .widgets import Card, LogView, SearchEntry, StatusBar, WellPlate, tip

GUI_STATE_FILE = APP_DIR / "gui_state.json"

#: "From" choices, and what each one means as an ExportOptions value.
START_CHOICES = ("First scan", "Today", "Last 24 hours", "Last 48 hours",
                 "Last 7 days", "Last 24 frames", "Last 100 frames",
                 "Custom...")
START_VALUES = {"First scan": START_FIRST, "Today": START_TODAY,
                "Last 24 hours": "-24h", "Last 48 hours": "-48h",
                "Last 7 days": "-7d",
                "Last 24 frames": "-24f", "Last 100 frames": "-100f"}

#: "To" choices. The relative ones are measured from the start, so
#: "First scan" + "+48 hours" is the first two days of the experiment.
END_CHOICES = ("Now", "+24 hours", "+48 hours", "+72 hours", "+7 days",
               "+24 frames", "+100 frames", "Custom...")
END_VALUES = {"Now": END_NOW, "+24 hours": "+24h", "+48 hours": "+48h",
              "+72 hours": "+72h", "+7 days": "+7d",
              "+24 frames": "+24f", "+100 frames": "+100f"}

CUSTOM = "Custom..."

LAYOUT_ORDER = ("separate", "channel_stack", "time_stack", "time_channel_stack")

#: Wells are cheap to select and expensive to look at - one thumbnail is a
#: full-size image off the device - so a preview stops at this many.
PREVIEW_MAX_IMAGES = DEFAULT_MAX_IMAGES

#: What the processing drop-downs show when a step is switched off.
PROCESSING_OFF = "off"


class App:
    """Controller and view for the main window."""

    def __init__(self, root, client=None):
        self.root = root
        self.ui_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.watcher = None
        self._last_held = None
        self.busy = False

        self.store = ConfigStore()
        self.client = client or IncucyteClient(store=self.store)
        self.vessels = []
        self.filtered_vessels = []
        self.selected_wells = {}       # vessel id -> set | None (None = all)
        self.scanned_wells = {}        # vessel id -> set of wells holding data
        self.recent_outputs = []
        self.active_vessel = None
        self.last_plan = None
        self._sort_column = None
        self._sort_reverse = False
        self._rate_samples = []
        self._rate_last = None

        state = self._read_state()
        scale = theme_mod.enable_dpi_awareness()
        self.theme = theme_mod.install(root, dark=state.get("dark"), scale=scale)
        # A PreviewSet opened from a script shares this window's palette
        # rather than restyling everything with a second Theme.
        root._pyincucyte_theme = self.theme

        root.title("PyIncucyte")
        root.minsize(1080, 760)
        root.geometry("1280x880")
        root.configure(background=self.theme["bg"])

        self._build_menu()
        self._build_ui()
        self._apply_state(state)
        self._pump()
        self._tick_clock()
        self._auto_connect()

    # ==================================================================
    # construction
    # ==================================================================

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Choose output folder...", accelerator="Ctrl+O",
                              command=self._browse_output)
        file_menu.add_command(label="Open output folder",
                              command=self._open_output_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Save settings as preset...",
                              command=self._save_preset)
        file_menu.add_command(label="Load preset...", command=self._load_preset)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Ctrl+Q",
                              command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        export_menu = tk.Menu(menubar, tearoff=0)
        export_menu.add_command(label="Preview plan", accelerator="Ctrl+P",
                                command=self._preview)
        export_menu.add_command(label="Download now", accelerator="Ctrl+D",
                                command=self._download)
        export_menu.add_command(label="Start watching", accelerator="Ctrl+W",
                                command=self._start_watch)
        export_menu.add_command(label="Stop", accelerator="Esc",
                                command=self._stop)
        menubar.add_cascade(label="Export", menu=export_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="View well images...", accelerator="Ctrl+I",
                               command=self._view_images)
        tools_menu.add_command(label="Copy CLI command",
                               command=self._copy_cli_command)
        tools_menu.add_command(label="Sign in...", command=self._login)
        tools_menu.add_command(label="Refresh vessels", accelerator="F5",
                               command=self._refresh_vessels)
        tools_menu.add_command(label="Test connection", command=self._probe)
        tools_menu.add_separator()
        # Everything below this line talks back to the instrument.
        tools_menu.add_command(label="Device status...",
                               command=self._device_status)
        tools_menu.add_command(label="Scan selected vessel now...",
                               command=self._scan_now)
        tools_menu.add_command(label="Save unmixing to instrument...",
                               command=self._save_unmix)
        tools_menu.add_separator()
        tools_menu.add_command(label="Forget saved login", command=self._logout)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Toggle dark mode", command=self._toggle_dark)
        view_menu.add_command(label="Clear log", command=lambda: self.log.clear())
        view_menu.add_command(label="Copy log", command=lambda: self.log.copy())
        view_menu.add_command(label="Save log...", command=self._save_log)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About PyIncucyte", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

        binds = {
            "<Control-o>": self._browse_output, "<Control-O>": self._browse_output,
            "<Control-p>": self._preview, "<Control-P>": self._preview,
            "<Control-d>": self._download, "<Control-D>": self._download,
            "<Control-w>": self._start_watch, "<Control-W>": self._start_watch,
            "<Control-q>": self.on_close, "<Control-Q>": self.on_close,
            "<Control-i>": self._view_images, "<Control-I>": self._view_images,
            "<F5>": self._refresh_vessels,
            "<Escape>": self._stop,
        }
        for sequence, handler in binds.items():
            self.root.bind_all(sequence, lambda e, fn=handler: fn())

    def _build_ui(self):
        pad = theme_mod.PAD_L
        outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True)

        self._build_header(outer)

        # Reserve the status strip before anything that expands, so a cramped
        # window steals height from the panels rather than hiding the Cancel
        # button.
        self.status = StatusBar(outer, self.theme, on_cancel=self._stop)
        self.status.pack(fill="x", side="bottom")
        ttk.Separator(outer).pack(fill="x", side="bottom")

        self.splitter = ttk.PanedWindow(outer, orient="vertical")
        self.splitter.pack(fill="both", expand=True, padx=pad)

        upper = ttk.Frame(self.splitter, style="TFrame")
        self.splitter.add(upper, weight=4)

        self.columns = ttk.PanedWindow(upper, orient="horizontal")
        self.columns.pack(fill="both", expand=True)

        left = ttk.Frame(self.columns, style="TFrame")
        right = ttk.Frame(self.columns, style="TFrame")
        self.columns.add(left, weight=3)
        self.columns.add(right, weight=2)

        for column in (left, right):
            column.columnconfigure(0, weight=1)
            column.rowconfigure(0, weight=1)   # absorbs spare height
            column.rowconfigure(1, weight=0)   # always gets its natural height

        self._build_vessels(left)
        self._build_wells(left)
        self._build_export(right)
        self._build_summary(right)

        lower = ttk.Frame(self.splitter, style="TFrame")
        self.splitter.add(lower, weight=1)
        self._build_log(lower)

        self.root.after(150, self._place_sashes)

    def _place_sashes(self):
        """Give the log a third of the height and the vessel column 58% width."""
        try:
            height = self.splitter.winfo_height()
            if height > 200:
                self.splitter.sashpos(0, int(height * 0.68))
            width = self.columns.winfo_width()
            if width > 400:
                self.columns.sashpos(0, int(width * 0.58))
        except tk.TclError:
            pass

    # -- header ---------------------------------------------------------

    def _build_header(self, parent):
        card = Card(parent, theme=self.theme, padding=theme_mod.PAD_M)
        card.pack(fill="x", padx=theme_mod.PAD_L, pady=(theme_mod.PAD_L,
                                                        theme_mod.PAD_M))
        row = card.body

        ttk.Label(row, text="PyIncucyte", style="Title.TLabel").pack(side="left")
        ttk.Label(row, text=f"v{__version__}", style="Muted.TLabel").pack(
            side="left", padx=(theme_mod.PAD_S, theme_mod.PAD_XL))

        ttk.Label(row, text="Device", style="Muted.TLabel").pack(side="left")
        self.host_var = tk.StringVar(value=self.client.host or DEFAULT_HOST)
        host_entry = ttk.Entry(row, textvariable=self.host_var, width=17)
        host_entry.pack(side="left", padx=(theme_mod.PAD_S, theme_mod.PAD_L))
        tip(host_entry, "The Incucyte's address on the site network. It is not "
                        "reachable from outside.", self.theme)

        self.login_btn = ttk.Button(row, text="Sign in", style="Accent.TButton",
                                    command=self._login)
        self.login_btn.pack(side="right")
        self.refresh_btn = ttk.Button(row, text="Refresh",
                                      command=self._refresh_vessels)
        self.refresh_btn.pack(side="right", padx=theme_mod.PAD_S)

        self.conn_var = tk.StringVar(value="Not connected")
        self.conn_pill = ttk.Label(row, textvariable=self.conn_var,
                                   style="Off.Pill.TLabel")
        self.conn_pill.pack(side="right", padx=theme_mod.PAD_L)

    # -- vessels --------------------------------------------------------

    def _build_vessels(self, parent):
        card = Card(parent, title="VESSELS", theme=self.theme)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, theme_mod.PAD_M))

        self.vessel_search = SearchEntry(card.actions, placeholder="Filter...",
                                         on_change=lambda v: self._populate_vessels())
        self.vessel_search.pack(side="right")

        body = card.body
        columns = ("id", "name", "owner", "plate", "last", "channels")
        headings = (("id", "ID", 46), ("name", "Name", 190), ("owner", "Owner", 68),
                    ("plate", "Plate", 58), ("last", "Last scan", 110),
                    ("channels", "Channels", 150))
        self.vessel_tree = ttk.Treeview(body, columns=columns, show="headings",
                                        height=6, selectmode="extended")
        for key, text, width in headings:
            self.vessel_tree.heading(
                key, text=text, command=lambda k=key: self._sort_vessels(k))
            self.vessel_tree.column(key, width=width, stretch=(key == "name"),
                                    anchor="w")
        scroll = ttk.Scrollbar(body, orient="vertical",
                               command=self.vessel_tree.yview)
        self.vessel_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.vessel_tree.pack(fill="both", expand=True)
        self.vessel_tree.tag_configure(
            "odd", background=self.theme["surface_alt"])
        self.vessel_tree.bind("<<TreeviewSelect>>", self._on_vessel_select)
        self.vessel_tree.bind("<Double-1>", lambda _e: self._view_images())

        self.vessel_hint = ttk.Label(
            body, style="Muted.TLabel",
            text="Sign in to list the vessels on this device.")
        self.vessel_hint.pack(anchor="w", pady=(theme_mod.PAD_S, 0))

    # -- wells ----------------------------------------------------------

    def _build_wells(self, parent):
        card = Card(parent, title="WELLS", theme=self.theme)
        card.grid(row=1, column=0, sticky="ew")
        self.wells_card = card

        self.well_count_var = tk.StringVar(value="No vessel selected")
        ttk.Label(card.actions, textvariable=self.well_count_var,
                  style="Accent.TLabel").pack(side="right")

        body = card.body
        self.plate = WellPlate(body, self.theme, on_change=self._on_wells_changed,
                               max_cell=38)
        self.plate.pack(fill="x")
        tip(self.plate,
            "Click a well to toggle it, drag to paint, shift-click for a block, "
            "and click a row letter or column number to flip a whole line.",
            self.theme)

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(theme_mod.PAD_M, 0))
        for label, command, hint in (
            ("All", self.plate.select_all, "Select every well on the plate"),
            ("None", self.plate.clear, "Clear the selection"),
            ("Invert", self.plate.invert, "Swap selected and unselected"),
            ("Scanned only", self._select_scanned,
             "Keep only wells the instrument actually imaged in the last scan"),
        ):
            button = ttk.Button(buttons, text=label, command=command)
            button.pack(side="left", padx=(0, theme_mod.PAD_S))
            tip(button, hint, self.theme)

        self.view_btn = ttk.Button(buttons, text="View images",
                                   command=self._view_images)
        self.view_btn.pack(side="left", padx=(theme_mod.PAD_M, 0))
        tip(self.view_btn,
            "Fetch a thumbnail of each selected well from the most recent scan, "
            "to check this is the right plate before downloading it.",
            self.theme)

        self.well_spec_var = tk.StringVar(value="all")
        ttk.Label(buttons, textvariable=self.well_spec_var, style="Muted.TLabel",
                  anchor="e").pack(side="right")

    # -- export settings ------------------------------------------------

    def _build_export(self, parent):
        """A compact two-column form: caption on the left, control on the right."""
        card = Card(parent, title="EXPORT", theme=self.theme)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, theme_mod.PAD_M))
        self.export_card = card
        body = card.body
        body.columnconfigure(1, weight=1)
        row = 0

        def caption(text, r):
            ttk.Label(body, text=text, style="Muted.TLabel").grid(
                row=r, column=0, sticky="nw", padx=(0, theme_mod.PAD_M),
                pady=(0, theme_mod.PAD_M))

        def line(r):
            frame = ttk.Frame(body, style="Surface.TFrame")
            frame.grid(row=r, column=1, sticky="ew", pady=(0, theme_mod.PAD_M))
            return frame

        # -- destination --------------------------------------------------
        caption("Output", row)
        folder_row = line(row)
        self.output_var = tk.StringVar()
        self.output_combo = ttk.Combobox(folder_row, textvariable=self.output_var,
                                         values=[], width=24)
        self.output_combo.pack(side="left", fill="x", expand=True)
        browse = ttk.Button(folder_row, text="...", width=3,
                            command=self._browse_output)
        browse.pack(side="left", padx=(theme_mod.PAD_XS, 0))
        tip(browse, "Choose where images are written (Ctrl+O)", self.theme)
        open_btn = ttk.Button(folder_row, text="Open", width=5,
                              command=self._open_output_folder)
        open_btn.pack(side="left", padx=(theme_mod.PAD_XS, 0))
        tip(open_btn, "Open this folder in the file browser", self.theme)
        row += 1

        # -- channels -----------------------------------------------------
        caption("Channels", row)
        channel_row = line(row)
        self.channel_vars = {PHASE: tk.BooleanVar(value=True),
                             COLOR1: tk.BooleanVar(value=False),
                             COLOR2: tk.BooleanVar(value=False)}
        self.channel_checks = {}
        for number, text in ((PHASE, "Phase"), (COLOR1, "Green"), (COLOR2, "Red")):
            check = ttk.Checkbutton(channel_row, text=text,
                                    variable=self.channel_vars[number],
                                    command=self._refresh_summary)
            check.pack(side="left", padx=(0, theme_mod.PAD_M))
            self.channel_checks[number] = check
        row += 1

        # -- layout, two by two -------------------------------------------
        caption("Layout", row)
        layout_box = line(row)
        self.layout_var = tk.StringVar(value="separate")
        for index, name in enumerate(LAYOUT_ORDER):
            radio = ttk.Radiobutton(layout_box, text=LAYOUT_LABELS[name],
                                    value=name, variable=self.layout_var,
                                    command=self._on_layout_change)
            radio.grid(row=index % 2, column=index // 2, sticky="w",
                       padx=(0, theme_mod.PAD_M))
            tip(radio, LAYOUT_DESCRIPTIONS[name], self.theme)
        row += 1

        # -- time window --------------------------------------------------
        caption("From", row)
        range_row = line(row)
        self.start_var = tk.StringVar(value="Today")
        self.start_combo = ttk.Combobox(range_row, textvariable=self.start_var,
                                        values=list(START_CHOICES),
                                        state="readonly", width=13)
        self.start_combo.pack(side="left")
        self.start_combo.bind("<<ComboboxSelected>>",
                              lambda e: self._on_window_change())
        tip(self.start_combo,
            "First scan downloads the whole experiment; Today only what the "
            "instrument captures from midnight on; Last 48 hours is a rolling "
            "window ending now; Last 24 frames counts scan times rather than "
            "clock time, so it gives a fixed-length stack.", self.theme)
        self.custom_date_var = tk.StringVar()
        self.custom_date_entry = ttk.Entry(range_row,
                                           textvariable=self.custom_date_var,
                                           width=16)
        self.custom_date_entry.bind("<FocusOut>",
                                    lambda e: self._refresh_summary())
        tip(self.custom_date_entry, MOMENT_HELP, self.theme)
        row += 1

        caption("To", row)
        end_row = line(row)
        self.end_var = tk.StringVar(value="Now")
        self.end_combo = ttk.Combobox(end_row, textvariable=self.end_var,
                                      values=list(END_CHOICES),
                                      state="readonly", width=13)
        self.end_combo.pack(side="left")
        self.end_combo.bind("<<ComboboxSelected>>",
                            lambda e: self._on_window_change())
        tip(self.end_combo,
            "Now keeps up with the instrument. The + options are measured from "
            "the start, so First scan + 48 hours is the first two days of the "
            "experiment, and + 100 frames is its first 100 scan times.",
            self.theme)
        self.custom_end_var = tk.StringVar()
        self.custom_end_entry = ttk.Entry(end_row,
                                          textvariable=self.custom_end_var,
                                          width=16)
        self.custom_end_entry.bind("<FocusOut>",
                                   lambda e: self._refresh_summary())
        tip(self.custom_end_entry,
            f"{MOMENT_HELP}. A bare date includes the whole of that day.",
            self.theme)
        row += 1

        # -- performance --------------------------------------------------
        caption("Speed", row)
        speed_row = line(row)
        ttk.Label(speed_row, text="Workers", style="Muted.TLabel").pack(side="left")
        self.workers_var = tk.IntVar(value=4)
        workers = ttk.Spinbox(speed_row, from_=1, to=16, width=4,
                              textvariable=self.workers_var)
        workers.pack(side="left", padx=(theme_mod.PAD_S, theme_mod.PAD_L))
        tip(workers, "How many images to fetch at once. More is faster until the "
                     "instrument or the network becomes the limit.", self.theme)
        ttk.Label(speed_row, text="Poll every", style="Muted.TLabel").pack(side="left")
        self.interval_var = tk.IntVar(value=10)
        interval = ttk.Spinbox(speed_row, from_=1, to=240, width=4,
                               textvariable=self.interval_var)
        interval.pack(side="left", padx=theme_mod.PAD_S)
        ttk.Label(speed_row, text="min", style="Muted.TLabel").pack(side="left")
        tip(interval, "How often Watch checks the instrument for a new scan.",
            self.theme)
        row += 1

        # -- chunked watching ----------------------------------------------
        caption("Batch", row)
        batch_row = line(row)
        ttk.Label(batch_row, text="Every", style="Muted.TLabel").pack(side="left")
        self.batch_frames_var = tk.IntVar(value=0)
        batch_frames = ttk.Spinbox(batch_row, from_=0, to=9999, width=5,
                                   textvariable=self.batch_frames_var,
                                   command=self._refresh_summary)
        batch_frames.pack(side="left", padx=theme_mod.PAD_S)
        batch_frames.bind("<FocusOut>", lambda _e: self._refresh_summary())
        tip(batch_frames,
            "Watch normally downloads a frame the moment it appears. Set a "
            "number here and it waits until that many new frames are ready, "
            "then fetches them in one go. 0 means download on sight.",
            self.theme)
        ttk.Label(batch_row, text="frames, or after",
                  style="Muted.TLabel").pack(side="left",
                                             padx=(0, theme_mod.PAD_XS))
        self.batch_after_var = tk.StringVar(value="")
        batch_after = ttk.Entry(batch_row, textvariable=self.batch_after_var,
                                width=7)
        batch_after.pack(side="left")
        batch_after.bind("<FocusOut>", lambda _e: self._refresh_summary())
        tip(batch_after,
            f"...or once the oldest waiting frame is this old: {SPAN_HELP}. "
            f"Leave blank for no time limit. '7d' collects a week at a time - "
            f"start it with the experiment and come back on Monday.",
            self.theme)
        row += 1

        # -- switches -----------------------------------------------------
        caption("Options", row)
        toggles = line(row)
        self.green_lut_var = tk.BooleanVar(value=False)
        self.green_lut_check = ttk.Checkbutton(
            toggles, text="Green LUT on Phase", variable=self.green_lut_var,
            command=self._refresh_summary)
        self.green_lut_check.pack(side="left", padx=(0, theme_mod.PAD_M))
        tip(self.green_lut_check,
            "Recolours Phase as a green RGB image. Display only - it changes "
            "pixel values, so leave it off for anything you will analyse.",
            self.theme)

        self.append_var = tk.BooleanVar(value=True)
        self.append_check = ttk.Checkbutton(
            toggles, text="Extend stacks", variable=self.append_var)
        self.append_check.pack(side="left", padx=(0, theme_mod.PAD_M))
        tip(self.append_check,
            "A time stack has to hold every frame, so a new scan would normally "
            "mean rewriting the whole file. This adds the new frames to the end "
            "of the file already on disk instead. Turn it off to write every "
            "stack whole.", self.theme)

        self.manifest_var = tk.BooleanVar(value=True)
        manifest_check = ttk.Checkbutton(
            toggles, text="Write manifest", variable=self.manifest_var)
        manifest_check.pack(side="left")
        tip(manifest_check,
            f"Writes {MANIFEST_FILENAME} and a CSV index listing every file with "
            f"its well, channel and timepoint - what an analysis pipeline reads "
            f"instead of parsing filenames.", self.theme)
        row += 1

        # -- preprocessing -------------------------------------------------
        caption("Processing", row)
        processing_row = line(row)
        self.calibrate_var = tk.BooleanVar(value=False)
        calibrate_check = ttk.Checkbutton(
            processing_row, text="Calibrated units",
            variable=self.calibrate_var, command=self._refresh_summary)
        calibrate_check.pack(side="left", padx=(0, theme_mod.PAD_M))
        tip(calibrate_check,
            "Convert raw camera counts to the instrument's calibrated units "
            "(GCU/RCU) using its own Scale and Bias, and write 32-bit float. "
            "Phase has no calibration and is never touched.", self.theme)

        ttk.Label(processing_row, text="Unmix", style="Muted.TLabel").pack(
            side="left", padx=(0, theme_mod.PAD_XS))
        self.unmix_var = tk.StringVar(value=PROCESSING_OFF)
        unmix_box = ttk.Combobox(processing_row, textvariable=self.unmix_var,
                                 values=(PROCESSING_OFF, "device"), width=13)
        unmix_box.pack(side="left", padx=(0, theme_mod.PAD_M))
        unmix_box.bind("<<ComboboxSelected>>",
                       lambda _event: self._refresh_summary())
        tip(unmix_box,
            "Linear (spectral) unmixing. 'device' uses the percentages saved "
            "on the vessel in the Incucyte software; or type a term like "
            "green:8%red. Reading the other channel costs an extra download "
            "per image.", self.theme)

        ttk.Label(processing_row, text="Background", style="Muted.TLabel").pack(
            side="left", padx=(0, theme_mod.PAD_XS))
        self.background_var = tk.StringVar(value=PROCESSING_OFF)
        background_box = ttk.Combobox(
            processing_row, textvariable=self.background_var,
            values=(PROCESSING_OFF, "device"), width=9)
        background_box.pack(side="left")
        background_box.bind("<<ComboboxSelected>>",
                            lambda _event: self._refresh_summary())
        tip(background_box,
            "Subtract a background level before unmixing. 'device' uses the "
            "level the instrument measured for each image, or type a number "
            "of raw counts.", self.theme)

    # -- summary + actions ----------------------------------------------

    def _build_summary(self, parent):
        card = Card(parent, title="SUMMARY", theme=self.theme)
        card.grid(row=1, column=0, sticky="ew")
        body = card.body

        self.summary_var = tk.StringVar(value="Select a vessel to begin.")
        ttk.Label(body, textvariable=self.summary_var, style="Surface.TLabel",
                  wraplength=340, justify="left").pack(anchor="w")

        self.estimate_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.estimate_var, style="Muted.TLabel",
                  wraplength=340, justify="left").pack(
            anchor="w", pady=(theme_mod.PAD_S, 0))

        self.window_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.window_var, style="Muted.TLabel",
                  wraplength=340, justify="left").pack(
            anchor="w", pady=(theme_mod.PAD_S, 0))

        self.example_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.example_var, style="Muted.TLabel",
                  wraplength=340, justify="left").pack(
            anchor="w", pady=(theme_mod.PAD_S, theme_mod.PAD_M))

        actions = ttk.Frame(body, style="Surface.TFrame")
        actions.pack(fill="x")
        self.download_btn = ttk.Button(actions, text="Download",
                                       style="Accent.TButton",
                                       command=self._download)
        self.download_btn.pack(side="left")
        self.preview_btn = ttk.Button(actions, text="Preview",
                                      command=self._preview)
        self.preview_btn.pack(side="left", padx=theme_mod.PAD_S)
        self.watch_btn = ttk.Button(actions, text="Watch",
                                    command=self._start_watch)
        self.watch_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", style="Danger.TButton",
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side="right")

        tip(self.preview_btn, "Count exactly what would be downloaded, without "
                              "fetching anything (Ctrl+P)", self.theme)
        tip(self.watch_btn, "Keep polling and download each new scan as it "
                            "appears (Ctrl+W)", self.theme)

    # -- log ------------------------------------------------------------

    def _build_log(self, parent):
        card = Card(parent, title="ACTIVITY", theme=self.theme)
        card.pack(fill="both", expand=True, pady=(theme_mod.PAD_M, theme_mod.PAD_M))
        self.log = LogView(card.body, self.theme, height=6)
        self.log.pack(fill="both", expand=True)
        ttk.Checkbutton(card.actions, text="Follow",
                        variable=self.log.autoscroll).pack(side="right")

    # ==================================================================
    # thread plumbing
    # ==================================================================

    def _post(self, callback, *args, **kwargs):
        """Queue work for the Tk thread (safe to call from any thread)."""
        self.ui_queue.put((callback, args, kwargs))

    def _pump(self):
        while True:
            try:
                callback, args, kwargs = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args, **kwargs)
            except tk.TclError:
                pass
            except Exception as exc:
                self.log.write(f"UI error: {exc}", "error")
        self.root.after(60, self._pump)

    def say(self, message, level="info"):
        """Log from any thread."""
        self._post(self.log.write, message, level)

    def _run_worker(self, target, *args):
        if self.busy:
            messagebox.showinfo("Busy", "Something is already running.",
                                parent=self.root)
            return False
        self.cancel_event.clear()
        self._set_busy(True)
        self.worker = threading.Thread(target=self._guarded, args=(target,) + args,
                                       daemon=True)
        self.worker.start()
        return True

    def _guarded(self, target, *args):
        try:
            target(*args)
        except NotLoggedInError as exc:
            self.say(str(exc), "warn")
            self._post(self._prompt_login)
        except IncucyteError as exc:
            self.say(str(exc), "error")
        except Exception as exc:
            self.say(f"Unexpected error: {exc}", "error")
        finally:
            self._post(self._finish_work)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.download_btn, self.preview_btn, self.watch_btn,
                       self.refresh_btn, self.view_btn):
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.status.set_busy(busy)

    def _finish_work(self):
        if self.watcher and self.watcher.is_running:
            return
        self._set_busy(False)
        self.status.reset("Ready")
        self.root.title("PyIncucyte")
        self._rate_samples = []
        self._rate_last = None

    # ==================================================================
    # connection
    # ==================================================================

    def _auto_connect(self):
        credentials = self.store.load()
        if credentials.token_valid or credentials.can_refresh:
            self.client = IncucyteClient(self.host_var.get().strip() or None,
                                         store=self.store)
            self._update_connection()
            if credentials.token_valid:
                self.say("Signed in with saved credentials.", "success")
                self._refresh_vessels()
                return
        self._update_connection()
        self.say("Not signed in. Use Sign in to connect to the instrument.",
                 "muted")

    def _update_connection(self):
        credentials = self.client.credentials
        if credentials.token_valid:
            hours = credentials.token_seconds_left / 3600
            unit = f"{hours:.1f}h" if hours >= 1 else \
                f"{credentials.token_seconds_left / 60:.0f}m"
            self.conn_var.set(f"{credentials.username or 'connected'} · {unit}")
            self.conn_pill.configure(style="Ok.Pill.TLabel")
            self.login_btn.configure(text="Switch user")
        elif credentials.can_refresh:
            self.conn_var.set(f"{credentials.username} · token expired")
            self.conn_pill.configure(style="Warn.Pill.TLabel")
            self.login_btn.configure(text="Sign in")
        else:
            self.conn_var.set("Not connected")
            self.conn_pill.configure(style="Off.Pill.TLabel")
            self.login_btn.configure(text="Sign in")

    def _tick_clock(self):
        self._update_connection()
        if self.watcher and self.watcher.is_running:
            remaining = self.watcher.seconds_until_next_poll()
            if remaining:
                minutes, seconds = divmod(remaining, 60)
                self.root.title(f"PyIncucyte - next poll in {minutes}m {seconds:02d}s")
        self.root.after(1000, self._tick_clock)

    def _login(self):
        host = self.host_var.get().strip() or DEFAULT_HOST
        self.client.host = host
        dialog = LoginDialog(self.root, self.theme, self.client, host=host)
        if dialog.show():
            self._update_connection()
            self.say(f"Signed in as {self.client.username} on {host}.", "success")
            self._refresh_vessels()

    def _prompt_login(self):
        if messagebox.askyesno("Sign in required",
                               "You need to sign in to the Incucyte. Do that now?",
                               parent=self.root):
            self._login()

    def _logout(self):
        self.client.logout()
        self.vessels = []
        self._populate_vessels()
        self._update_connection()
        self.say("Saved login removed.", "warn")

    def _probe(self):
        self.client.host = self.host_var.get().strip() or DEFAULT_HOST
        self._run_worker(self._probe_worker)

    def _probe_worker(self):
        self._post(self.status.set_message, "Testing connection...")
        report = self.client.probe()
        open_ports = ", ".join(str(p) for p, ok in report["ports"].items() if ok)
        if report["api"]:
            self.say(f"{report['host']} reachable (ports {open_ports or 'none'}); "
                     f"API responded.", "success")
        else:
            self.say(f"{report['host']} did not respond: "
                     f"{report.get('error', 'unreachable')}", "error")
            self.say("The instrument is only routable from the site network.",
                     "muted")

    # ==================================================================
    # the instrument itself
    #
    # Reading is free.  The two writes each put the question in a dialog
    # first, because the Incucyte is shared and a mis-click here is somebody
    # else's experiment.
    # ==================================================================

    def _device_status(self):
        self._run_worker(self._device_status_worker)

    def _device_status_worker(self):
        self._post(self.status.set_message, "Reading device status...")
        state = self.client.device_state()
        self.say(f"Instrument: {state.summary()}",
                 "warn" if state.has_problem else "success")
        for line in state.describe()[1:]:
            self.say(f"  {line}", "muted")

    def _one_selected_vessel(self, action):
        """The single selected vessel id, or None having said why not."""
        selected = self._selected_vessel_ids()
        if len(selected) != 1:
            messagebox.showinfo("Pick one vessel",
                                f"Select exactly one vessel to {action}.",
                                parent=self.root)
            return None
        return selected[0]

    def _scan_now(self):
        vessel_id = self._one_selected_vessel("scan")
        if vessel_id is None:
            return
        vessel = self._vessel(vessel_id)
        name = vessel.label if vessel else f"vessel {vessel_id}"
        if not messagebox.askyesno(
                "Scan now?",
                f"Ask the Incucyte to scan {name} now?\n\n"
                f"This adds work to the instrument. Nothing already scheduled "
                f"is cancelled or moved, and there is no way to call it back.",
                parent=self.root):
            return
        self._run_worker(self._scan_now_worker, vessel_id)

    def _scan_now_worker(self, vessel_id):
        self._post(self.status.set_message, "Requesting a scan...")
        state = self.client.begin_scan(vessel_id, confirm=True)
        self.say(f"Asked the instrument to scan vessel {vessel_id}.", "success")
        self.say(f"  Instrument: {state.summary()}", "muted")

    def _save_unmix(self):
        vessel_id = self._one_selected_vessel("save unmixing onto")
        if vessel_id is None:
            return
        try:
            spec = self._current_options().unmix
        except ValueError as exc:
            messagebox.showerror("Unmixing", str(exc), parent=self.root)
            return
        mixing = Unmixing.coerce(spec)
        change = (f"Save {mixing.describe()} onto vessel {vessel_id}?"
                  if mixing else
                  f"Clear the unmixing saved on vessel {vessel_id}?")
        if not messagebox.askyesno(
                "Change the instrument?",
                f"{change}\n\n"
                f"This changes what the Incucyte's own software displays, for "
                f"everyone. Downloading with these values needs none of it - "
                f"that arithmetic happens here.",
                parent=self.root):
            return
        self._run_worker(self._save_unmix_worker, vessel_id, mixing)

    def _save_unmix_worker(self, vessel_id, mixing):
        self._post(self.status.set_message, "Saving unmixing...")
        self.client.save_unmix(vessel_id, mixing, confirm=True)
        self.say(f"Saved {mixing.describe()} onto vessel {vessel_id}.",
                 "success")

    # ==================================================================
    # vessels
    # ==================================================================

    def _refresh_vessels(self):
        self.client.host = self.host_var.get().strip() or DEFAULT_HOST
        if not (self.client.credentials.token_valid
                or self.client.credentials.can_refresh):
            self.say("Sign in before listing vessels.", "warn")
            return
        self._run_worker(self._refresh_vessels_worker)

    def _refresh_vessels_worker(self):
        self._post(self.status.set_message, "Fetching vessels...")
        self._post(self.status.start_indeterminate)
        vessels = self.client.vessels(refresh=True)
        self.vessels = vessels
        self._post(self._populate_vessels)
        self.say(f"Found {len(vessels)} vessels on {self.client.host}.", "success")

    def _populate_vessels(self):
        query = self.vessel_search.value.lower() if hasattr(self, "vessel_search") else ""
        previously = set(self.vessel_tree.selection())

        rows = []
        for vessel in self.vessels:
            haystack = " ".join(str(part) for part in (
                vessel.id, vessel.name, vessel.owner, vessel.type_name,
                vessel.channel_summary)).lower()
            if query and query not in haystack:
                continue
            rows.append(vessel)

        if self._sort_column:
            rows.sort(key=lambda v: self._sort_key(v, self._sort_column),
                      reverse=self._sort_reverse)
        self.filtered_vessels = rows

        self.vessel_tree.delete(*self.vessel_tree.get_children())
        for index, vessel in enumerate(rows):
            self.vessel_tree.insert(
                "", "end", iid=str(vessel.id),
                tags=("odd",) if index % 2 else (),
                values=(vessel.id, vessel.name or "-", vessel.owner or "-",
                        vessel.plate_format,
                        vessel.last_scan.strftime("%d/%m/%y %H:%M")
                        if vessel.last_scan else "-",
                        vessel.channel_summary or "-"))

        restore = [iid for iid in previously
                   if self.vessel_tree.exists(iid)]
        if restore:
            self.vessel_tree.selection_set(restore)

        if not self.vessels:
            self.vessel_hint.configure(
                text="No vessels loaded. Sign in, then press Refresh.")
        elif not rows:
            self.vessel_hint.configure(text=f"No vessel matches '{query}'.")
        else:
            self.vessel_hint.configure(
                text=f"{len(rows)} of {len(self.vessels)} vessels shown. "
                     f"Ctrl-click to select several.")
        self._refresh_summary()

    @staticmethod
    def _sort_key(vessel, column):
        return {
            "id": vessel.id,
            "name": (vessel.name or "").lower(),
            "owner": (vessel.owner or "").lower(),
            "plate": vessel.well_count,
            "last": vessel.last_scan or datetime.min,
            "channels": (vessel.channel_summary or "").lower(),
        }.get(column, vessel.id)

    def _sort_vessels(self, column):
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column, self._sort_reverse = column, False
        self._populate_vessels()

    def _selected_vessel_ids(self):
        ids = []
        for iid in self.vessel_tree.selection():
            try:
                ids.append(int(iid))
            except (TypeError, ValueError):
                continue
        return ids

    def _vessel(self, vessel_id):
        for vessel in self.vessels:
            if vessel.id == vessel_id:
                return vessel
        return None

    def _on_vessel_select(self, _event=None):
        ids = self._selected_vessel_ids()
        if not ids:
            return
        vessel = self._vessel(ids[0])
        if vessel is None:
            return
        self.active_vessel = vessel.id
        self.plate.configure_plate(
            vessel.rows, vessel.cols,
            selection=self.selected_wells.get(vessel.id),
            available=self.scanned_wells.get(vessel.id))
        self.wells_card.set_title(
            f"WELLS - VESSEL {vessel.id} ({vessel.plate_format})")
        self._sync_channel_checks(vessel)
        if vessel.id not in self.scanned_wells:
            threading.Thread(target=self._load_scanned_wells, args=(vessel,),
                             daemon=True).start()
        self._refresh_summary()

    def _sync_channel_checks(self, vessel):
        """Name the channel boxes after the vessel, and grey out unused ones."""
        for number, check in self.channel_checks.items():
            label = vessel.channel_labels.get(number, str(number))
            if number != PHASE:
                label = f"{label} (Color {1 if number == COLOR1 else 2})"
            available = (not vessel.active_channels) or (
                number in vessel.active_channels)
            check.configure(text=label,
                            state="normal" if available else "disabled")
            if not available:
                self.channel_vars[number].set(False)

    def _load_scanned_wells(self, vessel):
        """Ask the last scan which wells actually hold images."""
        if not vessel.last_scan:
            return
        try:
            token = self.client.ensure_token()
            data = api_post(self.client.host, token, "Vessels/GetScanVessel", {
                "VesselID": vessel.id,
                "DateTime": vessel.last_scan.isoformat(),
                "IncludeDiagnosticMetrics": False,
            })
            scan = unpack_values(data.get("Data", {}))
            wells = set()
            for image in scan.get("ImageInfos", []) or []:
                swell = image.get("Swell", {}) or {}
                wells.add((swell.get("RowZeroBased", 0),
                           swell.get("ColumnZeroBased", 0)))
        except Exception:
            return
        if not wells:
            return
        self.scanned_wells[vessel.id] = wells
        self._post(self._apply_scanned_wells, vessel.id, wells)

    def _apply_scanned_wells(self, vessel_id, wells):
        if self.active_vessel != vessel_id:
            return
        self.plate.available = wells
        self.plate.redraw()
        self.say(f"Vessel {vessel_id}: {len(wells)} wells contain images "
                 f"in the most recent scan.", "muted")

    # ==================================================================
    # wells + summary
    # ==================================================================

    def _on_wells_changed(self, selection):
        if self.active_vessel is None:
            return
        full = self.plate.all_wells()
        self.selected_wells[self.active_vessel] = (
            None if selection == full else set(selection))
        self.well_count_var.set(f"{len(selection)} / {self.plate.total} selected")
        spec = "all" if selection == full else (well_spec(selection) or "none")
        self.well_spec_var.set(spec if len(spec) <= 46 else spec[:43] + "...")
        self._refresh_summary()

    def _select_scanned(self):
        if self.active_vessel is None:
            return
        available = self.scanned_wells.get(self.active_vessel)
        if not available:
            self.say("Which wells hold images is not known yet - "
                     "select a vessel with a completed scan.", "warn")
            return
        self.plate.set_selection(available)

    def _on_layout_change(self):
        layout = self.layout_var.get()
        stacked = layout != "separate"
        if stacked:
            self.green_lut_var.set(False)
        self.green_lut_check.configure(state="disabled" if stacked else "normal")
        # Only a time stack is ever rewritten to gain a frame; the per-scan
        # layouts write one file per moment and simply skip the ones they have.
        self.append_check.configure(
            state="normal" if "time" in layout else "disabled")
        self._refresh_summary()

    def _on_window_change(self):
        """Show a text box only for the choices that need one."""
        for choice, entry, variable in (
            (self.start_var.get(), self.custom_date_entry, self.custom_date_var),
            (self.end_var.get(), self.custom_end_entry, self.custom_end_var),
        ):
            if choice == CUSTOM:
                entry.pack(side="left", padx=(theme_mod.PAD_S, 0))
                if not variable.get():
                    variable.set(date.today().isoformat())
            else:
                entry.pack_forget()
        self._refresh_summary()

    # The old name, still used by the settings loader.
    _on_start_change = _on_window_change

    def _selected_channels(self):
        return [n for n, var in self.channel_vars.items() if var.get()]

    def _refresh_summary(self):
        ids = self._selected_vessel_ids()
        channels = self._selected_channels()
        layout = self.layout_var.get()

        if not ids:
            self.summary_var.set("Select a vessel to begin.")
            self.estimate_var.set("")
            self.example_var.set("")
            self.window_var.set("")
            return
        if not channels:
            self.summary_var.set("Select at least one channel.")
            self.estimate_var.set("")
            self.example_var.set("")
            self.window_var.set("")
            return

        wells_total = 0
        for vessel_id in ids:
            vessel = self._vessel(vessel_id)
            selection = self.selected_wells.get(vessel_id)
            wells_total += (vessel.well_count if selection is None and vessel
                            else len(selection or ()))

        vessel = self._vessel(ids[0])
        names = [vessel.channel_labels.get(n, str(n)) if vessel else str(n)
                 for n in sorted(channels)]
        self.summary_var.set(
            f"{len(ids)} vessel{'s' if len(ids) != 1 else ''} · "
            f"{wells_total} well{'s' if wells_total != 1 else ''} · "
            f"{' + '.join(names)}\n{LAYOUT_DESCRIPTIONS[layout]}")

        per_scan_layout = layout in ("separate", "channel_stack")
        count = wells_total * (len(channels) if layout in ("separate", "time_stack")
                               else 1)
        size = wells_total * len(channels) * 2_500_000
        if per_scan_layout:
            estimate = (f"{count:,} files per scan time · "
                        f"about {human_bytes(size)} per scan time.")
        else:
            estimate = (f"{count:,} stack files in total · "
                        f"about {human_bytes(size)} of source images per scan time.")
        recipe = ExportOptions(
            calibrate=bool(self.calibrate_var.get()),
            unmix=_processing_value(self.unmix_var.get()),
            background=_processing_value(self.background_var.get())).recipe
        if recipe.is_active:
            estimate += f" Pixels: {recipe.describe()}."
        self.estimate_var.set(estimate + " Press Preview for the exact count.")

        example_channels = "-".join(
            n.lower().replace(" ", "-") for n in names) or "phase"
        first_well = "A1"
        if vessel and self.selected_wells.get(vessel.id):
            row, col = sorted(self.selected_wells[vessel.id])[0]
            first_well = well_name(row, col)
        if layout == "separate":
            example = f"VID{ids[0]}_{first_well}_1_00d00h00m.tif"
        elif layout == "channel_stack":
            example = f"VID{ids[0]}_{first_well}_{example_channels}_00d00h00m.tif"
        else:
            example = f"VID{ids[0]}_{first_well}_{example_channels}_timestack.tif"
        self.example_var.set(f"Example filename: {example}")
        self._refresh_window_label(ids)

    def _refresh_window_label(self, vessel_ids):
        """Spell out the window the current From/To choices actually mean."""
        try:
            options = self._current_options()
        except (ValueError, tk.TclError):
            self.window_var.set("")
            return
        first = None
        for vessel_id in vessel_ids:
            vessel = self._vessel(vessel_id)
            if vessel and vessel.first_scan:
                first = (vessel.first_scan if first is None
                         else min(first, vessel.first_scan))
        try:
            description = options.window_description(first_scan=first)
        except ValueError as exc:
            self.window_var.set(str(exc).split(".")[0])
            return
        self.window_var.set(f"Window: {description}")

    # ==================================================================
    # options bridge
    # ==================================================================

    def _current_options(self):
        ids = self._selected_vessel_ids()
        channels = self._selected_channels()
        channel_spec = "all" if len(channels) == 3 else ",".join(
            {PHASE: "phase", COLOR1: "green", COLOR2: "red"}[n]
            for n in sorted(channels))

        start_from = START_VALUES.get(
            self.start_var.get(),
            self.custom_date_var.get().strip() or date.today().isoformat())
        end_at = END_VALUES.get(
            self.end_var.get(), self.custom_end_var.get().strip() or END_NOW)

        wells_by_vessel = {}
        for vessel_id in ids:
            selection = self.selected_wells.get(vessel_id)
            if selection is not None:
                wells_by_vessel[str(vessel_id)] = well_spec(selection)

        return ExportOptions(
            output=self.output_var.get().strip(),
            vessels=ids,
            wells="all",
            wells_by_vessel=wells_by_vessel,
            channels=channel_spec or "all",
            layout=resolve_layout(self.layout_var.get()),
            start_from=start_from,
            end_at=end_at,
            green_lut=bool(self.green_lut_var.get()),
            calibrate=bool(self.calibrate_var.get()),
            unmix=_processing_value(self.unmix_var.get()),
            background=_processing_value(self.background_var.get()),
            workers=int(self.workers_var.get() or 4),
            interval_minutes=int(self.interval_var.get() or 10),
            batch_frames=int(self.batch_frames_var.get() or 0),
            batch_after=self.batch_after_var.get().strip(),
            host=self.host_var.get().strip() or DEFAULT_HOST,
            write_manifest=bool(self.manifest_var.get()),
            append_stacks=bool(self.append_var.get()),
        )

    def _apply_options(self, options):
        if options.output:
            self.output_var.set(options.output)
        if options.host:
            self.host_var.set(options.host)
        selected = options.channel_set
        for number, var in self.channel_vars.items():
            var.set(True if selected is None else number in selected)
        self.layout_var.set(options.layout)
        self.workers_var.set(options.workers)
        self.interval_var.set(options.interval_minutes)
        self.batch_frames_var.set(options.batch_frames)
        self.batch_after_var.set(options.batch_after)
        self.green_lut_var.set(options.green_lut)
        self.calibrate_var.set(options.calibrate)
        self.unmix_var.set(options.unmix or PROCESSING_OFF)
        self.background_var.set(options.background or PROCESSING_OFF)
        self.manifest_var.set(options.write_manifest)
        self.append_var.set(options.append_stacks)

        self._set_choice(self.start_var, self.custom_date_var,
                         START_VALUES, options.start_from)
        self._set_choice(self.end_var, self.custom_end_var,
                         END_VALUES, options.end_at or END_NOW)

        for key, spec in options.wells_by_vessel.items():
            try:
                self.selected_wells[int(key)] = options.wells_for(int(key))
            except (TypeError, ValueError):
                continue

        if options.vessels:
            available = [str(v) for v in options.vessels
                         if self.vessel_tree.exists(str(v))]
            if available:
                self.vessel_tree.selection_set(available)
                self._on_vessel_select()

        self._on_layout_change()
        self._on_window_change()
        self._refresh_summary()

    @staticmethod
    def _set_choice(choice_var, custom_var, table, value):
        """Select the named choice for a value, or fall back to Custom."""
        for label, mapped in table.items():
            if mapped == value:
                choice_var.set(label)
                return
        choice_var.set(CUSTOM)
        custom_var.set(str(value))

    def _validate(self):
        try:
            options = self._current_options()
        except ValueError as exc:
            # A free-text field (the custom date, or the batch delay) holds
            # something that is not a time at all.
            messagebox.showwarning("Not ready", str(exc), parent=self.root)
            return None
        problems = options.validate()
        if not (self.client.credentials.token_valid
                or self.client.credentials.can_refresh):
            problems.insert(0, "Not signed in.")
        if problems:
            messagebox.showwarning("Not ready", "\n".join(problems),
                                   parent=self.root)
            return None
        self._remember_output(options.output)
        self._save_state()
        return options

    # ==================================================================
    # export actions
    # ==================================================================

    def _progress(self, event):
        self._post(self._show_progress, event)

    def _show_progress(self, event):
        labels = {"scanning": "Looking for scans", "planning": "Building file list",
                  "downloading": "Downloading", "writing": "Writing stacks",
                  "waiting": "Waiting", "polling": "Checking for new scans",
                  "done": "Finished"}
        self.status.set_message(labels.get(event.stage, event.stage),
                                event.detail[:70])
        self.status.set_progress(event.done, event.total)
        if event.total and event.stage in ("downloading", "writing"):
            now = time.monotonic()
            if self._rate_last is not None:
                self._rate_samples.append(now - self._rate_last)
                self._rate_samples = self._rate_samples[-25:]
            self._rate_last = now
            if self._rate_samples:
                average = sum(self._rate_samples) / len(self._rate_samples)
                remaining = int(average * max(0, event.total - event.done))
                minutes, seconds = divmod(remaining, 60)
                self.status.rate_var.set(
                    f"{event.done:,}/{event.total:,} · ~{minutes}m {seconds:02d}s left")
            self.root.title(
                f"PyIncucyte - {event.percent}% ({event.done:,}/{event.total:,})")

    def _preview(self):
        options = self._validate()
        if options:
            self._run_worker(self._plan_worker, options, False)

    # -- looking at the wells --------------------------------------------

    def _view_images(self):
        """Thumbnails of the selected wells: is this the right plate?"""
        if not (self.client.credentials.token_valid
                or self.client.credentials.can_refresh):
            self.say("Sign in before looking at wells.", "warn")
            self._prompt_login()
            return
        ids = self._selected_vessel_ids()
        if not ids:
            self.say("Select a vessel first, then View images.", "warn")
            return
        vessel_id = self.active_vessel if self.active_vessel in ids else ids[0]
        # Preview whatever the Processing row is set to, so what is on screen
        # is what a download would write.
        self._run_worker(self._view_images_worker, vessel_id,
                         self.selected_wells.get(vessel_id),
                         self._selected_channels(),
                         {"calibrate": bool(self.calibrate_var.get()),
                          "unmix": _processing_value(self.unmix_var.get()),
                          "background": _processing_value(
                              self.background_var.get())})

    def _view_images_worker(self, vessel_id, wells, channels, recipe=None):
        self.say(f"Vessel {vessel_id}: finding the most recent scan ...")
        scans = self.client.find_scans(vessel=vessel_id, most_recent=1,
                                       progress=self._progress,
                                       cancel=self.cancel_event)
        if self.cancel_event.is_set():
            return
        if not scans:
            self.say(f"Vessel {vessel_id} has no scan holding images.", "warn")
            return
        scan = scans[0]
        result = self.client.preview(
            scan, wells=wells, channels=channels or None,
            max_images=PREVIEW_MAX_IMAGES, progress=self._progress,
            cancel=self.cancel_event, **(recipe or {}))
        if self.cancel_event.is_set():
            self.say("Preview cancelled.", "muted")
            return
        self.say(f"{scan.label}: {result.summary()}")
        if result.skipped:
            self.say(f"Only the first {PREVIEW_MAX_IMAGES} wells are shown - "
                     f"each thumbnail is a full-size image off the device.",
                     "muted")
        for message in result.errors[:3]:
            self.say(message, "warn")
        self._post(self._open_preview_window, result)

    def _open_preview_window(self, result):
        if result.is_empty:
            messagebox.showinfo(
                "Nothing to show",
                "That scan holds no images for the selected wells.",
                parent=self.root)
            return
        PreviewWindow(self.root, self.theme, result)

    def _download(self):
        options = self._validate()
        if options:
            self._run_worker(self._plan_worker, options, True)

    def _plan_worker(self, options, then_download):
        self.say(f"Planning export to {options.output} ...")
        plan = self.client.plan(options, progress=self._progress,
                                cancel=self.cancel_event)
        self.last_plan = plan
        if self.cancel_event.is_set():
            self.say("Cancelled.", "warn")
            return
        for line in plan.summary().splitlines():
            self.say(line, "info")
        if plan.is_empty:
            self.say("Nothing new to download - the folder is already up to date.",
                     "success")
            return
        if then_download:
            self._post(self._confirm_and_download, plan, options)
        else:
            self._post(self._show_plan_dialog, plan, options)

    def _show_plan_dialog(self, plan, options):
        PlanDialog(self.root, self.theme, plan, options).show()

    def _confirm_and_download(self, plan, options):
        if not PlanDialog(self.root, self.theme, plan, options).show():
            self.say("Download cancelled.", "warn")
            self._finish_work()
            return
        self._set_busy(True)
        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._guarded, args=(self._download_worker, plan, options),
            daemon=True)
        self.worker.start()

    def _download_worker(self, plan, options):
        result = self.client.download(plan, progress=self._progress,
                                      cancel=self.cancel_event)
        level = "warn" if result.cancelled else (
            "error" if result.errors else "success")
        self.say(result.summary(), level)
        for message in result.errors[:8]:
            self.say(message, "error")
        if len(result.errors) > 8:
            self.say(f"... and {len(result.errors) - 8} more failures", "error")
        if result.manifest_path:
            self.say(f"Manifest written to {result.manifest_path}", "muted")
        if result.cache is not None and result.cache.hits:
            self.say(f"Reused {result.cache.hits:,} cached source images "
                     f"instead of re-downloading them.", "muted")

    # -- watch ----------------------------------------------------------

    def _start_watch(self):
        if self.watcher and self.watcher.is_running:
            self.say("Already watching.", "warn")
            return
        options = self._validate()
        if not options:
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.watch_btn.configure(state="disabled")
        self.say(f"Watching for new scans every {options.interval_minutes} "
                 f"minutes; new images go to {options.output}.", "success")
        if options.batches:
            self.say(f"Holding new frames until {options.batch_description}.",
                     "muted")
        self._last_held = None
        self.watcher = self.client.watch(
            options, progress=self._progress,
            on_result=self._watch_result,
            on_hold=lambda watcher: self._watch_hold(watcher.hold_description,
                                                      watcher.pending_frames),
            on_error=lambda exc: self.say(f"Poll failed: {exc}", "error"))

    # Both of these run on the watcher thread; say() is what crosses back.
    def _watch_result(self, result):
        self._last_held = None
        self.say(result.summary(), "success")

    def _watch_hold(self, description, frames):
        """Log a held chunk once per change, not once per poll."""
        if frames == self._last_held:
            return
        self._last_held = frames
        self.say(description, "muted")

    def _stop(self):
        stopped = False
        if self.watcher and self.watcher.is_running:
            self.watcher.stop()
            stopped = True
        if self.busy:
            self.cancel_event.set()
            stopped = True
        if stopped:
            self.say("Stopping...", "warn")
            self.status.set_message("Stopping...")
            self.root.after(400, self._finish_watch)

    def _finish_watch(self):
        if self.watcher and self.watcher.is_running:
            self.root.after(400, self._finish_watch)
            return
        watcher, self.watcher = self.watcher, None
        if watcher and watcher.pending_frames and self._offer_the_held_chunk(watcher):
            return                      # the flush thread finishes the work
        self._finish_work()

    def _offer_the_held_chunk(self, watcher):
        """Stopping mid-chunk should be a choice, not a silent abandonment."""
        held = watcher.pending_frames
        plural = "" if held == 1 else "s"
        if not messagebox.askyesno(
                "Frames still waiting",
                f"{held} frame{plural} have been held for a later chunk and "
                f"not downloaded yet.\n\nDownload them now? They stay on the "
                f"instrument either way.",
                parent=self.root):
            self.say(f"{watcher.hold_description}; still on the instrument.",
                     "muted")
            return False
        self.say(f"Collecting the {held} held frame{plural}...")
        self.worker = threading.Thread(
            target=self._guarded, args=(self._flush_watcher, watcher),
            name="pyincucyte-flush", daemon=True)
        self.worker.start()
        return True

    def _flush_watcher(self, watcher):
        """Runs on a worker thread; _guarded reports and clears the busy state."""
        result = watcher.flush()
        if result and result.files:
            self.say(result.summary(), "success")
        else:
            self.say("Nothing left to collect.", "muted")

    # ==================================================================
    # files, presets, misc
    # ==================================================================

    def _browse_output(self):
        folder = filedialog.askdirectory(
            title="Choose the output folder", parent=self.root,
            initialdir=self.output_var.get() or str(Path.home()))
        if folder:
            self.output_var.set(folder)
            self._remember_output(folder)

    def _remember_output(self, folder):
        if not folder:
            return
        if folder in self.recent_outputs:
            self.recent_outputs.remove(folder)
        self.recent_outputs.insert(0, folder)
        self.recent_outputs = self.recent_outputs[:8]
        self.output_combo.configure(values=self.recent_outputs)

    def _open_output_folder(self):
        folder = self.output_var.get().strip()
        if not folder or not Path(folder).exists():
            self.say("Output folder does not exist yet.", "warn")
            return
        try:
            if sys.platform == "win32":
                import os
                os.startfile(folder)          # noqa: S606 - opening a folder
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            self.say(f"Could not open folder: {exc}", "error")

    def _copy_cli_command(self):
        options = self._current_options()
        command = options.cli_command(
            "watch" if self.watcher and self.watcher.is_running else "download")
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.say(f"Copied to clipboard: {command}", "muted")

    def _save_preset(self):
        path = filedialog.asksaveasfilename(
            title="Save preset", parent=self.root, defaultextension=".json",
            filetypes=[("PyIncucyte preset", "*.json")])
        if not path:
            return
        self._current_options().save(path)
        self.say(f"Preset saved to {path}", "success")

    def _load_preset(self):
        path = filedialog.askopenfilename(
            title="Load preset", parent=self.root,
            filetypes=[("PyIncucyte preset", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            options = ExportOptions.load(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Bad preset", str(exc), parent=self.root)
            return
        self._apply_options(options)
        self.say(f"Loaded preset {Path(path).name}", "success")

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log", parent=self.root, defaultextension=".txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            Path(path).write_text(self.log.contents(), encoding="utf-8")
            self.say(f"Log saved to {path}", "success")

    def _toggle_dark(self):
        """Swap palettes and repaint the widgets ttk styles cannot reach."""
        self.theme.toggle_dark()
        self.root.configure(background=self.theme["bg"])
        self.plate.apply_theme(self.theme)
        self.log.apply_theme(self.theme)
        self.vessel_tree.tag_configure("odd",
                                       background=self.theme["surface_alt"])
        self._save_state()

    def _about(self):
        AboutDialog(self.root, self.theme, self.client).show()

    # ==================================================================
    # persistence
    # ==================================================================

    def _read_state(self):
        if GUI_STATE_FILE.exists():
            try:
                return self._migrate_state(
                    json.loads(GUI_STATE_FILE.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return {}

    @staticmethod
    def _migrate_state(state):
        """Upgrade a pre-0.2 gui_state.json so old installs keep their settings."""
        if not isinstance(state, dict) or "options" in state:
            return state or {}

        channels = [name for name, key in
                    (("phase", "phase"), ("green", "color1"), ("red", "color2"))
                    if state.get(key)]
        start = state.get("start_from", "Today")
        if start == "First scan":
            start_from = START_FIRST
        elif start.startswith("Custom") and state.get("custom_date"):
            start_from = state["custom_date"]
        else:
            start_from = START_TODAY

        migrated = {
            "host": state.get("host"),
            "dark": state.get("dark"),
            "geometry": state.get("geometry"),
            "recent_outputs": [state["output"]] if state.get("output") else [],
            "wells": state.get("wells", {}),
            "options": {
                "host": state.get("host"),
                "output": state.get("output", ""),
                "vessels": state.get("selected_vessels", []),
                "channels": ",".join(channels) if channels else "all",
                "layout": None,
                "hyperstack": bool(state.get("hyperstack")),
                "time_stack": bool(state.get("time_stack")),
                "green_lut": bool(state.get("green_phase")),
                "workers": state.get("max_workers", 4),
                "interval_minutes": state.get("interval", 10),
                "start_from": start_from,
            },
        }
        migrated["options"].pop("layout")
        return migrated

    def _apply_state(self, state):
        geometry = state.get("geometry")
        if geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass
        self.recent_outputs = [p for p in state.get("recent_outputs", [])
                               if isinstance(p, str)]
        self.output_combo.configure(values=self.recent_outputs)

        options_data = state.get("options")
        if options_data:
            try:
                self._apply_options(ExportOptions.from_dict(options_data))
            except (ValueError, TypeError):
                pass
        else:
            self._on_layout_change()
            self._on_window_change()

        for key, wells in (state.get("wells") or {}).items():
            try:
                self.selected_wells[int(key)] = (
                    None if wells is None else {(r, c) for r, c in wells})
            except (TypeError, ValueError):
                continue

        if state.get("host"):
            self.host_var.set(state["host"])
            self.client.host = state["host"]

    def _save_state(self):
        try:
            options = self._current_options().to_dict()
        except Exception:
            options = None
        wells = {}
        for vessel_id, selection in self.selected_wells.items():
            wells[str(vessel_id)] = (None if selection is None
                                     else [[r, c] for r, c in sorted(selection)])
        geometry = self.root.winfo_geometry()
        if geometry.startswith(("1x1", "200x200")):
            geometry = None      # window was never mapped; keep the old value
        payload = {
            "version": __version__,
            "geometry": geometry,
            "dark": self.theme.dark,
            "host": self.host_var.get().strip(),
            "recent_outputs": self.recent_outputs,
            "options": options,
            "wells": wells,
        }
        try:
            GUI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            GUI_STATE_FILE.write_text(json.dumps(payload, indent=2),
                                      encoding="utf-8")
        except OSError:
            pass

    def on_close(self):
        self._save_state()
        if self.watcher and self.watcher.is_running:
            self.watcher.stop()
        self.cancel_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)
        try:
            self.client.close()
        except Exception:
            pass
        self.root.destroy()


def _processing_value(text):
    """An empty option and the word shown for "off" mean the same thing."""
    value = str(text or "").strip()
    return "" if value.lower() in ("", PROCESSING_OFF) else value


def main():
    """Launch the desktop app."""
    theme_mod.enable_dpi_awareness()
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
