#!/usr/bin/env python3
"""
Incucyte Auto-Downloader GUI
=============================
Tkinter GUI wrapping incucyte_downloader.py for non-technical users.
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from datetime import datetime, date
from pathlib import Path

from incucyte_downloader import (
    authenticate, api_post, download_collected_scan_items,
    download_collected_time_stack_items,
    collect_scan_items_parallel, collect_time_stacks,
    collect_scans_in_range, count_time_stack_payloads,
    parse_wells, parse_channels, parse_scan_datetime, load_config,
    save_config, encrypt_password, get_token, DEFAULT_HOST,
    API_BASE_TEMPLATE, CONFIG_FILE, IMAGE_TYPE_MAP, IMAGE_TYPE_LABELS,
    IMAGE_TYPE_SHORT_LABELS,
    channel_name_from_channels, vessel_id_from_record, extract_search_vessels,
    load_state,
    save_state, APP_DIR,
)

GUI_STATE_FILE = APP_DIR / "gui_state.json"

PLATE_FORMATS = {
    6: (2, 3), 12: (3, 4), 24: (4, 6), 48: (6, 8),
    96: (8, 12), 384: (16, 24),
}


def guess_plate_size(vessel_type_name):
    """Parse well count from vessel type name like 'Sarstedt 24-well'."""
    import re
    m = re.search(r'(\d+)\s*-?\s*well', vessel_type_name, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n in PLATE_FORMATS:
            return PLATE_FORMATS[n]
    return PLATE_FORMATS[96]


def load_gui_state():
    if GUI_STATE_FILE.exists():
        try:
            return json.loads(GUI_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_gui_state(state):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    GUI_STATE_FILE.write_text(json.dumps(state, indent=2))


def channel_token(label):
    """Return a filename-safe channel token."""
    import re
    token = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return token or "channel"


class LoginDialog(tk.Toplevel):
    """Modal dialog for username/password login."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.title("Incucyte Login")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.result = None

        self.host = host

        frame = ttk.Frame(self, padding=15)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=f"Host: {host}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Username:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.username_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.username_var, width=30).grid(row=1, column=1)

        ttk.Label(frame, text="Password:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(5, 0))
        self.password_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.password_var, width=30, show="*").grid(row=2, column=1, pady=(5, 0))

        self.status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.status_var, foreground="red").grid(
            row=3, column=0, columnspan=2, pady=(8, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(12, 0))
        self.login_btn = ttk.Button(btn_frame, text="Login", command=self._do_login)
        self.login_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=4)

        self.bind("<Return>", lambda e: self._do_login())

        # Pre-fill username from config
        config = load_config()
        if config.get("username"):
            self.username_var.set(config["username"])

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _do_login(self):
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        if not username or not password:
            self.status_var.set("Enter username and password")
            return
        self.login_btn.config(state="disabled")
        self.status_var.set("Encrypting password...")
        self.update()
        threading.Thread(target=self._login_thread, args=(username, password), daemon=True).start()

    def _login_thread(self, username, password):
        try:
            encrypted = encrypt_password(password)
            self.after(0, lambda: self.status_var.set("Authenticating..."))
            token, expires_in = get_token(self.host, username, encrypted)
            from datetime import timedelta
            config = {
                "host": self.host,
                "username": username,
                "encrypted_password": encrypted,
                "token": token,
                "token_expires_at": (datetime.now().replace(microsecond=0) +
                                     timedelta(seconds=expires_in - 60)).isoformat(),
                "login_time": datetime.now().isoformat(),
            }
            save_config(config)
            self.result = config
            self.after(0, self.destroy)
        except Exception as e:
            self.after(0, lambda: self._login_failed(str(e)))

    def _login_failed(self, msg):
        self.status_var.set(msg[:80])
        self.login_btn.config(state="normal")


