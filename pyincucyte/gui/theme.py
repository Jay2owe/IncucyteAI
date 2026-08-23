"""Look and feel for the desktop app.

Tk's default widgets look dated because nobody ever tells them otherwise.  This
module does three things that account for most of the difference: it turns on
per-monitor DPI awareness (without it Windows stretches the window like a
low-resolution image), it picks a coherent palette that follows the system's
light/dark setting, and it registers named ttk styles so the rest of the code
asks for ``style="Accent.TButton"`` rather than hand-colouring widgets.
"""

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------------------
# palettes
# ---------------------------------------------------------------------------

LIGHT = {
    "bg": "#EEF1F5",          # window behind the cards
    "surface": "#FFFFFF",     # card background
    "surface_alt": "#F6F8FA",  # striped rows, inset panels
    "border": "#D5DCE5",
    "border_strong": "#B9C4D0",
    "text": "#16212E",
    "text_muted": "#657588",
    "text_inverse": "#FFFFFF",
    "accent": "#0E7C86",
    "accent_hover": "#0A626A",
    "accent_soft": "#DFF1F2",
    "selection": "#0E7C86",
    "success": "#2E7D32",
    "success_soft": "#E4F1E4",
    "warning": "#B26A00",
    "warning_soft": "#FBEEDC",
    "danger": "#C62828",
    "danger_soft": "#FBE6E6",
    "well_on": "#0E7C86",
    "well_on_hover": "#12A0AD",
    "well_off": "#DFE5EC",
    "well_off_hover": "#CBD5E0",
    "well_empty": "#F1F4F8",
    "grid_label": "#657588",
}

DARK = {
    "bg": "#171C24",
    "surface": "#1F262F",
    "surface_alt": "#252D38",
    "border": "#333D4A",
    "border_strong": "#46525F",
    "text": "#E8EDF3",
    "text_muted": "#94A3B3",
    "text_inverse": "#0B1016",
    "accent": "#2AA9B4",
    "accent_hover": "#3FC2CD",
    "accent_soft": "#1B3B40",
    "selection": "#2AA9B4",
    "success": "#5DBB63",
    "success_soft": "#1D2E20",
    "warning": "#E0A34A",
    "warning_soft": "#33280F",
    "danger": "#EF6C6C",
    "danger_soft": "#34191A",
    "well_on": "#2AA9B4",
    "well_on_hover": "#46C6D1",
    "well_off": "#333D4A",
    "well_off_hover": "#46525F",
    "well_empty": "#252D38",
    "grid_label": "#94A3B3",
}

#: Spacing scale, in pixels. Using a scale rather than ad-hoc numbers is what
#: makes a layout feel deliberate instead of assembled.
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 2, 4, 8, 12, 18


# ---------------------------------------------------------------------------
# platform integration
# ---------------------------------------------------------------------------

def enable_dpi_awareness():
    """Tell Windows we will handle scaling ourselves, so text stays crisp."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        try:                                     # Windows 10+ per-monitor v2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def system_prefers_dark():
    """Return True when the OS is set to a dark app theme."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except Exception:
        return False


def pick_font(root, candidates, fallback="TkDefaultFont"):
    """Return the first installed font family from a preference list."""
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except tk.TclError:
        return fallback
    for family in candidates:
        if family.lower() in available:
            return family
    return fallback


# ---------------------------------------------------------------------------
# the theme object
# ---------------------------------------------------------------------------

