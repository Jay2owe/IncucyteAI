"""Modal dialogs: logging in, confirming a plan, and the about box."""

import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .. import __version__
from ..engine import APP_DIR
from ..models import LAYOUT_DESCRIPTIONS, LAYOUT_LABELS, human_bytes
from . import theme as theme_mod
from .widgets import Card, tip


class ModalDialog(tk.Toplevel):
    """Shared plumbing: centred on the parent, Escape closes, modal."""

    def __init__(self, parent, theme, title, resizable=False):
        super().__init__(parent)
        self.theme = theme
        self.parent = parent
        self.result = None
        self.title(title)
        self.configure(background=theme["bg"])
        self.transient(parent)
        self.resizable(resizable, resizable)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def cancel(self):
        self.result = None
        self.destroy()

    def show(self):
        """Centre, grab focus, and block until closed. Returns ``result``."""
        self.update_idletasks()
        try:
            x = self.parent.winfo_rootx() + max(
                0, (self.parent.winfo_width() - self.winfo_width()) // 2)
            y = self.parent.winfo_rooty() + max(
                0, (self.parent.winfo_height() - self.winfo_height()) // 3)
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:
            pass
        self.grab_set()
        self.wait_window(self)
        return self.result


class LoginDialog(ModalDialog):
    """Username and password, encrypted and checked off the UI thread."""

    def __init__(self, parent, theme, client, host=None):
        super().__init__(parent, theme, "Sign in to the Incucyte")
        self.client = client
        saved = client.credentials

        self.host_var = tk.StringVar(value=host or client.host)
        self.username_var = tk.StringVar(value=saved.username or "")
        self.password_var = tk.StringVar()
        self.status_var = tk.StringVar(value="")
        self._busy = False

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body

        ttk.Label(body, text="Sign in", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=330,
                  text="Your password is hashed by the Incucyte's own client "
                       "library before it leaves this machine; only the hash "
                       "is stored.").grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(2, theme_mod.PAD_L))

        rows = (("Device", self.host_var, False),
                ("Username", self.username_var, False),
                ("Password", self.password_var, True))
        self.entries = {}
        for index, (label, variable, secret) in enumerate(rows, start=2):
            ttk.Label(body, text=label, style="Muted.TLabel").grid(
                row=index, column=0, sticky="w", pady=(0, theme_mod.PAD_S),
                padx=(0, theme_mod.PAD_M))
            entry = ttk.Entry(body, textvariable=variable, width=30,
                              show="•" if secret else "")
            entry.grid(row=index, column=1, sticky="ew",
                       pady=(0, theme_mod.PAD_S))
            self.entries[label] = entry

        self.status = ttk.Label(body, textvariable=self.status_var,
                                style="Danger.TLabel", wraplength=330)
        self.status.grid(row=5, column=0, columnspan=2, sticky="w",
                         pady=(theme_mod.PAD_S, 0))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=6, column=0, columnspan=2, sticky="e",
                     pady=(theme_mod.PAD_L, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(
            side="left", padx=(0, theme_mod.PAD_S))
        self.login_btn = ttk.Button(buttons, text="Sign in",
                                    style="Accent.TButton", command=self._submit)
        self.login_btn.pack(side="left")

        body.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda e: self._submit())
        first = "Password" if self.username_var.get() else "Username"
        self.after(60, self.entries[first].focus_set)

    def _submit(self):
        if self._busy:
            return
        username = self.username_var.get().strip()
        password = self.password_var.get()
        host = self.host_var.get().strip()
        if not (username and password and host):
            self.status_var.set("Device, username and password are all needed.")
            return
        self._busy = True
        self.login_btn.configure(state="disabled")
        self.status.configure(style="Muted.TLabel")
        self.status_var.set("Encrypting password...")
        self.client.host = host
        threading.Thread(target=self._worker, args=(username, password),
                         daemon=True).start()

    def _worker(self, username, password):
        try:
            self.after(0, lambda: self.status_var.set("Authenticating..."))
            credentials = self.client.login(username, password)
            self.after(0, lambda: self._succeeded(credentials))
        except Exception as exc:
            message = str(exc)
            self.after(0, lambda: self._failed(message))

    def _succeeded(self, credentials):
        self.result = credentials
        self.destroy()

    def _failed(self, message):
        self._busy = False
        self.login_btn.configure(state="normal")
        self.status.configure(style="Danger.TLabel")
        self.status_var.set(message[:220])


class PlanDialog(ModalDialog):
    """Pre-flight confirmation: exactly what is about to be written, and where."""

    def __init__(self, parent, theme, plan, options):
        super().__init__(parent, theme, "Confirm export", resizable=True)
        self.plan = plan
        self.options = options

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body

        ttk.Label(body, text="Ready to export", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, style="Muted.TLabel",
                  text=f"{LAYOUT_LABELS[plan.layout]} - "
                       f"{LAYOUT_DESCRIPTIONS[plan.layout]}").pack(
            anchor="w", pady=(2, theme_mod.PAD_L))

        stats = ttk.Frame(body, style="Surface.TFrame")
        stats.pack(fill="x", pady=(0, theme_mod.PAD_L))
        figures = (
            ("Output files", f"{plan.output_file_count:,}"),
            ("Source images", f"{plan.source_image_count:,}"),
            ("Estimated size", human_bytes(plan.estimated_bytes)),
            ("Scan times", f"{len(plan.scan_times):,}"),
            ("Wells", f"{plan.well_count:,}"),
        )
        for column, (caption, value) in enumerate(figures):
            cell = ttk.Frame(stats, style="Surface.TFrame")
            cell.grid(row=0, column=column, sticky="w",
                      padx=(0, theme_mod.PAD_XL))
            ttk.Label(cell, text=caption.upper(), style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, text=value, style="Value.TLabel").pack(anchor="w")

        ttk.Label(body, text="DESTINATION", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(body, text=str(plan.output_dir), style="Surface.TLabel",
                  wraplength=560).pack(anchor="w", pady=(0, theme_mod.PAD_M))

        ttk.Label(body, text="FILES TO BE WRITTEN", style="Muted.TLabel").pack(
            anchor="w")
        listing = tk.Listbox(
            body, height=7, bd=0, highlightthickness=1, activestyle="none",
            background=theme["surface_alt"], foreground=theme["text"],
            highlightbackground=theme["border"], font=theme.font_mono,
            selectbackground=theme["accent_soft"], selectforeground=theme["text"])
        listing.pack(fill="both", expand=True, pady=(theme_mod.PAD_XS, 0))
        for name in plan.preview(200):
            listing.insert("end", name)
        if plan.output_file_count > 200:
            listing.insert("end", f"... and {plan.output_file_count - 200:,} more")

        note = ttk.Label(
            body, style="Muted.TLabel", wraplength=560,
            text="Files are the instrument's stored payloads: Phase is 8-bit, the "
                 "fluorescence channels are uncalibrated 16-bit. Scan times that "
                 "do not contain this vessel are skipped. A manifest listing every "
                 "file, well, channel and timepoint is written alongside the images.")
        note.pack(anchor="w", pady=(theme_mod.PAD_M, 0))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(theme_mod.PAD_L, 0))
        copy_btn = ttk.Button(buttons, text="Copy CLI command",
                              command=self._copy_command)
        copy_btn.pack(side="left")
        tip(copy_btn,
            "Copies the equivalent pyincucyte command, so this export can be "
            "dropped straight into a pipeline script.", theme)
        self.copied_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.copied_var,
                  style="Success.TLabel").pack(side="left", padx=theme_mod.PAD_M)

        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(
            side="right", padx=(theme_mod.PAD_S, 0))
        ttk.Button(buttons, text="Download", style="Accent.TButton",
                   command=self._accept).pack(side="right")

        self.bind("<Return>", lambda e: self._accept())
        self.minsize(620, 520)

    def _accept(self):
        self.result = True
        self.destroy()

    def _copy_command(self):
        self.clipboard_clear()
        self.clipboard_append(self.options.cli_command())
        self.copied_var.set("Copied")
        self.after(1800, lambda: self.copied_var.set(""))


