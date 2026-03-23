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
    authenticate, api_post, unpack_values, download_scan_images,
    collect_scan_images, collect_scans_in_range,
    parse_wells, parse_channels, parse_scan_datetime, load_config,
    save_config, encrypt_password, get_token, DEFAULT_HOST,
    API_BASE_TEMPLATE, CONFIG_FILE, IMAGE_TYPE_MAP, load_state,
    save_state, SCRIPT_DIR,
)

GUI_STATE_FILE = SCRIPT_DIR / ".tmp" / "gui_state.json"

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
    (SCRIPT_DIR / ".tmp").mkdir(exist_ok=True)
    GUI_STATE_FILE.write_text(json.dumps(state, indent=2))


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

    def __init__(self, parent, total, stop_event):
        super().__init__(parent)
        self.title("Downloading...")
        self.resizable(False, False)
        self.total = total
        self.stop_event = stop_event
        self.completed = 0
        self._times = []  # list of per-file download durations
        self._last_time = None

        frame = ttk.Frame(self, padding=15)
        frame.pack(fill="both", expand=True)

        self.count_var = tk.StringVar(value=f"0 / {total} files")
        ttk.Label(frame, textvariable=self.count_var, font=("Segoe UI", 11, "bold")).pack(pady=(0, 6))

        self.progress = ttk.Progressbar(frame, length=360, mode="determinate", maximum=total)
        self.progress.pack(pady=(0, 6))

        self.pct_var = tk.StringVar(value="0%")
        ttk.Label(frame, textvariable=self.pct_var).pack()

        self.current_var = tk.StringVar(value="")
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
        self.stop_event.set()

    def update_progress(self, fname, size, done, total):
        """Called from worker thread via root.after()."""
        import time as _time
        now = _time.monotonic()
        if self._last_time is not None:
            self._times.append(now - self._last_time)
            # Rolling average of last 20 files
            if len(self._times) > 20:
                self._times = self._times[-20:]
        self._last_time = now

        self.completed = done
        self.total = total
        self.count_var.set(f"{done} / {total} files")
        self.progress.config(maximum=total, value=done)
        pct = int(100 * done / total) if total else 0
        self.pct_var.set(f"{pct}%")
        self.current_var.set(f"Current: {fname}")

        if self._times:
            avg = sum(self._times) / len(self._times)
            self.speed_var.set(f"Speed: ~{avg:.1f}s per file")
            remaining = avg * (total - done)
            mins, secs = divmod(int(remaining), 60)
            self.remaining_var.set(f"Remaining: ~{mins}m {secs:02d}s")

    def finish(self):
        try:
            self.destroy()
        except tk.TclError:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Incucyte Auto-Downloader")
        self.root.minsize(720, 700)

        self.msg_queue = queue.Queue()
        self.watching = False
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
        ttk.Checkbutton(ch_row, text="Color 1", variable=self.ch_color1).pack(side="left", padx=4)
        ttk.Checkbutton(ch_row, text="Color 2", variable=self.ch_color2).pack(side="left", padx=4)

        opt_row = ttk.Frame(settings_frame)
        opt_row.pack(fill="x", pady=(0, 4))
        self.green_phase_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row, text="Apply green LUT to Phase", variable=self.green_phase_var).pack(side="left")

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
        ttk.Button(ctrl_row, text="Download Now", command=self._download_now).pack(side="left", padx=4)
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
            vessels = unpack_values(data.get("Data", {}))
            if not isinstance(vessels, list):
                vessels = []
            self.vessels = vessels
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
        for v in self.vessels:
            vid = v.get("VesselID", "?")
            doc = v.get("VesselDocumentation", {})
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
            channels = v.get("Channels", {})
            phase = "Ph" if channels.get("Phase", {}).get("On") else ""
            colors = channels.get("Colors", {})
            c1 = "C1" if colors.get("Color1", {}).get("On") else ""
            c2 = "C2" if colors.get("Color2", {}).get("On") else ""
            ch_str = "+".join(filter(None, [phase, c1, c2]))
            self.vessel_tree.insert("", "end", iid=str(vid),
                                    values=(vid, vname, owner, last_scan, scan_type, ch_str))
        self._log(f"Found {len(self.vessels)} vessels")

    def _on_vessel_select(self, event=None):
        sel = self.vessel_tree.selection()
        if not sel:
            return
        vid = int(sel[0])
        vessel = next((v for v in self.vessels if v.get("VesselID") == vid), None)
        if vessel:
            self._build_well_grid(vid, vessel)

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
                vessel = next((v for v in self.vessels if v.get("VesselID") == vid), None)
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

    def _get_selected_vessels(self):
        """Return list of selected vessel IDs from the treeview."""
        sel = self.vessel_tree.selection()
        if not sel:
            return []
        return [int(s) for s in sel]

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

    def _download_now(self):
        result = self._validate_for_download()
        if not result:
            return
        vessel_ids, output = result
        self._save_state()
        self._log("Starting one-shot download...")
        threading.Thread(target=self._download_thread, args=(vessel_ids, output, False), daemon=True).start()

    def _start_watching(self):
        result = self._validate_for_download()
        if not result:
            return
        vessel_ids, output = result
        self._save_state()
        self.watching = True
        self.stop_event.clear()
        self.watch_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log("Watch mode started")
        self.watch_thread = threading.Thread(
            target=self._download_thread, args=(vessel_ids, output, True), daemon=True)
        self.watch_thread.start()

    def _stop_watching(self):
        self.stop_event.set()
        self.watching = False
        self.watch_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._log("Stopping watch...")

    def _download_thread(self, vessel_ids, output_path, watch_mode):
        output = Path(output_path)
        output.mkdir(parents=True, exist_ok=True)
        channels = self._get_selected_channels()
        state = load_state()
        max_workers = self.workers_var.get()
        green_phase = self.green_phase_var.get()

        # Resolve start date from GUI setting
        start_date = self._resolve_start_date(vessel_ids)
        self._log(f"Scanning from {start_date} to today")

        # Get FirstScanDateTime from vessel data for elapsed-time filenames
        reference_times = {}
        for vid in vessel_ids:
            vessel = next((v for v in self.vessels if v.get("VesselID") == vid), None)
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

                scans = collect_scans_in_range(host, token, start_date, now.date())

                if not scans:
                    self._log("No scans found.")
                else:
                    # Collect all images first for progress tracking
                    all_items = []
                    for scan_time in scans:
                        for vid in vessel_ids:
                            wells = self.selected_wells.get(vid)
                            items = collect_scan_images(
                                host, token, vid, scan_time, output,
                                state, wells=wells, channels=channels,
                                reference_time=reference_times.get(vid))
                            all_items.extend(items)

                    if all_items:
                        total = len(all_items)
                        self._log(f"Found {total} new images to download")

                        # Show progress dialog
                        self.root.after(0, lambda t=total: self._show_progress(t))

                        def on_progress(fname, size, done, total_count):
                            self.root.after(0, lambda: self._update_progress(fname, size, done, total_count))

                        # Download with parallel workers
                        new_count = 0
                        for scan_time in scans:
                            if self.stop_event.is_set():
                                break
                            for vid in vessel_ids:
                                if self.stop_event.is_set():
                                    break
                                wells = self.selected_wells.get(vid)
                                n = download_scan_images(
                                    host, token, vid, scan_time, output,
                                    state, wells=wells, channels=channels,
                                    reference_time=reference_times.get(vid),
                                    max_workers=max_workers,
                                    green_phase=green_phase,
                                    progress_callback=on_progress,
                                    stop_event=self.stop_event)
                                if n:
                                    new_count += n
                                    self._log(f"Downloaded {n} images from vessel {vid}")

                        self.root.after(0, self._hide_progress)

                        if new_count:
                            self._log(f"Total: {new_count} new images downloaded")
                        else:
                            self._log(f"No new images ({len(scans)} scans checked)")
                    else:
                        self._log(f"No new images ({len(scans)} scans checked)")

            except Exception as e:
                self._log(f"Error: {e}")
                self.root.after(0, self._hide_progress)

            if not watch_mode:
                self._log("Download complete.")
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
                self.root.after(0, lambda: self.root.title("Incucyte Auto-Downloader"))
                self.root.after(0, lambda: self.watch_btn.config(state="normal"))
                self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
                break

    def _show_progress(self, total):
        if self.progress_dialog:
            try:
                self.progress_dialog.destroy()
            except tk.TclError:
                pass
        self.progress_dialog = ProgressDialog(self.root, total, self.stop_event)

    def _update_progress(self, fname, size, done, total):
        self.root.title(f"Incucyte — Downloading {done}/{total}")
        if self.progress_dialog:
            try:
                self.progress_dialog.update_progress(fname, size, done, total)
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
