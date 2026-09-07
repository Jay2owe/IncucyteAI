"""Modal dialogs: logging in, confirming a plan, and the about box."""

import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .. import __version__
from ..engine import APP_DIR
from ..models import LAYOUT_DESCRIPTIONS, LAYOUT_LABELS, human_bytes
from ..schedule import CADENCES, DEFAULT_CADENCE
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
            x, y = self._centred_position(
                self.parent.winfo_rootx(), self.parent.winfo_rooty(),
                self.parent.winfo_width(), self.parent.winfo_height(),
                self.winfo_width(), self.winfo_height())
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        self.grab_set()
        self.wait_window(self)
        return self.result

    @staticmethod
    def _centred_position(parent_x, parent_y, parent_width, parent_height,
                           dialog_width, dialog_height):
        """Centre on the parent without discarding negative monitor coordinates."""
        return (
            parent_x + max(0, (parent_width - dialog_width) // 2),
            parent_y + max(0, (parent_height - dialog_height) // 3),
        )


class LoginDialog(ModalDialog):
    """Username and password, encrypted and checked off the UI thread."""

    def __init__(self, parent, theme, client, host=None):
        super().__init__(parent, theme, "Sign in to the Incucyte")
        self.client = client
        saved = client.credentials

        self.host_var = tk.StringVar(value=host or client.host)
        self.device_name_var = tk.StringVar(value=saved.device_name or "")
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
                       "is stored. Device name is a local label for the "
                       "chooser.").grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(2, theme_mod.PAD_L))

        rows = (("Device name", self.device_name_var, False),
                ("Address", self.host_var, False),
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
        self.status.grid(row=6, column=0, columnspan=2, sticky="w",
                         pady=(theme_mod.PAD_S, 0))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=7, column=0, columnspan=2, sticky="e",
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
            self.status_var.set("Address, username and password are all needed.")
            return
        self._busy = True
        self.login_btn.configure(state="disabled")
        self.status.configure(style="Muted.TLabel")
        self.status_var.set("Encrypting password...")
        self.client.host = host
        threading.Thread(
            target=self._worker,
            args=(username, password, self.device_name_var.get().strip()),
                         daemon=True).start()

    def _worker(self, username, password, device_name):
        try:
            self.after(0, lambda: self.status_var.set("Authenticating..."))
            credentials = self.client.login(
                username, password, device_name=device_name)
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


class ExportSettingsDialog(ModalDialog):
    """The settings checkpoint shown immediately before an export action."""

    def __init__(self, parent, theme, *, title, mode_label, description, note,
                 confirm_text, build_form, validate, tone="accent",
                 minimum_size=(940, 720)):
        super().__init__(parent, theme, title, resizable=True)
        self.validate = validate

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)

        ttk.Label(body, text=title, style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(body, text=description, style="Muted.TLabel",
                  wraplength=820, justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, theme_mod.PAD_M))

        banner = tk.Frame(body, background=theme[f"{tone}_soft"],
                          padx=theme_mod.PAD_M, pady=theme_mod.PAD_S)
        banner.grid(row=2, column=0, sticky="ew",
                    pady=(0, theme_mod.PAD_L))
        tk.Label(banner, text=mode_label, background=theme[f"{tone}_soft"],
                 foreground=theme[tone], font=theme.font_bold).pack(
            anchor="w")
        tk.Label(banner, text=note, background=theme[f"{tone}_soft"],
                 foreground=theme["text"], font=theme.font,
                 wraplength=850, justify="left").pack(
            anchor="w", pady=(theme_mod.PAD_XS, 0))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=4, column=0, sticky="ew",
                     pady=(theme_mod.PAD_L, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text=confirm_text, style="Accent.TButton",
                   command=self._accept).grid(row=0, column=1)
        ttk.Button(buttons, text="Cancel", command=self.cancel).grid(
            row=0, column=2, padx=(theme_mod.PAD_S, 0))

        form = ttk.Frame(body, style="Surface.TFrame")
        form.grid(row=3, column=0, sticky="nsew")
        form.grid_propagate(False)
        build_form(form)

        # Deliberately no Return binding: starting an export must require the
        # visible confirmation button, even when a menu was opened by keyboard.
        self.minsize(*minimum_size)

    def _accept(self):
        options = self.validate(self)
        if options is None:
            return
        self.result = options
        self.destroy()


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
        python_btn = ttk.Button(buttons, text="Copy Python",
                                command=self._copy_python)
        python_btn.pack(side="left")
        tip(python_btn,
            "Copies a runnable Python program using IncucyteClient and these "
            "export settings.", theme)
        cli_btn = ttk.Button(buttons, text="Copy CLI command",
                             command=self._copy_cli)
        cli_btn.pack(side="left", padx=(theme_mod.PAD_S, 0))
        tip(cli_btn,
            "Copies the equivalent pyincucyte command-line interface command.",
            theme)
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

    def _copy_text(self, value, confirmation):
        self.clipboard_clear()
        self.clipboard_append(value)
        self.copied_var.set(confirmation)
        self.after(1800, lambda: self.copied_var.set(""))

    def _copy_python(self):
        self._copy_text(self.options.python_code(), "Python copied")

    def _copy_cli(self):
        self._copy_text(self.options.cli_command(), "CLI copied")