class ProtocolWindow(tk.Toplevel):
    """The acquisition protocol, drawn - the same picture the CLI prints.

    Deliberately not modal: this is a reference you leave open beside the export
    settings while you set them up, not a question to answer.  The drawing shown
    is the ASCII one, because it is the same geometry as the SVG and needs no
    image decoder; ``Save`` writes the real figure.
    """

    WIDTH = 108

    def __init__(self, parent, theme, protocol, on_save=None):
        super().__init__(parent)
        self.theme = theme
        self.protocol_record = protocol
        self.on_save = on_save
        self.title(f"Acquisition protocol - {protocol.title()}")
        self.configure(background=theme["bg"])
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body

        ttk.Label(body, text="How this run was set up",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=760,
                  text="Read off the instrument's own metadata - no image was "
                       "fetched. Requested and achieved are different facts "
                       "and both are on the drawing.").pack(
            anchor="w", pady=(2, theme_mod.PAD_L))

        text = tk.Text(body, wrap="none", bd=0, highlightthickness=1,
                       background=theme["surface_alt"], foreground=theme["text"],
                       highlightbackground=theme["border"],
                       font=theme.font_mono, height=26, width=self.WIDTH + 2,
                       padx=theme_mod.PAD_M, pady=theme_mod.PAD_M)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(protocol.lines(width=self.WIDTH)))
        text.configure(state="disabled")
        self.text = text

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(theme_mod.PAD_L, 0))
        self.dark_var = tk.BooleanVar(value=False)
        dark = ttk.Checkbutton(buttons, text="Dark drawing",
                               variable=self.dark_var)
        dark.pack(side="left")
        tip(dark, "Save the figure on a dark background, for a dark slide.",
            theme)
        save_btn = ttk.Button(buttons, text="Save drawing...",
                              style="Accent.TButton", command=self._save)
        save_btn.pack(side="right")
        tip(save_btn,
            "SVG needs nothing and scales; PNG and PDF need matplotlib, which "
            "the packaged app does not ship.", theme)
        ttk.Button(buttons, text="Copy", command=self._copy).pack(
            side="right", padx=(0, theme_mod.PAD_S))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(
            side="right", padx=(0, theme_mod.PAD_S))

        self.minsize(700, 520)

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(
            self.protocol_record.lines(width=self.WIDTH)))

    def _save(self):
        if self.on_save is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save the protocol drawing",
            defaultextension=".svg",
            initialfile=f"{self.protocol_record.vessel_id}-protocol.svg",
            filetypes=[("SVG drawing", "*.svg"), ("PNG image", "*.png"),
                       ("PDF document", "*.pdf")])
        if not path:
            return
        # Writing a PNG goes through matplotlib and takes about a second, so
        # the app writes it on a worker like everything else.
        self.on_save(self.protocol_record, path, bool(self.dark_var.get()))