class ProgressDialog(tk.Toplevel):
    """Non-modal progress dialog for batch downloads."""

    def __init__(self, parent, total=0, stop_event=None, unit_label="files",
                 file_total=None, stage="Preparing download", detail=""):
        super().__init__(parent)
        self.title("Download Progress")
        self.resizable(False, False)
        self.total = max(0, int(total or 0))
        self.unit_label = unit_label
        self.file_total = file_total
        self.stop_event = stop_event
        self.completed = 0
        self._times = []  # list of per-file download durations
        self._last_time = None
        self._indeterminate = False

        frame = ttk.Frame(self, padding=15)
        frame.pack(fill="both", expand=True)

        self.stage_var = tk.StringVar(value=stage)
        ttk.Label(frame, textvariable=self.stage_var, font=("Segoe UI", 11, "bold")).pack(
            pady=(0, 6))

        count_text = f"0 / {self.total} {unit_label}" if self.total else f"Preparing {unit_label}..."
        self.count_var = tk.StringVar(value=count_text)
        ttk.Label(frame, textvariable=self.count_var, font=("Segoe UI", 11, "bold")).pack(pady=(0, 6))

        mode = "determinate" if self.total else "indeterminate"
        maximum = self.total if self.total else 100
        self.progress = ttk.Progressbar(frame, length=360, mode=mode, maximum=maximum)
        self.progress.pack(pady=(0, 6))
        if not self.total:
            self._indeterminate = True
            self.progress.start(12)

        self.pct_var = tk.StringVar(value="0%" if self.total else "Working...")
        ttk.Label(frame, textvariable=self.pct_var).pack()

        self.file_var = tk.StringVar(value=f"0 / {file_total} output files" if file_total else "")
        ttk.Label(frame, textvariable=self.file_var).pack(pady=(4, 0))

        self.current_var = tk.StringVar(value=detail)
        ttk.Label(frame, textvariable=self.current_var, wraplength=340).pack(pady=(6, 0))

        self.speed_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.speed_var).pack()

        self.remaining_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.remaining_var).pack()

        ttk.Button(frame, text="Cancel", command=self._cancel).pack(pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _cancel(self):
        if self.stop_event:
            self.stop_event.set()

    def _set_indeterminate(self, enabled):
        if enabled == self._indeterminate:
            return
        if enabled:
            self.progress.config(mode="indeterminate", maximum=100)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.config(mode="determinate")
        self._indeterminate = enabled

    def set_stage(self, stage=None, detail=None, total=None, unit_label=None,
                  file_total=None):
        if stage is not None:
            self.stage_var.set(stage)
        if unit_label is not None:
            self.unit_label = unit_label
        if file_total is not None:
            self.file_total = file_total
            self.file_var.set(f"0 / {file_total} output files" if file_total else "")
        if total is not None:
            self.total = max(0, int(total or 0))
            self.completed = 0
            self._times = []
            self._last_time = None
            if self.total:
                self._set_indeterminate(False)
                self.progress.config(maximum=self.total, value=0)
                self.count_var.set(f"0 / {self.total} {self.unit_label}")
                self.pct_var.set("0%")
            else:
                self._set_indeterminate(True)
                self.count_var.set(f"Preparing {self.unit_label}...")
                self.pct_var.set("Working...")
        if detail is not None:
            self.current_var.set(detail)

    def update_progress(self, fname, size, done, total, unit_label=None):
        """Called from worker thread via root.after()."""
        import time as _time
        if unit_label is not None:
            self.unit_label = unit_label
        self._set_indeterminate(False)
        now = _time.monotonic()
        if self._last_time is not None:
            self._times.append(now - self._last_time)
            # Rolling average of last 20 files
            if len(self._times) > 20:
                self._times = self._times[-20:]
        self._last_time = now

        self.completed = done
        self.total = total
        self.count_var.set(f"{done} / {total} {self.unit_label}")
        self.progress.config(maximum=total, value=done)
        pct = int(100 * done / total) if total else 0
        self.pct_var.set(f"{pct}%")
        self.current_var.set(f"Current: {fname}")

        if self._times:
            avg = sum(self._times) / len(self._times)
            unit = self.unit_label[:-1] if self.unit_label.endswith("s") else self.unit_label
            self.speed_var.set(f"Speed: ~{avg:.1f}s per {unit}")
            remaining = avg * (total - done)
            mins, secs = divmod(int(remaining), 60)
            self.remaining_var.set(f"Remaining: ~{mins}m {secs:02d}s")

    def update_file_progress(self, fname, size, done, total):
        self.file_total = total
        self.file_var.set(f"{done} / {total} output files")
        self.current_var.set(f"Completed: {fname}")

    def finish(self):
        try:
            self.destroy()
        except tk.TclError:
            pass


class ExportDialog(tk.Toplevel):
    """Pre-flight dialog for one-shot image exports."""

    FORMAT_OPTIONS = (
        ("separate", "Separate TIFFs", "One file per well, channel, and scan time"),
        ("hyperstack", "Channel hyperstack", "One ImageJ CYX file per well and scan time"),
        ("time", "Time stack", "One ImageJ TYX file per well and channel"),
        ("time_hyper", "Time + channel hyperstack", "One ImageJ TCYX file per well"),
    )

    def __init__(self, parent, app, vessel_ids, output):
        super().__init__(parent)
        self.app = app
        self.vessel_ids = vessel_ids
        self.result = None
        self.channel_labels = app._channel_label_map(vessel_ids)

        self.title("Export Images")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self.output_var = tk.StringVar(value=output)
        self.phase_var = tk.BooleanVar(value=app.ch_phase.get())
        self.color1_var = tk.BooleanVar(value=app.ch_color1.get())
        self.color2_var = tk.BooleanVar(value=app.ch_color2.get())
        self.green_phase_var = tk.BooleanVar(value=app.green_phase_var.get())
        self.workers_var = tk.IntVar(value=app.workers_var.get())
        self.start_from_var = tk.StringVar(value=app.start_from_var.get())
        self.custom_date_var = tk.StringVar(value=app.custom_date_var.get())

        if app.time_stack_var.get() and app.hyperstack_var.get():
            initial_format = "time_hyper"
        elif app.time_stack_var.get():
            initial_format = "time"
        elif app.hyperstack_var.get():
            initial_format = "hyperstack"
        else:
            initial_format = "separate"
        self.format_var = tk.StringVar(value=initial_format)

        self.mode_detail_var = tk.StringVar()
        self.source_detail_var = tk.StringVar()
        self.filename_var = tk.StringVar()

        self._build_ui()
        self._refresh_details()
        self._center(parent)

    def _build_ui(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Export Images", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")

        vessel_frame = ttk.LabelFrame(main, text="Selection", padding=8)
        vessel_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        cols = ("id", "name", "wells", "first", "last")
        self.vessel_tree = ttk.Treeview(
            vessel_frame, columns=cols, show="headings",
            height=max(1, min(5, len(self.vessel_ids))))
        for col, text, width in (
            ("id", "Vessel", 60),
            ("name", "Name", 190),
            ("wells", "Wells", 140),
            ("first", "First scan", 120),
            ("last", "Last scan", 120),
        ):
            self.vessel_tree.heading(col, text=text)
            self.vessel_tree.column(col, width=width, stretch=(col == "name"))
        self.vessel_tree.grid(row=0, column=0, sticky="ew")
        vessel_frame.columnconfigure(0, weight=1)
        self._populate_vessel_summary()

        output_frame = ttk.LabelFrame(main, text="Destination", padding=8)
        output_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Entry(output_frame, textvariable=self.output_var, width=72).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(output_frame, text="Browse...", command=self._browse_output).grid(row=0, column=1)
        output_frame.columnconfigure(0, weight=1)

        left = ttk.LabelFrame(main, text="Channels", padding=8)
        left.grid(row=3, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        ttk.Checkbutton(left, text="Phase", variable=self.phase_var,
                        command=self._refresh_details).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(left, text=f"{self.channel_labels[2]} (Color 1)", variable=self.color1_var,
                        command=self._refresh_details).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(left, text=f"{self.channel_labels[3]} (Color 2)", variable=self.color2_var,
                        command=self._refresh_details).grid(row=2, column=0, sticky="w")

        ttk.Label(left, text="Start from:").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.start_combo = ttk.Combobox(
            left, textvariable=self.start_from_var,
            values=["First scan", "Today", "Custom date..."],
            state="readonly", width=16)
        self.start_combo.grid(row=4, column=0, sticky="w", pady=(2, 0))
        self.start_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_details())
        self.custom_date_entry = ttk.Entry(left, textvariable=self.custom_date_var, width=14)
        self.custom_date_entry.grid(row=5, column=0, sticky="w", pady=(4, 0))

        right = ttk.LabelFrame(main, text="Export Format", padding=8)
        right.grid(row=3, column=1, sticky="nsew", pady=(0, 6))
        for idx, (value, label, desc) in enumerate(self.FORMAT_OPTIONS):
            ttk.Radiobutton(
                right, text=label, variable=self.format_var, value=value,
                command=self._refresh_details).grid(row=idx * 2, column=0, sticky="w")
            ttk.Label(right, text=desc, foreground="#555555").grid(
                row=idx * 2 + 1, column=0, sticky="w", padx=(20, 0), pady=(0, 2))

        settings = ttk.Frame(right)
        settings.grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.green_check = ttk.Checkbutton(
            settings, text="Apply green LUT to Phase", variable=self.green_phase_var,
            command=self._refresh_details)
        self.green_check.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(settings, text="Workers:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(settings, from_=1, to=16, textvariable=self.workers_var, width=5).grid(
            row=1, column=1, sticky="w", padx=(4, 0), pady=(6, 0))

        detail_frame = ttk.LabelFrame(main, text="What Will Be Exported", padding=8)
        detail_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(detail_frame, textvariable=self.source_detail_var, wraplength=680).grid(
            row=0, column=0, sticky="w")
        ttk.Label(detail_frame, textvariable=self.mode_detail_var, wraplength=680).grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(detail_frame, textvariable=self.filename_var, wraplength=680).grid(
            row=2, column=0, sticky="w", pady=(4, 0))

        btn_row = ttk.Frame(main)
        btn_row.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Download", command=self._accept).pack(side="left")

        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Return>", lambda e: self._accept())
        self.bind("<Escape>", lambda e: self.destroy())

    def _populate_vessel_summary(self):
        for vid in self.vessel_ids:
            vessel = self.app._find_vessel(vid) or {}
            doc = vessel.get("VesselDocumentation", {})
            first_scan = self._format_scan(vessel.get("FirstScanDateTime", ""))
            last_scan = self._format_scan(vessel.get("LastScanDateTime", ""))
            self.vessel_tree.insert("", "end", values=(
                vid, doc.get("Label", ""), self._format_wells(vid), first_scan, last_scan))

    def _format_scan(self, value):
        if not value:
            return ""
        try:
            return parse_scan_datetime(value).strftime("%d/%m/%Y %H:%M")
        except Exception:
            return str(value)

    def _format_wells(self, vessel_id):
        wells = self.app.selected_wells.get(vessel_id)
        if wells is None:
            return "All wells"
        if not wells:
            return "No wells"
        names = [f"{chr(65 + r)}{c + 1}" for r, c in sorted(wells)]
        if len(names) <= 6:
            return ", ".join(names)
        return f"{len(names)} wells ({', '.join(names[:4])}, ...)"

    def _selected_channel_names(self):
        names = []
        if self.phase_var.get():
            names.append("phase")
        if self.color1_var.get():
            names.append(channel_token(self.channel_labels[2]))
        if self.color2_var.get():
            names.append(channel_token(self.channel_labels[3]))
        return names or [
            channel_token(self.channel_labels[1]),
            channel_token(self.channel_labels[2]),
            channel_token(self.channel_labels[3]),
        ]

    def _example_well(self):
        for vid in self.vessel_ids:
            wells = self.app.selected_wells.get(vid)
            if wells:
                row, col = sorted(wells)[0]
                return vid, f"{chr(65 + row)}{col + 1}"
            return vid, "A1"
        return "VID", "A1"

    def _refresh_details(self):
        fmt = self.format_var.get()
        stack_mode = fmt in ("hyperstack", "time", "time_hyper")
        if stack_mode:
            self.green_phase_var.set(False)
            self.green_check.config(state="disabled")
        else:
            self.green_check.config(state="normal")

        if self.start_from_var.get() == "Custom date...":
            self.custom_date_entry.config(state="normal")
        else:
            self.custom_date_entry.config(state="disabled")

        self.source_detail_var.set(
            "Source: Incucyte stored/source TIFF payloads. Phase is original 8-bit; "
            f"{self.channel_labels[2]} (Color 1) and {self.channel_labels[3]} (Color 2) "
            "are uncalibrated 16-bit. These are not calibrated or display-rendered exports."
        )
        details = {
            "separate": "Separate TIFFs: one YX image per selected well, channel, and scan time.",
            "hyperstack": "Channel hyperstack: ImageJ CYX stack per well/site/scan time.",
            "time": "Time stack: ImageJ TYX stack per well/site/channel across selected scans.",
            "time_hyper": "Time + channel hyperstack: ImageJ TCYX stack per well/site across selected scans.",
        }
        self.mode_detail_var.set(details[fmt] + " Missing-vessel scan times are skipped.")

        vid, well = self._example_well()
        channels = self._selected_channel_names()
        if fmt == "separate":
            example = f"VID{vid}_{well}_1_00d00h00m.tif"
        elif fmt == "hyperstack":
            example = f"VID{vid}_{well}_{'-'.join(channels)}_00d00h00m.tif"
        elif fmt == "time":
            example = f"VID{vid}_{well}_{channels[0]}_timestack.tif"
        else:
            example = f"VID{vid}_{well}_{'-'.join(channels)}_timestack.tif"
        self.filename_var.set(f"Example filename: {example}")

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Select output folder", parent=self)
        if folder:
            self.output_var.set(folder)

    def _accept(self):
        output = self.output_var.get().strip()
        if not output:
            messagebox.showwarning("No output folder", "Choose an output folder.", parent=self)
            return
        try:
            workers = int(self.workers_var.get())
        except (TypeError, ValueError, tk.TclError):
            messagebox.showwarning("Invalid workers", "Workers must be a number.", parent=self)
            return
        workers = max(1, min(16, workers))

        fmt = self.format_var.get()
        self.result = {
            "output": output,
            "phase": self.phase_var.get(),
            "color1": self.color1_var.get(),
            "color2": self.color2_var.get(),
            "green_phase": self.green_phase_var.get() if fmt == "separate" else False,
            "hyperstack": fmt in ("hyperstack", "time_hyper"),
            "time_stack": fmt in ("time", "time_hyper"),
            "workers": workers,
            "start_from": self.start_from_var.get(),
            "custom_date": self.custom_date_var.get().strip(),
        }
        self.destroy()

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Incucyte Auto-Downloader")
        self.root.minsize(720, 700)

        self.msg_queue = queue.Queue()
        self.watching = False
        self.download_active = False
        self.watch_thread = None
        self.stop_event = threading.Event()
        self.host = DEFAULT_HOST
        self.token = None
        self.vessels = []
        self.selected_wells = {}  # vessel_id -> set of (row, col)
        self.progress_dialog = None

        self._build_ui()
        self._load_state()
        self._poll_queue()
        self._try_auto_connect()

    def _find_vessel(self, vessel_id):
        try:
            target_id = int(vessel_id)
        except (TypeError, ValueError):
            return None
        for vessel in self.vessels:
            if vessel_id_from_record(vessel) == target_id:
                return vessel
        return None

    def _channel_label_map(self, vessel_ids=None):
        labels = dict(IMAGE_TYPE_LABELS)
        selected = {str(v) for v in (vessel_ids or [])}
        for vessel in self.vessels:
            if not isinstance(vessel, dict):
                continue
            vid = vessel_id_from_record(vessel)
            if selected and str(vid) not in selected:
                continue
            channels = vessel.get("Channels", {}) or {}
            labels[2] = channel_name_from_channels(channels, 2)
            labels[3] = channel_name_from_channels(channels, 3)
            break
        return labels

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # --- Connection ---
        conn_frame = ttk.LabelFrame(main, text="Connection", padding=8)
        conn_frame.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(conn_frame)
        row.pack(fill="x")
        ttk.Label(row, text="Host:").pack(side="left")
        self.host_var = tk.StringVar(value=self.host)
        ttk.Entry(row, textvariable=self.host_var, width=20).pack(side="left", padx=(4, 12))
        self.conn_status_var = tk.StringVar(value="Not connected")
        ttk.Label(row, textvariable=self.conn_status_var).pack(side="left", padx=(0, 12))
        ttk.Button(row, text="Login...", command=self._login).pack(side="right")

        # --- Vessels ---
        vessel_frame = ttk.LabelFrame(main, text="Vessels", padding=8)
        vessel_frame.pack(fill="x", pady=(0, 6))

        btn_row = ttk.Frame(vessel_frame)
        btn_row.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_row, text="Refresh", command=self._refresh_vessels).pack(side="right")

        cols = ("id", "name", "owner", "last_scan", "scan_type", "channels")
        self.vessel_tree = ttk.Treeview(vessel_frame, columns=cols, show="headings", height=6, selectmode="extended")
        self.vessel_tree.heading("id", text="Vessel ID")
        self.vessel_tree.heading("name", text="Vessel Name")
        self.vessel_tree.heading("owner", text="Owner")
        self.vessel_tree.heading("last_scan", text="Last Scan")
        self.vessel_tree.heading("scan_type", text="Scan Type")
        self.vessel_tree.heading("channels", text="Channels")
        self.vessel_tree.column("id", width=60, stretch=False)
        self.vessel_tree.column("name", width=280)
        self.vessel_tree.column("owner", width=80)
        self.vessel_tree.column("last_scan", width=130)
        self.vessel_tree.column("scan_type", width=80)
        self.vessel_tree.column("channels", width=90)
        self.vessel_tree.pack(fill="x")
        self.vessel_tree.bind("<<TreeviewSelect>>", self._on_vessel_select)

        # --- Download Settings ---
        settings_frame = ttk.LabelFrame(main, text="Download Settings", padding=8)
        settings_frame.pack(fill="x", pady=(0, 6))

        folder_row = ttk.Frame(settings_frame)
        folder_row.pack(fill="x", pady=(0, 4))
        ttk.Label(folder_row, text="Output folder:").pack(side="left")
        self.output_var = tk.StringVar()
        ttk.Entry(folder_row, textvariable=self.output_var, width=50).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(folder_row, text="Browse...", command=self._browse_folder).pack(side="right")

        ch_row = ttk.Frame(settings_frame)
        ch_row.pack(fill="x", pady=(0, 4))
        ttk.Label(ch_row, text="Channels:").pack(side="left")
        self.ch_phase = tk.BooleanVar(value=True)
        self.ch_color1 = tk.BooleanVar(value=False)
        self.ch_color2 = tk.BooleanVar(value=False)
        ttk.Checkbutton(ch_row, text="Phase", variable=self.ch_phase).pack(side="left", padx=4)
        ttk.Checkbutton(ch_row, text="Green (Color 1)", variable=self.ch_color1).pack(side="left", padx=4)
        ttk.Checkbutton(ch_row, text="Red (Color 2)", variable=self.ch_color2).pack(side="left", padx=4)

        opt_row = ttk.Frame(settings_frame)
        opt_row.pack(fill="x", pady=(0, 4))
        self.green_phase_var = tk.BooleanVar(value=False)
        self.green_phase_check = ttk.Checkbutton(
            opt_row, text="Apply green LUT to Phase", variable=self.green_phase_var)
        self.green_phase_check.pack(side="left")
        self.hyperstack_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_row, text="ImageJ hyperstack", variable=self.hyperstack_var,
            command=self._on_stack_option_toggle).pack(side="left", padx=(12, 0))
        self.time_stack_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_row, text="Time stack", variable=self.time_stack_var,
            command=self._on_stack_option_toggle).pack(side="left", padx=(12, 0))

        interval_row = ttk.Frame(settings_frame)
        interval_row.pack(fill="x")
        ttk.Label(interval_row, text="Poll interval:").pack(side="left")
        self.interval_var = tk.IntVar(value=10)
        ttk.Spinbox(interval_row, from_=1, to=120, textvariable=self.interval_var, width=5).pack(side="left", padx=4)
        ttk.Label(interval_row, text="minutes").pack(side="left")

        ttk.Label(interval_row, text="    Workers:").pack(side="left")
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(interval_row, from_=1, to=16, textvariable=self.workers_var, width=4).pack(side="left", padx=4)

        start_row = ttk.Frame(settings_frame)
        start_row.pack(fill="x", pady=(4, 0))
        ttk.Label(start_row, text="Start from:").pack(side="left")
        self.start_from_var = tk.StringVar(value="Today")
        self.start_from_combo = ttk.Combobox(start_row, textvariable=self.start_from_var,
                                              values=["First scan", "Today", "Custom date..."],
                                              state="readonly", width=16)
        self.start_from_combo.pack(side="left", padx=4)
        self.start_from_combo.bind("<<ComboboxSelected>>", self._on_start_from_change)

        self.custom_date_var = tk.StringVar()
        self.custom_date_entry = ttk.Entry(start_row, textvariable=self.custom_date_var,
                                            width=12)
        self.custom_date_label = ttk.Label(start_row, text="(YYYY-MM-DD)")
        # Hidden by default — shown when "Custom date..." selected

        # --- Well Selection ---
        self.well_frame = ttk.LabelFrame(main, text="Well Selection", padding=8)
        self.well_frame.pack(fill="x", pady=(0, 6))
        self.well_inner = ttk.Frame(self.well_frame)
        self.well_inner.pack(fill="x")
        self.well_buttons = {}
        self.well_grid_vessel = None
        ttk.Label(self.well_inner, text="Select a vessel above to show wells").pack()

        well_btn_row = ttk.Frame(self.well_frame)
        well_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(well_btn_row, text="Select All", command=self._wells_select_all).pack(side="left", padx=2)
        ttk.Button(well_btn_row, text="Clear All", command=self._wells_clear_all).pack(side="left", padx=2)

        # --- Controls ---
        ctrl_frame = ttk.LabelFrame(main, text="Controls", padding=8)
        ctrl_frame.pack(fill="x", pady=(0, 6))
        ctrl_row = ttk.Frame(ctrl_frame)
        ctrl_row.pack()
        self.watch_btn = ttk.Button(ctrl_row, text="Start Watching", command=self._start_watching)
        self.watch_btn.pack(side="left", padx=4)
        self.download_btn = ttk.Button(ctrl_row, text="Download Now", command=self._download_now)
        self.download_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctrl_row, text="Stop", command=self._stop_watching, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        # --- Log ---
        log_frame = ttk.LabelFrame(main, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        log_btn_row = ttk.Frame(log_frame)
        log_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(log_btn_row, text="Clear Log", command=self._clear_log).pack(side="left")
        ttk.Button(log_btn_row, text="Save Log", command=self._save_log).pack(side="right")

    def _log(self, msg):
        """Thread-safe log message."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.msg_queue.put(f"{timestamp}  {msg}")

    def _poll_queue(self):
        """Drain the message queue and append to the log widget."""
        while True:
            try:
                msg = self.msg_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(100, self._poll_queue)

    def _try_auto_connect(self):
        """If saved credentials exist and token is valid, auto-connect."""
        config = load_config()
        if config.get("token") and config.get("token_expires_at"):
            try:
                expires = datetime.fromisoformat(config["token_expires_at"])
                if datetime.now() < expires:
                    self.host = config.get("host", DEFAULT_HOST)
                    self.host_var.set(self.host)
                    self.token = config["token"]
                    remaining = expires - datetime.now()
                    hours = remaining.total_seconds() / 3600
                    self.conn_status_var.set(f"Connected as {config.get('username', '?')} (token expires in {hours:.1f}h)")
                    self._log("Auto-connected with saved credentials")
                    self._refresh_vessels()
                    return
            except Exception:
                pass
        self.conn_status_var.set("Not connected")

    def _login(self):
        self.host = self.host_var.get().strip() or DEFAULT_HOST
        dlg = LoginDialog(self.root, self.host)
        self.root.wait_window(dlg)
        if dlg.result:
            self.token = dlg.result["token"]
            self.conn_status_var.set(f"Connected as {dlg.result['username']}")
            self._log("Login successful")
            self._refresh_vessels()

    def _refresh_vessels(self):
        if not self.token:
            self._log("Not connected. Login first.")
            return
        self._log("Fetching vessels...")
        threading.Thread(target=self._fetch_vessels_thread, daemon=True).start()

    def _fetch_vessels_thread(self):
        try:
            self.host, self.token = self._re_auth()
            data = api_post(self.host, self.token, "Vessels/GetAllSearchVessels")
            self.vessels = extract_search_vessels(data)
            self.root.after(0, self._populate_vessels)
        except Exception as e:
            self._log(f"Error fetching vessels: {e}")

    def _re_auth(self):
        """Re-authenticate using saved config (token refresh)."""
        config = load_config()
        host = config.get("host", DEFAULT_HOST)

        if config.get("token") and config.get("token_expires_at"):
            try:
                expires = datetime.fromisoformat(config["token_expires_at"])
                if datetime.now() < expires:
                    return host, config["token"]
            except Exception:
                pass

        username = config.get("username")
        encrypted_pw = config.get("encrypted_password")
        if not username or not encrypted_pw:
            raise RuntimeError("Not logged in")

        from datetime import timedelta
        token, expires_in = get_token(host, username, encrypted_pw)
        config["token"] = token
        config["token_expires_at"] = (datetime.now().replace(microsecond=0) +
                                       timedelta(seconds=expires_in - 60)).isoformat()
        save_config(config)
        self.token = token
        return host, token

    def _populate_vessels(self):
        self.vessel_tree.delete(*self.vessel_tree.get_children())
        inserted = 0
        skipped = 0
        seen = set()
        for v in self.vessels:
            if not isinstance(v, dict):
                skipped += 1
                continue
            vid = vessel_id_from_record(v)
            if vid is None:
                skipped += 1
                continue
            if vid in seen:
                skipped += 1
                continue
            seen.add(vid)

            doc = v.get("VesselDocumentation", {}) or {}
            vname = doc.get("Label", "")
            owner = doc.get("UserName", "")
            last_scan = v.get("LastScanDateTime", "")
            if last_scan:
                try:
                    dt = datetime.fromisoformat(last_scan.split("+")[0].split("Z")[0])
                    last_scan = dt.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    pass
            scan_type = v.get("ScanTypeDisplayText", "")
            channels = v.get("Channels", {}) or {}
            phase = "Ph" if channels.get("Phase", {}).get("On") else ""
            colors = channels.get("Colors", {}) or {}
            c1_state = colors.get("Color1", {}) or {}
            c2_state = colors.get("Color2", {}) or {}
            c1 = channel_name_from_channels(channels, 2) if c1_state.get("On") else ""
            c2 = channel_name_from_channels(channels, 3) if c2_state.get("On") else ""
            ch_str = "+".join(filter(None, [phase, c1, c2]))
            self.vessel_tree.insert("", "end", iid=str(vid),
                                    values=(vid, vname, owner, last_scan, scan_type, ch_str))
            inserted += 1

        msg = f"Found {inserted} vessels"
        if skipped:
            msg += f" ({skipped} invalid/duplicate records skipped)"
        self._log(msg)

    def _on_vessel_select(self, event=None):
        sel = self.vessel_tree.selection()
        if not sel:
            return
        for item_id in sel:
            try:
                vid = int(item_id)
            except (TypeError, ValueError):
                continue
            vessel = self._find_vessel(vid)
            if vessel:
                self._build_well_grid(vid, vessel)
                return

    def _build_well_grid(self, vessel_id, vessel):
        vtype = vessel.get("VesselTypeName", "")
        rows, cols = guess_plate_size(vtype)

        # Destroy old grid
        for w in self.well_inner.winfo_children():
            w.destroy()
        self.well_buttons = {}
        self.well_grid_vessel = vessel_id
        self.well_grid_rows = rows
        self.well_grid_cols = cols

        self.well_frame.config(text=f"Well Selection (Vessel {vessel_id} - {vtype})")

        # Restore previously selected wells for this vessel
        saved = self.selected_wells.get(vessel_id, None)

        # Column headers
        ttk.Label(self.well_inner, text="", width=3).grid(row=0, column=0)
        for c in range(cols):
            lbl = ttk.Label(self.well_inner, text=str(c + 1), width=4, anchor="center", cursor="hand2")
            lbl.grid(row=0, column=c + 1)
            lbl.bind("<Button-1>", lambda e, col=c: self._toggle_column(col))

        # Row headers + well buttons
        for r in range(rows):
            row_letter = chr(65 + r)
            lbl = ttk.Label(self.well_inner, text=row_letter, width=3, anchor="center", cursor="hand2")
            lbl.grid(row=r + 1, column=0)
            lbl.bind("<Button-1>", lambda e, row=r: self._toggle_row(row))

            for c in range(cols):
                is_selected = saved is None or (r, c) in saved  # default: all selected if no saved state
                # Actually default to all selected only if no saved state at all
                if saved is None:
                    is_selected = True

                btn = tk.Button(
                    self.well_inner, width=3, height=1,
                    bg="#4CAF50" if is_selected else "#E0E0E0",
                    activebackground="#66BB6A" if is_selected else "#BDBDBD",
                    relief="flat", bd=1,
                )
                btn.grid(row=r + 1, column=c + 1, padx=1, pady=1)
                btn.bind("<Button-1>", lambda e, row=r, col=c: self._toggle_well(row, col))
                btn.bind("<B1-Motion>", lambda e, row=r, col=c: self._drag_well(e))
                self.well_buttons[(r, c)] = {"btn": btn, "selected": is_selected}

        # Save initial state if none existed
        if saved is None:
            self.selected_wells[vessel_id] = {(r, c) for r in range(rows) for c in range(cols)}

    def _toggle_well(self, row, col):
        info = self.well_buttons.get((row, col))
        if not info:
            return
        info["selected"] = not info["selected"]
        self._update_well_color(row, col)
        self._sync_well_state()

    def _drag_well(self, event):
        widget = event.widget.winfo_containing(event.x_root, event.y_root)
        if widget:
            for (r, c), info in self.well_buttons.items():
                if info["btn"] is widget and not info["selected"]:
                    info["selected"] = True
                    self._update_well_color(r, c)
                    self._sync_well_state()
                    break

    def _update_well_color(self, row, col):
        info = self.well_buttons.get((row, col))
        if not info:
            return
        color = "#4CAF50" if info["selected"] else "#E0E0E0"
        info["btn"].config(bg=color, activebackground="#66BB6A" if info["selected"] else "#BDBDBD")

    def _toggle_row(self, row):
        # If all selected, deselect all; otherwise select all
        row_wells = [(row, c) for c in range(self.well_grid_cols)]
        all_selected = all(self.well_buttons[(r, c)]["selected"] for r, c in row_wells if (r, c) in self.well_buttons)
        for r, c in row_wells:
            if (r, c) in self.well_buttons:
                self.well_buttons[(r, c)]["selected"] = not all_selected
                self._update_well_color(r, c)
        self._sync_well_state()

    def _toggle_column(self, col):
        col_wells = [(r, col) for r in range(self.well_grid_rows)]
        all_selected = all(self.well_buttons[(r, c)]["selected"] for r, c in col_wells if (r, c) in self.well_buttons)
        for r, c in col_wells:
            if (r, c) in self.well_buttons:
                self.well_buttons[(r, c)]["selected"] = not all_selected
                self._update_well_color(r, c)
        self._sync_well_state()

    def _wells_select_all(self):
        for (r, c), info in self.well_buttons.items():
            info["selected"] = True
            self._update_well_color(r, c)
        self._sync_well_state()

    def _wells_clear_all(self):
        for (r, c), info in self.well_buttons.items():
            info["selected"] = False
            self._update_well_color(r, c)
        self._sync_well_state()

    def _sync_well_state(self):
        """Update self.selected_wells from the current button states."""
        if self.well_grid_vessel is None:
            return
        selected = set()
        for (r, c), info in self.well_buttons.items():
            if info["selected"]:
                selected.add((r, c))
        self.selected_wells[self.well_grid_vessel] = selected

    def _on_start_from_change(self, event=None):
        choice = self.start_from_var.get()
        if choice == "Custom date...":
            self.custom_date_entry.pack(side="left", padx=4)
            self.custom_date_label.pack(side="left")
        else:
            self.custom_date_entry.pack_forget()
            self.custom_date_label.pack_forget()

    def _resolve_start_date(self, vessel_ids):
        """Resolve the start date from the GUI setting. Returns a date object."""
        choice = self.start_from_var.get()
        if choice == "First scan":
            # Use earliest FirstScanDateTime from selected vessels
            earliest = None
            for vid in vessel_ids:
                vessel = self._find_vessel(vid)
                if vessel and vessel.get("FirstScanDateTime"):
                    try:
                        dt = parse_scan_datetime(vessel["FirstScanDateTime"])
                        if earliest is None or dt < earliest:
                            earliest = dt
                    except Exception:
                        pass
            if earliest:
                return earliest.date()
            self._log("Could not find first scan date, using today")
            return date.today()
        elif choice == "Custom date...":
            custom = self.custom_date_var.get().strip()
            if custom:
                try:
                    return datetime.strptime(custom, "%Y-%m-%d").date()
                except ValueError:
                    self._log(f"Invalid date '{custom}', using today")
            return date.today()
        else:  # "Today"
            return date.today()

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_var.set(folder)

    def _get_selected_channels(self):
        channels = set()
        if self.ch_phase.get():
            channels.add(1)
        if self.ch_color1.get():
            channels.add(2)
        if self.ch_color2.get():
            channels.add(3)
        return channels if channels else None

    def _on_stack_option_toggle(self):
        """Disable the Phase LUT option when writing raw stack exports."""
        if self.hyperstack_var.get() or self.time_stack_var.get():
            self.green_phase_var.set(False)
            self.green_phase_check.config(state="disabled")
        else:
            self.green_phase_check.config(state="normal")

    def _get_selected_vessels(self):
        """Return list of selected vessel IDs from the treeview."""
        sel = self.vessel_tree.selection()
        if not sel:
            return []
        vessel_ids = []
        for item_id in sel:
            try:
                vessel_ids.append(int(item_id))
            except (TypeError, ValueError):
                continue
        return vessel_ids

    def _validate_for_download(self):
        vessel_ids = self._get_selected_vessels()
        if not vessel_ids:
            messagebox.showwarning("No vessels", "Select at least one vessel.")
            return None
        output = self.output_var.get().strip()
        if not output:
            messagebox.showwarning("No output folder", "Choose an output folder.")
            return None
        if not self.token:
            messagebox.showwarning("Not connected", "Login first.")
            return None
        return vessel_ids, output

    def _set_download_active(self, active):
        self.download_active = active
        state = "disabled" if active else "normal"
        self.download_btn.config(state=state)
        self.watch_btn.config(state=state)
        self.stop_btn.config(state="normal" if active else "disabled")
        if not active:
            self.watching = False
            self.root.title("Incucyte Auto-Downloader")

    def _finish_download_thread(self):
        self._hide_progress()
        self._set_download_active(False)

    def _filter_scans_for_vessel(self, scans, reference_time):
        if reference_time is None:
            return scans
        filtered = []
        for scan_time in scans:
            try:
                if parse_scan_datetime(scan_time) >= reference_time:
                    filtered.append(scan_time)
            except Exception:
                filtered.append(scan_time)
        return filtered

    def _log_collection_progress(self, vessel_id, scan_time, done, total):
        self.root.after(0, lambda d=done, t=total, vid=vessel_id:
            self._update_task_progress(
                "Building export list",
                f"Vessel {vid}: {d}/{t} scan times checked",
                d, t, "scan times"))
        if not total:
            return
        if done == 1 or done == total or done % 10 == 0:
            self._log(f"Collecting vessel {vessel_id}: {done}/{total} scan times checked")
            self.root.after(0, lambda d=done, t=total:
                self.root.title(f"Incucyte - Collecting {d}/{t}"))

    def _log_scan_progress(self, scan_day, done, total):
        self.root.after(0, lambda d=done, t=total, day=scan_day:
            self._update_task_progress(
                "Scanning for scans",
                f"Checking {day.isoformat()}",
                d, t, "days"))

    def _apply_export_settings(self, settings):
        """Apply settings chosen in the one-shot export dialog."""
        self.output_var.set(settings["output"])
        self.ch_phase.set(settings["phase"])
        self.ch_color1.set(settings["color1"])
        self.ch_color2.set(settings["color2"])
        self.green_phase_var.set(settings["green_phase"])
        self.hyperstack_var.set(settings["hyperstack"])
        self.time_stack_var.set(settings["time_stack"])
        self.workers_var.set(settings["workers"])
        self.start_from_var.set(settings["start_from"])
        self.custom_date_var.set(settings["custom_date"])
        self._on_start_from_change()
        self._on_stack_option_toggle()

    def _download_now(self):
        if self.download_active:
            messagebox.showinfo("Download running", "A download is already running.")
            return
        result = self._validate_for_download()
        if not result:
            return
        vessel_ids, output = result

        dlg = ExportDialog(self.root, self, vessel_ids, output)
        self.root.wait_window(dlg)
        if not dlg.result:
            self._log("Download cancelled")
            return

        self._apply_export_settings(dlg.result)
        output = dlg.result["output"]
        self._save_state()
        self.stop_event.clear()
        self._set_download_active(True)
        self._log("Starting one-shot download...")
        self._show_progress(
            0, unit_label="steps", stage="Preparing download",
            detail="Starting one-shot download...")
        threading.Thread(target=self._download_thread, args=(vessel_ids, output, False), daemon=True).start()

    def _start_watching(self):
        if self.download_active:
            messagebox.showinfo("Download running", "A download is already running.")
            return
        result = self._validate_for_download()
        if not result:
            return
        vessel_ids, output = result
        self._save_state()
        self.watching = True
        self.stop_event.clear()
        self._set_download_active(True)
        self._log("Watch mode started")
        self._show_progress(
            0, unit_label="steps", stage="Preparing watch mode",
            detail="Checking for new scans...")
        self.watch_thread = threading.Thread(
            target=self._download_thread, args=(vessel_ids, output, True), daemon=True)
        self.watch_thread.start()

    def _stop_watching(self):
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        self._log("Stopping...")

    def _download_thread(self, vessel_ids, output_path, watch_mode):
        output = Path(output_path)
        output.mkdir(parents=True, exist_ok=True)
        channels = self._get_selected_channels()
        state = load_state()
        max_workers = self.workers_var.get()
        green_phase = self.green_phase_var.get()
        hyperstack = self.hyperstack_var.get()
        time_stack = self.time_stack_var.get()

        # Resolve start date from GUI setting
        start_date = self._resolve_start_date(vessel_ids)
        self._log(f"Scanning from {start_date} to today")
        self.root.after(0, lambda sd=start_date: self._set_progress_stage(
            "Preparing download", f"Scanning from {sd} to today", unit_label="days"))

        # Get FirstScanDateTime from vessel data for elapsed-time filenames
        reference_times = {}
        for vid in vessel_ids:
            vessel = self._find_vessel(vid)
            if vessel and vessel.get("FirstScanDateTime"):
                try:
                    reference_times[vid] = parse_scan_datetime(vessel["FirstScanDateTime"])
                    self._log(f"Vessel {vid}: first scan {reference_times[vid]}")
                except Exception:
                    pass

        while True:
            try:
                host, token = self._re_auth()
                self.token = token

                now = datetime.now()
                self._log("Checking for new scans...")
                self.root.after(0, lambda: self._set_progress_stage(
                    "Scanning for scans",
                    f"Checking dates from {start_date} to {now.date()}",
                    unit_label="days"))

                scans = collect_scans_in_range(
                    host, token, start_date, now.date(),
                    progress_callback=self._log_scan_progress,
                    stop_event=self.stop_event)
                self._log(f"Found {len(scans)} scan times; building export list...")

                if not scans:
                    self._log("No scans found.")
                else:
                    # Collect all images first for progress tracking
                    all_items = []
                    if time_stack:
                        for vid in vessel_ids:
                            if self.stop_event.is_set():
                                break
                            wells = self.selected_wells.get(vid)
                            vessel_scans = self._filter_scans_for_vessel(
                                scans, reference_times.get(vid))
                            self._log(f"Collecting vessel {vid}: {len(vessel_scans)} scan times")
                            items = collect_time_stacks(
                                host, token, vid, vessel_scans, output,
                                state, wells=wells, channels=channels,
                                reference_time=reference_times.get(vid),
                                channel_hyperstack=hyperstack,
                                progress_callback=self._log_collection_progress,
                                stop_event=self.stop_event,
                                max_workers=max_workers)
                            all_items.extend(items)
                    else:
                        for vid in vessel_ids:
                            vessel_scans = self._filter_scans_for_vessel(
                                scans, reference_times.get(vid))
                            self._log(
                                f"Collecting vessel {vid}: {len(vessel_scans)} scan times "
                                f"with up to {max_workers} workers")
                            wells = self.selected_wells.get(vid)
                            items = collect_scan_items_parallel(
                                host, token, vid, vessel_scans, output,
                                state=state, wells=wells, channels=channels,
                                reference_time=reference_times.get(vid),
                                hyperstack=hyperstack,
                                max_workers=max_workers,
                                progress_callback=self._log_collection_progress,
                                stop_event=self.stop_event)
                            all_items.extend(items)
                            if self.stop_event.is_set():
                                break

                    if all_items:
                        total = len(all_items)
                        if time_stack:
                            payload_total = count_time_stack_payloads(all_items)
                            self._log(f"Found {total} output files using {payload_total} source images")
                        else:
                            payload_total = total
                            self._log(f"Found {total} new files to download")

                        if time_stack:
                            self.root.after(
                                0,
                                lambda p=payload_total, f=total: self._show_progress(
                                    p, unit_label="source images", file_total=f,
                                    stage="Downloading source images",
                                    detail="Downloading frames and writing stack files..."))
                        else:
                            self.root.after(
                                0,
                                lambda t=total: self._show_progress(
                                    t, unit_label="files",
                                    stage="Downloading files",
                                    detail="Starting file transfers..."))

                        def on_progress(fname, size, done, total_count):
                            self.root.after(0, lambda: self._update_progress(fname, size, done, total_count))

                        def on_file_progress(fname, size, done, total_count):
                            self.root.after(0, lambda: self._update_file_progress(fname, size, done, total_count))

                        # Download with parallel workers
                        new_count = 0
                        if time_stack:
                            new_count = download_collected_time_stack_items(
                                host, token, all_items,
                                state=state, max_workers=max_workers,
                                progress_callback=on_file_progress,
                                unit_progress_callback=on_progress,
                                stop_event=self.stop_event)
                            if new_count:
                                self._log(f"Downloaded {new_count} files")
                        else:
                            new_count = download_collected_scan_items(
                                host, token, all_items,
                                state=state, max_workers=max_workers,
                                green_phase=green_phase,
                                hyperstack=hyperstack,
                                progress_callback=on_progress,
                                stop_event=self.stop_event)

                        self.root.after(0, self._hide_progress)

                        if new_count:
                            self._log(f"Total: {new_count} new files downloaded")
                        else:
                            self._log(f"No new images ({len(scans)} scans checked)")
                    else:
                        self._log(f"No new images ({len(scans)} scans checked)")

            except Exception as e:
                self._log(f"Error: {e}")
                self.root.after(0, self._hide_progress)

            if watch_mode:
                self.root.after(0, self._hide_progress)

            if not watch_mode:
                self._log("Download complete.")
                self.root.after(0, self._finish_download_thread)
                break

            # Wait for interval or stop, showing countdown in title
            interval_secs = self.interval_var.get() * 60
            for remaining in range(interval_secs, 0, -1):
                if self.stop_event.is_set():
                    break
                mins, secs = divmod(remaining, 60)
                self.root.after(0, lambda m=mins, s=secs:
                    self.root.title(f"Incucyte — Next poll in {m}m {s:02d}s"))
                self.stop_event.wait(1)

            if self.stop_event.is_set():
                self._log("Watch stopped.")
                self.root.after(0, self._finish_download_thread)
                break

    def _show_progress(self, total=0, unit_label="files", file_total=None,
                       stage="Preparing download", detail=""):
        if self.progress_dialog:
            try:
                self.progress_dialog.set_stage(
                    stage=stage, detail=detail, total=total,
                    unit_label=unit_label, file_total=file_total)
                return
            except tk.TclError:
                self.progress_dialog = None
        self.progress_dialog = ProgressDialog(
            self.root, total, self.stop_event,
            unit_label=unit_label, file_total=file_total,
            stage=stage, detail=detail)

    def _set_progress_stage(self, stage, detail="", total=None,
                            unit_label=None, file_total=None):
        self._show_progress(
            total=0 if total is None else total,
            unit_label=unit_label or "items",
            file_total=file_total,
            stage=stage,
            detail=detail)

    def _update_task_progress(self, stage, detail, done, total, unit_label):
        self.root.title(f"Incucyte - {stage} {done}/{total}")
        if self.progress_dialog:
            try:
                self.progress_dialog.set_stage(stage=stage)
                self.progress_dialog.update_progress(
                    detail, 0, done, total, unit_label=unit_label)
            except tk.TclError:
                self.progress_dialog = None

    def _update_progress(self, fname, size, done, total):
        self.root.title(f"Incucyte - Downloading {done}/{total}")
        if self.progress_dialog:
            try:
                self.progress_dialog.update_progress(fname, size, done, total)
            except tk.TclError:
                self.progress_dialog = None

    def _update_file_progress(self, fname, size, done, total):
        if self.progress_dialog:
            try:
                self.progress_dialog.update_file_progress(fname, size, done, total)
            except tk.TclError:
                self.progress_dialog = None

    def _hide_progress(self):
        self.root.title("Incucyte Auto-Downloader")
        if self.progress_dialog:
            try:
                self.progress_dialog.finish()
            except tk.TclError:
                pass
            self.progress_dialog = None

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
            title="Save log")
        if path:
            content = self.log_text.get("1.0", "end").strip()
            Path(path).write_text(content, encoding="utf-8")
            self._log(f"Log saved to {path}")

    def _save_state(self):
        """Persist GUI state to disk."""
        # Convert well sets to serializable lists
        wells_data = {}
        for vid, wells in self.selected_wells.items():
            wells_data[str(vid)] = [[r, c] for r, c in wells]

        state = {
            "host": self.host_var.get(),
            "output": self.output_var.get(),
            "interval": self.interval_var.get(),
            "phase": self.ch_phase.get(),
            "color1": self.ch_color1.get(),
            "color2": self.ch_color2.get(),
            "selected_vessels": self._get_selected_vessels(),
            "wells": wells_data,
            "max_workers": self.workers_var.get(),
            "green_phase": self.green_phase_var.get(),
            "hyperstack": self.hyperstack_var.get(),
            "time_stack": self.time_stack_var.get(),
            "start_from": self.start_from_var.get(),
            "custom_date": self.custom_date_var.get(),
        }
        save_gui_state(state)

    def _load_state(self):
        """Restore GUI state from disk."""
        state = load_gui_state()
        if not state:
            return

        if state.get("host"):
            self.host_var.set(state["host"])
            self.host = state["host"]
        if state.get("output"):
            self.output_var.set(state["output"])
        if "interval" in state:
            self.interval_var.set(state["interval"])
        if "phase" in state:
            self.ch_phase.set(state["phase"])
        if "color1" in state:
            self.ch_color1.set(state["color1"])
        if "color2" in state:
            self.ch_color2.set(state["color2"])

        if "max_workers" in state:
            self.workers_var.set(state["max_workers"])
        if "green_phase" in state:
            self.green_phase_var.set(state["green_phase"])
        if "hyperstack" in state:
            self.hyperstack_var.set(state["hyperstack"])
        if "time_stack" in state:
            self.time_stack_var.set(state["time_stack"])
        self._on_stack_option_toggle()
        if "start_from" in state:
            self.start_from_var.set(state["start_from"])
            self._on_start_from_change()
        if "custom_date" in state:
            self.custom_date_var.set(state["custom_date"])

        # Restore well selections
        for vid_str, wells_list in state.get("wells", {}).items():
            self.selected_wells[int(vid_str)] = {(r, c) for r, c in wells_list}


def main():
    root = tk.Tk()
    app = App(root)
    def on_close():
        app._save_state()
        if app.watching:
            app.stop_event.set()
            if app.watch_thread and app.watch_thread.is_alive():
                app.watch_thread.join(timeout=3)
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