class Theme:
    """Holds the palette and fonts, and applies them to a Tk root."""

    def __init__(self, root, dark=None, scale=1.0):
        self.root = root
        self.dark = system_prefers_dark() if dark is None else bool(dark)
        self.colors = dict(DARK if self.dark else LIGHT)
        self.scale = scale
        self.style = ttk.Style(root)

        base = pick_font(root, ["Segoe UI Variable Text", "Segoe UI", "Inter",
                                "Helvetica Neue", "DejaVu Sans"])
        mono = pick_font(root, ["Cascadia Mono", "Consolas", "JetBrains Mono",
                                "DejaVu Sans Mono", "Courier New"])
        self.family = base
        self.mono_family = mono

        self.font = (base, 9)
        self.font_bold = (base, 9, "bold")
        self.font_small = (base, 8)
        self.font_title = (base, 15, "bold")
        self.font_heading = (base, 10, "bold")
        self.font_value = (base, 11, "bold")
        self.font_mono = (mono, 9)

        self.apply()

    # -- colours ----------------------------------------------------------

    def color(self, name):
        return self.colors[name]

    def __getitem__(self, name):
        return self.colors[name]

    # -- application ------------------------------------------------------

    def apply(self):
        c = self.colors
        root, style = self.root, self.style

        root.configure(background=c["bg"])
        try:
            style.theme_use("clam")   # the only built-in theme that restyles fully
        except tk.TclError:
            pass

        # Default fonts, so unstyled widgets follow along too.
        for name, spec in (("TkDefaultFont", self.font),
                           ("TkTextFont", self.font),
                           ("TkMenuFont", self.font),
                           ("TkHeadingFont", self.font_bold),
                           ("TkFixedFont", self.font_mono)):
            try:
                tkfont.nametofont(name).configure(
                    family=spec[0], size=spec[1],
                    weight="bold" if len(spec) > 2 else "normal")
            except tk.TclError:
                pass

        style.configure(".", background=c["bg"], foreground=c["text"],
                        fieldbackground=c["surface"], font=self.font,
                        borderwidth=0, focuscolor=c["accent"])

        # -- frames -------------------------------------------------------
        style.configure("TFrame", background=c["bg"])
        style.configure("Surface.TFrame", background=c["surface"])
        style.configure("Card.TFrame", background=c["surface"],
                        relief="solid", borderwidth=1,
                        bordercolor=c["border"])
        style.configure("Toolbar.TFrame", background=c["surface"])
        style.configure("Inset.TFrame", background=c["surface_alt"])

        # -- labels -------------------------------------------------------
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Surface.TLabel", background=c["surface"],
                        foreground=c["text"])
        style.configure("Title.TLabel", background=c["surface"],
                        foreground=c["text"], font=self.font_title)
        style.configure("Heading.TLabel", background=c["surface"],
                        foreground=c["text"], font=self.font_heading)
        style.configure("CardTitle.TLabel", background=c["surface"],
                        foreground=c["text_muted"], font=self.font_bold)
        style.configure("Muted.TLabel", background=c["surface"],
                        foreground=c["text_muted"])
        style.configure("MutedBg.TLabel", background=c["bg"],
                        foreground=c["text_muted"])
        style.configure("Value.TLabel", background=c["surface"],
                        foreground=c["text"], font=self.font_value)
        style.configure("Accent.TLabel", background=c["surface"],
                        foreground=c["accent"], font=self.font_bold)
        style.configure("Danger.TLabel", background=c["surface"],
                        foreground=c["danger"])
        style.configure("Warning.TLabel", background=c["surface"],
                        foreground=c["warning"])
        style.configure("Success.TLabel", background=c["surface"],
                        foreground=c["success"])

        # -- status pills -------------------------------------------------
        for name, fg, bg in (("Ok", c["success"], c["success_soft"]),
                             ("Warn", c["warning"], c["warning_soft"]),
                             ("Off", c["text_muted"], c["surface_alt"]),
                             ("Bad", c["danger"], c["danger_soft"])):
            style.configure(f"{name}.Pill.TLabel", background=bg, foreground=fg,
                            font=self.font_bold, padding=(8, 3))

        # -- buttons ------------------------------------------------------
        style.configure("TButton", background=c["surface_alt"],
                        foreground=c["text"], padding=(12, 6),
                        relief="flat", borderwidth=1, bordercolor=c["border"])
        style.map("TButton",
                  background=[("pressed", c["border"]),
                              ("active", c["border"]),
                              ("disabled", c["surface_alt"])],
                  foreground=[("disabled", c["text_muted"])],
                  bordercolor=[("active", c["border_strong"])])

        style.configure("Accent.TButton", background=c["accent"],
                        foreground=c["text_inverse"], font=self.font_bold,
                        padding=(14, 7), bordercolor=c["accent"])
        style.map("Accent.TButton",
                  background=[("pressed", c["accent_hover"]),
                              ("active", c["accent_hover"]),
                              ("disabled", c["border"])],
                  foreground=[("disabled", c["text_muted"])],
                  bordercolor=[("disabled", c["border"])])

        style.configure("Danger.TButton", background=c["surface_alt"],
                        foreground=c["danger"], bordercolor=c["border"])
        style.map("Danger.TButton",
                  background=[("active", c["danger_soft"]),
                              ("pressed", c["danger_soft"])],
                  foreground=[("disabled", c["text_muted"])])

        style.configure("Ghost.TButton", background=c["surface"],
                        foreground=c["text_muted"], padding=(8, 4),
                        bordercolor=c["surface"])
        style.map("Ghost.TButton",
                  background=[("active", c["surface_alt"])],
                  foreground=[("active", c["text"])])

        style.configure("Link.TButton", background=c["surface"],
                        foreground=c["accent"], padding=(2, 2),
                        bordercolor=c["surface"], relief="flat")
        style.map("Link.TButton", background=[("active", c["surface"])],
                  foreground=[("active", c["accent_hover"])])

        # -- inputs -------------------------------------------------------
        style.configure("TEntry", fieldbackground=c["surface"],
                        foreground=c["text"], bordercolor=c["border"],
                        lightcolor=c["border"], darkcolor=c["border"],
                        insertcolor=c["text"], padding=(6, 5),
                        borderwidth=1, relief="solid")
        style.map("TEntry",
                  bordercolor=[("focus", c["accent"])],
                  lightcolor=[("focus", c["accent"])],
                  darkcolor=[("focus", c["accent"])])

        style.configure("TCombobox", fieldbackground=c["surface"],
                        background=c["surface"], foreground=c["text"],
                        bordercolor=c["border"], arrowcolor=c["text_muted"],
                        padding=(6, 4), borderwidth=1, relief="solid")
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["surface"])],
                  bordercolor=[("focus", c["accent"])],
                  arrowcolor=[("active", c["accent"])])
        root.option_add("*TCombobox*Listbox.background", c["surface"])
        root.option_add("*TCombobox*Listbox.foreground", c["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        root.option_add("*TCombobox*Listbox.selectForeground", c["text_inverse"])

        style.configure("TSpinbox", fieldbackground=c["surface"],
                        foreground=c["text"], bordercolor=c["border"],
                        arrowcolor=c["text_muted"], padding=(5, 4),
                        borderwidth=1, relief="solid")

        # clam names these indicatorbackground (the box) and indicatorforeground
        # (the tick or dot) - there is no "indicatorcolor" option here.
        indicator = int(13 * self.scale)
        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(name, background=c["surface"], foreground=c["text"],
                            focuscolor=c["surface"], padding=(0, 3),
                            indicatorsize=indicator,
                            indicatormargin=(0, 0, 6, 0),
                            indicatorbackground=c["surface"],
                            indicatorforeground=c["text_inverse"],
                            upperbordercolor=c["border_strong"],
                            lowerbordercolor=c["border_strong"])
            style.map(name,
                      indicatorbackground=[
                          ("disabled", c["surface_alt"]),
                          ("selected", "pressed", c["accent_hover"]),
                          ("selected", c["accent"]),
                          ("active", c["surface_alt"]),
                      ],
                      indicatorforeground=[("disabled", c["text_muted"]),
                                           ("selected", c["text_inverse"])],
                      upperbordercolor=[("selected", c["accent"]),
                                        ("active", c["accent"]),
                                        ("disabled", c["border"])],
                      lowerbordercolor=[("selected", c["accent"]),
                                        ("active", c["accent"]),
                                        ("disabled", c["border"])],
                      foreground=[("disabled", c["text_muted"])])

        # -- treeview -----------------------------------------------------
        row_height = int(24 * self.scale)
        style.configure("Treeview", background=c["surface"],
                        fieldbackground=c["surface"], foreground=c["text"],
                        rowheight=row_height, borderwidth=0)
        style.map("Treeview",
                  background=[("selected", c["selection"])],
                  foreground=[("selected", c["text_inverse"])])
        style.configure("Treeview.Heading", background=c["surface_alt"],
                        foreground=c["text_muted"], font=self.font_bold,
                        relief="flat", padding=(8, 6), borderwidth=0)
        style.map("Treeview.Heading",
                  background=[("active", c["border"])],
                  foreground=[("active", c["text"])])

        # -- misc ---------------------------------------------------------
        style.configure("TProgressbar", background=c["accent"],
                        troughcolor=c["surface_alt"], bordercolor=c["border"],
                        lightcolor=c["accent"], darkcolor=c["accent"],
                        thickness=int(10 * self.scale))
        style.configure("Thin.TProgressbar", thickness=int(6 * self.scale))

        style.configure("TSeparator", background=c["border"])
        style.configure("TPanedwindow", background=c["bg"])
        style.configure("Sash", sashthickness=6, gripcount=0)

        style.configure("TNotebook", background=c["bg"], borderwidth=0,
                        tabmargins=(0, 4, 0, 0))
        style.configure("TNotebook.Tab", background=c["bg"],
                        foreground=c["text_muted"], padding=(14, 7),
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", c["surface"])],
                  foreground=[("selected", c["text"])])

        style.configure("Vertical.TScrollbar", background=c["surface_alt"],
                        troughcolor=c["bg"], bordercolor=c["bg"],
                        arrowcolor=c["text_muted"], width=12)
        style.map("Vertical.TScrollbar",
                  background=[("active", c["border_strong"])])
        style.configure("Horizontal.TScrollbar", background=c["surface_alt"],
                        troughcolor=c["bg"], bordercolor=c["bg"],
                        arrowcolor=c["text_muted"])

    # -- helpers ----------------------------------------------------------

    def toggle_dark(self):
        """Flip between the light and dark palettes and restyle everything."""
        self.dark = not self.dark
        self.colors = dict(DARK if self.dark else LIGHT)
        self.apply()
        return self.dark


def install(root, dark=None, scale=1.0):
    """Create and apply a :class:`Theme` for this root window."""
    return Theme(root, dark=dark, scale=scale)


__all__ = ["Theme", "install", "enable_dpi_awareness", "system_prefers_dark",
           "LIGHT", "DARK", "PAD_XS", "PAD_S", "PAD_M", "PAD_L", "PAD_XL"]