class ScheduleDialog(ModalDialog):
    """Choose when Windows should run one self-contained synchronization."""

    def __init__(self, parent, theme, default_name):
        super().__init__(parent, theme, "Scheduled download")
        self.name_var = tk.StringVar(value=default_name)
        self.cadence_var = tk.StringVar(value=DEFAULT_CADENCE)
        self.replace_var = tk.BooleanVar(value=False)
        self.logged_out_var = tk.BooleanVar(value=True)
        self.wake_var = tk.BooleanVar(value=False)

        card = Card(self, theme=theme)
        card.pack(fill="both", expand=True, padx=theme_mod.PAD_L,
                  pady=theme_mod.PAD_L)
        body = card.body

        ttk.Label(body, text="Schedule this download",
                  style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            body, style="Muted.TLabel", wraplength=430, justify="left",
            text="Like an alarm clock, Windows starts one synchronization at "
                 "each interval and then closes it. PyIncucyte does not need "
                 "to remain open, and by default neither does anyone's logon "
                 "session - it keeps downloading through a reboot.").grid(
            row=1, column=0, columnspan=2, sticky="w",
            pady=(2, theme_mod.PAD_L))

        ttk.Label(body, text="Name", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, theme_mod.PAD_M),
            pady=(0, theme_mod.PAD_M))
        self.name_entry = ttk.Entry(body, textvariable=self.name_var, width=34)
        self.name_entry.grid(row=2, column=1, sticky="ew",
                             pady=(0, theme_mod.PAD_M))

        ttk.Label(body, text="Run", style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, theme_mod.PAD_M),
            pady=(0, theme_mod.PAD_M))
        ttk.Combobox(body, textvariable=self.cadence_var,
                     values=list(CADENCES), state="readonly", width=22).grid(
            row=3, column=1, sticky="w", pady=(0, theme_mod.PAD_M))

        logged_out = ttk.Checkbutton(
            body, text="Keep downloading when nobody is logged in",
            variable=self.logged_out_var)
        logged_out.grid(row=4, column=0, columnspan=2, sticky="w")
        # Said here rather than discovered when a console appears. Windows will
        # not run a task on a locked, freshly rebooted machine unless it holds
        # the account's credential, and it asks for that itself.
        tip(logged_out, "Windows opens a prompt of its own for this account's "
                        "credential and keeps it. Nothing here sees it. Turn "
                        "this off and downloads wait until somebody logs in.",
            theme)

        wake = ttk.Checkbutton(body, text="Wake the computer for each check",
                               variable=self.wake_var)
        wake.grid(row=5, column=0, columnspan=2, sticky="w")
        tip(wake, "A sleeping computer otherwise sleeps through every check.",
            theme)

        replace = ttk.Checkbutton(
            body, text="Replace a schedule with the same name",
            variable=self.replace_var)
        replace.grid(row=6, column=0, columnspan=2, sticky="w")
        tip(replace, "Leave this off to prevent an existing schedule being "
                     "overwritten by accident.", theme)

        self.status_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status_var, style="Danger.TLabel",
                  wraplength=430).grid(row=7, column=0, columnspan=2,
                                       sticky="w", pady=(theme_mod.PAD_S, 0))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=8, column=0, columnspan=2, sticky="e",
                     pady=(theme_mod.PAD_L, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(
            side="left", padx=(0, theme_mod.PAD_S))
        ttk.Button(buttons, text="Create schedule", style="Accent.TButton",
                   command=self._accept).pack(side="left")

        body.columnconfigure(1, weight=1)
        # REGRESSION GUARD: when a menu item was activated with Enter, binding
        # Return here could accept the new dialog before the user saw it.
        # Creation is deliberately limited to the visible button.
        self.after(60, self.name_entry.focus_set)

    def _accept(self):
        name = self.name_var.get().strip()
        cadence = self.cadence_var.get()
        if not name:
            self.status_var.set("Give the scheduled download a name.")
            return
        if cadence not in CADENCES:
            self.status_var.set("Choose how often the download should run.")
            return
        # The duration string the command line takes, not schtasks' own
        # vocabulary: the window hands `--every 6h` to the same parser a
        # person types at, so the two front ends cannot mean different
        # periods.
        self.result = {
            "name": name,
            "cadence": cadence,
            "every": CADENCES[cadence],
            "logged_out": bool(self.logged_out_var.get()),
            "wake": bool(self.wake_var.get()),
            "replace": bool(self.replace_var.get()),
        }
        self.destroy()


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
            ("Device", client.device_name or "unnamed"),
            ("Address", client.host),
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


__all__ = ["LoginDialog", "PlanDialog", "ScheduleDialog", "AboutDialog",
           "ModalDialog"]