class AboutDialog(ModalDialog):
    """Version, where settings live, and how to reach the API."""

    def __init__(self, parent, theme, client):
        super().__init__(parent, theme, "About PyIncucyte")

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body

        ttk.Label(body, text="PyIncucyte", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"Version {__version__}",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, theme_mod.PAD_L))

        for caption, value in (
            ("Device", client.host),
            ("Signed in as", client.username or "not signed in"),
            ("Settings folder", str(APP_DIR)),
        ):
            row = ttk.Frame(body, style="Surface.TFrame")
            row.pack(fill="x", pady=theme_mod.PAD_XS)
            ttk.Label(row, text=caption, style="Muted.TLabel",
                      width=16, anchor="w").pack(side="left")
            ttk.Label(row, text=value, style="Surface.TLabel",
                      wraplength=340).pack(side="left")

        ttk.Separator(body).pack(fill="x", pady=theme_mod.PAD_L)
        ttk.Label(body, text="Use it from Python", style="Heading.TLabel").pack(
            anchor="w")
        snippet = tk.Text(body, height=7, width=54, bd=0, highlightthickness=1,
                          background=theme["surface_alt"], foreground=theme["text"],
                          highlightbackground=theme["border"],
                          font=theme.font_mono, padx=8, pady=6, wrap="none")
        snippet.pack(fill="x", pady=(theme_mod.PAD_XS, 0))
        snippet.insert("1.0",
                       "from pyincucyte import IncucyteClient\n\n"
                       "with IncucyteClient.from_saved() as incucyte:\n"
                       "    result = incucyte.fetch(\n"
                       "        vessel=38, output='./run-01',\n"
                       "        channels='phase', layout='time_stack')\n"
                       "    paths = result.paths\n")
        snippet.configure(state="disabled")

        ttk.Button(body, text="Close", style="Accent.TButton",
                   command=self.cancel).pack(anchor="e", pady=(theme_mod.PAD_L, 0))


__all__ = ["LoginDialog", "PlanDialog", "AboutDialog", "ModalDialog"]
