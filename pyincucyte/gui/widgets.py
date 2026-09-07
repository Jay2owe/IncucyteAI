"""Reusable widgets for the desktop app.

The plate picker is the important one.  The old version created one Tk button
per well, which is 384 widgets on a large plate - slow to build and impossible
to style.  :class:`WellPlate` draws the whole plate on a single canvas, which
makes drag-painting, hover feedback and dimming of never-scanned wells cheap.
"""

import tkinter as tk
from datetime import datetime
from tkinter import ttk

from . import theme as theme_mod

# ---------------------------------------------------------------------------
# tooltip
# ---------------------------------------------------------------------------


class Tooltip:
    """A small delayed hover label. Explains an option without a manual."""

    def __init__(self, widget, text, theme=None, delay=450, wraplength=280):
        self.widget = widget
        self.text = text
        self.theme = theme
        self.delay = delay
        self.wraplength = wraplength
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text):
        self.text = text

    def _schedule(self, _event=None):
        self._cancel()
        if self.text:
            self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        if self._window or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        colors = self.theme.colors if self.theme else theme_mod.LIGHT
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        try:
            self._window.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(self._window, background=colors["border_strong"], bd=0)
        frame.pack()
        label = tk.Label(
            frame, text=self.text, justify="left", wraplength=self.wraplength,
            background=colors["surface"], foreground=colors["text"],
            padx=9, pady=6, bd=0,
            font=(self.theme.family if self.theme else "Segoe UI", 8))
        label.pack(padx=1, pady=1)

    def _hide(self, _event=None):
        self._cancel()
        if self._window:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


def tip(widget, text, theme=None):
    """Attach a tooltip and return the widget, so it can be used inline."""
    Tooltip(widget, text, theme=theme)
    return widget


# ---------------------------------------------------------------------------
# card
# ---------------------------------------------------------------------------

class Card(ttk.Frame):
    """A titled panel on a light surface - the app's basic building block."""

    def __init__(self, parent, title=None, theme=None, padding=None, **kwargs):
        super().__init__(parent, style="Card.TFrame", **kwargs)
        self.theme = theme
        pad = theme_mod.PAD_L if padding is None else padding
        self.header = None
        self.title_var = tk.StringVar(value=title or "")

        self.body = ttk.Frame(self, style="Surface.TFrame")
        self._body_pack = {
            "fill": "both",
            "expand": True,
            "padx": pad,
            "pady": (theme_mod.PAD_M if title is not None else pad, pad),
        }
        self.body_visible = True
        if title is not None:
            self.header = ttk.Frame(self, style="Surface.TFrame")
            self.header.pack(fill="x", padx=pad, pady=(pad, 0))
            self.title_label = ttk.Label(
                self.header, textvariable=self.title_var, style="CardTitle.TLabel")
            self.title_label.pack(side="left")
            self.actions = ttk.Frame(self.header, style="Surface.TFrame")
            self.actions.pack(side="right")
        self.body.pack(**self._body_pack)

    def set_title(self, text):
        self.title_var.set(text)

    def set_body_visible(self, visible):
        """Show or fold the content while leaving the card header visible."""
        self.body_visible = bool(visible)
        if self.body_visible:
            self.body.pack(**self._body_pack)
        else:
            self.body.pack_forget()


# ---------------------------------------------------------------------------
# well plate
# ---------------------------------------------------------------------------

class WellPlate(ttk.Frame):
    """A clickable plate map drawn on one canvas.

    Click toggles a well; dragging paints whichever state the first well was
    set to; row and column headers toggle a whole line; shift-click selects the
    rectangle between the last click and this one.
    """

    def __init__(self, parent, theme, rows=8, cols=12, on_change=None,
                 min_cell=13, max_cell=30, **kwargs):
        super().__init__(parent, style="Surface.TFrame", **kwargs)
        self.theme = theme
        self.rows = rows
        self.cols = cols
        self.on_change = on_change
        self.min_cell = min_cell
        self.max_cell = max_cell

        self.selected = set()
        self.available = None     # None = unknown, else wells that hold data
        self._paint_value = None
        self._anchor = None
        self._hover = None
        self._cell_ids = {}
        self._geometry = (0, 0, 0, 0)  # cell, gap, left, top

        self.canvas = tk.Canvas(
            self, height=180, highlightthickness=0, bd=0,
            background=theme["surface"], cursor="hand2")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Shift-Button-1>", self._on_shift_press)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))

    # -- public API -------------------------------------------------------

    def configure_plate(self, rows, cols, selection=None, available=None):
        """Switch to a different plate format, keeping or replacing selection."""
        self.rows, self.cols = int(rows), int(cols)
        self.available = set(available) if available is not None else None
        if selection is None:
            self.selected = self.all_wells()
        else:
            self.selected = {(r, c) for r, c in selection
                             if 0 <= r < self.rows and 0 <= c < self.cols}
        self._anchor = None
        self.redraw()
        self._changed()

    def all_wells(self):
        return {(r, c) for r in range(self.rows) for c in range(self.cols)}

    @property
    def selection(self):
        return set(self.selected)

    def set_selection(self, wells, notify=True):
        self.selected = {(r, c) for r, c in (wells or set())
                         if 0 <= r < self.rows and 0 <= c < self.cols}
        self.redraw()
        if notify:
            self._changed()

    def select_all(self):
        self.set_selection(self.all_wells())

    def clear(self):
        self.set_selection(set())

    def invert(self):
        self.set_selection(self.all_wells() - self.selected)

    def select_scanned(self):
        """Keep only wells the instrument actually imaged, when we know them."""
        if self.available is None:
            self.select_all()
        else:
            self.set_selection(set(self.available))

    @property
    def count(self):
        return len(self.selected)

    @property
    def total(self):
        return self.rows * self.cols

    # -- drawing ----------------------------------------------------------

    def preferred_height(self):
        cell, gap, _left, top = self._compute_geometry(
            self.canvas.winfo_width() or 480)
        return int(top + self.rows * (cell + gap) + gap)

    def _compute_geometry(self, width, height=None):
        label = max(16, int(11 * self.theme.scale))
        usable = max(60, width - label - 8)
        gap = 2
        cell = int((usable - gap * (self.cols + 1)) / max(1, self.cols))
        cell = max(self.min_cell, min(self.max_cell, cell))
        if height is not None and height > label + gap:
            vertical = int(
                (height - label - gap * (self.rows + 1)) / max(1, self.rows))
            # A compact window must show every well, even when that requires
            # going below the normal mouse-target size.
            cell = max(8, min(cell, vertical))
        # Centre the plate when it does not fill the card, so a 6-well vessel
        # does not sit in the corner of a wide panel.
        plate_width = self.cols * (cell + gap) + gap
        left = label + gap + max(0, (usable - plate_width) // 2)
        return cell, gap, left, label + gap

    def redraw(self):
        canvas = self.canvas
        canvas.delete("all")
        self._cell_ids = {}
        width = canvas.winfo_width()
        if width <= 1:
            self.after(30, self.redraw)
            return

        c = self.theme.colors
        cell, gap, left, top = self._compute_geometry(
            width, canvas.winfo_height())
        self._geometry = (cell, gap, left, top)
        font = (self.theme.family, max(6, int(cell * 0.42)))
        label_width = max(16, int(11 * self.theme.scale))

        for col in range(self.cols):
            x = left + col * (cell + gap) + cell / 2
            canvas.create_text(x, top / 2, text=str(col + 1), font=font,
                               fill=c["grid_label"], tags=("colhead", f"col{col}"))
        for row in range(self.rows):
            y = top + row * (cell + gap) + cell / 2
            # Sit the letter against the first column, not in the middle of
            # whatever margin centring happened to leave.
            canvas.create_text(left - gap - label_width / 2, y,
                               text=chr(65 + row), font=font,
                               fill=c["grid_label"], tags=("rowhead", f"row{row}"))

        radius = max(2, cell // 4)
        for row in range(self.rows):
            for col in range(self.cols):
                x0 = left + col * (cell + gap)
                y0 = top + row * (cell + gap)
                item = self._rounded_rect(x0, y0, x0 + cell, y0 + cell, radius,
                                          fill=self._fill(row, col), outline="")
                self._cell_ids[(row, col)] = item

        height = int(top + self.rows * (cell + gap) + gap)
        canvas.configure(scrollregion=(0, 0, width, height))

    def _rounded_rect(self, x0, y0, x1, y1, r, **kwargs):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def apply_theme(self, theme):
        """Repaint with a new palette (used by the dark-mode toggle)."""
        self.theme = theme
        self.canvas.configure(background=theme["surface"])
        self.redraw()

    def _fill(self, row, col):
        c = self.theme.colors
        selected = (row, col) in self.selected
        hovered = self._hover == (row, col)
        if self.available is not None and (row, col) not in self.available:
            return c["well_on_hover"] if (selected and hovered) else (
                c["well_on"] if selected else c["well_empty"])
        if selected:
            return c["well_on_hover"] if hovered else c["well_on"]
        return c["well_off_hover"] if hovered else c["well_off"]

    def _repaint(self, cells):
        for cell in cells:
            item = self._cell_ids.get(cell)
            if item:
                self.canvas.itemconfigure(item, fill=self._fill(*cell))

    # -- interaction ------------------------------------------------------

    def _cell_at(self, x, y):
        cell, gap, left, top = self._geometry
        if not cell or x < left or y < top:
            return None
        col = int((x - left) // (cell + gap))
        row = int((y - top) // (cell + gap))
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return (row, col)
        return None

    def _header_at(self, x, y):
        cell, gap, left, top = self._geometry
        if not cell:
            return None
        if y < top and x >= left:
            col = int((x - left) // (cell + gap))
            if 0 <= col < self.cols:
                return ("col", col)
        label_width = max(16, int(11 * self.theme.scale))
        if left - gap - label_width <= x < left and y >= top:
            row = int((y - top) // (cell + gap))
            if 0 <= row < self.rows:
                return ("row", row)
        return None

    def _on_press(self, event):
        header = self._header_at(event.x, event.y)
        if header:
            self._toggle_line(*header)
            return
        cell = self._cell_at(event.x, event.y)
        if cell is None:
            return
        self._paint_value = cell not in self.selected
        self._anchor = cell
        self._apply(cell, self._paint_value)
        self._changed()

    def _on_shift_press(self, event):
        cell = self._cell_at(event.x, event.y)
        if cell is None or self._anchor is None:
            return self._on_press(event)
        r0, c0 = self._anchor
        r1, c1 = cell
        block = {(r, c)
                 for r in range(min(r0, r1), max(r0, r1) + 1)
                 for c in range(min(c0, c1), max(c0, c1) + 1)}
        self.selected |= block
        self._repaint(block)
        self._changed()
        return "break"

    def _on_drag(self, event):
        if self._paint_value is None:
            return
        cell = self._cell_at(event.x, event.y)
        if cell is None:
            return
        if (cell in self.selected) != self._paint_value:
            self._apply(cell, self._paint_value)
            self._changed()

    def _on_release(self, _event):
        self._paint_value = None

    def _on_motion(self, event):
        self._set_hover(self._cell_at(event.x, event.y))

    def _set_hover(self, cell):
        if cell == self._hover:
            return
        previous, self._hover = self._hover, cell
        self._repaint([c for c in (previous, cell) if c])

    def _apply(self, cell, value):
        if value:
            self.selected.add(cell)
        else:
            self.selected.discard(cell)
        self._repaint([cell])

    def _toggle_line(self, kind, index):
        cells = ({(index, c) for c in range(self.cols)} if kind == "row"
                 else {(r, index) for r in range(self.rows)})
        if cells <= self.selected:
            self.selected -= cells
        else:
            self.selected |= cells
        self._repaint(cells)
        self._changed()

    def _changed(self):
        if self.on_change:
            self.on_change(self.selection)


# ---------------------------------------------------------------------------
# log view
# ---------------------------------------------------------------------------

LOG_LEVELS = ("info", "success", "warn", "error", "muted")


class LogView(ttk.Frame):
    """A timestamped, colour-coded activity log."""

    def __init__(self, parent, theme, height=8, **kwargs):
        super().__init__(parent, style="Surface.TFrame", **kwargs)
        self.theme = theme
        self.autoscroll = tk.BooleanVar(value=True)

        c = theme.colors
        self.text = tk.Text(
            self, height=height, wrap="word", state="disabled", bd=0,
            highlightthickness=0, padx=10, pady=8,
            background=c["surface"], foreground=c["text"],
            insertbackground=c["text"], font=theme.font_mono,
            selectbackground=c["accent_soft"], selectforeground=c["text"])
        scrollbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

        self.text.tag_configure("time", foreground=c["text_muted"])
        self.text.tag_configure("info", foreground=c["text"])
        self.text.tag_configure("muted", foreground=c["text_muted"])
        self.text.tag_configure("success", foreground=c["success"])
        self.text.tag_configure("warn", foreground=c["warning"])
        self.text.tag_configure("error", foreground=c["danger"])

    def apply_theme(self, theme):
        """Recolour the text widget and its severity tags for a new palette."""
        self.theme = theme
        c = theme.colors
        self.text.configure(background=c["surface"], foreground=c["text"],
                            insertbackground=c["text"],
                            selectbackground=c["accent_soft"],
                            selectforeground=c["text"])
        for tag, colour in (("time", c["text_muted"]), ("info", c["text"]),
                            ("muted", c["text_muted"]), ("success", c["success"]),
                            ("warn", c["warning"]), ("error", c["danger"])):
            self.text.tag_configure(tag, foreground=colour)

    def write(self, message, level="info"):
        level = level if level in LOG_LEVELS else "info"
        self.text.configure(state="normal")
        self.text.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.text.insert("end", f"{message}\n", level)
        if self.autoscroll.get():
            self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def contents(self):
        return self.text.get("1.0", "end").rstrip()

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.contents())


# ---------------------------------------------------------------------------
# status bar
# ---------------------------------------------------------------------------

class StatusBar(ttk.Frame):
    """Bottom strip: what is happening, how far along, and a way to stop it.

    Progress lives here rather than in a modal dialog, so the window stays
    usable while a long download runs.
    """

    def __init__(self, parent, theme, on_cancel=None, **kwargs):
        super().__init__(parent, style="Toolbar.TFrame", **kwargs)
        self.theme = theme
        self.on_cancel = on_cancel

        self.message_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value="")
        self.rate_var = tk.StringVar(value="")

        inner = ttk.Frame(self, style="Toolbar.TFrame")
        inner.pack(fill="x", padx=theme_mod.PAD_L, pady=theme_mod.PAD_M)

        ttk.Label(inner, textvariable=self.message_var, style="Surface.TLabel",
                  width=34, anchor="w").pack(side="left")

        self.progress = ttk.Progressbar(inner, mode="determinate", maximum=100,
                                        length=int(220 * theme.scale))
        self.progress.pack(side="left", padx=(theme_mod.PAD_M, theme_mod.PAD_M))

        ttk.Label(inner, textvariable=self.detail_var, style="Muted.TLabel",
                  anchor="w").pack(side="left")

        self.cancel_btn = ttk.Button(inner, text="Cancel", style="Danger.TButton",
                                     command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        ttk.Label(inner, textvariable=self.rate_var, style="Muted.TLabel",
                  anchor="e").pack(side="right", padx=(0, theme_mod.PAD_M))

        self._busy = False

    def _cancel(self):
        if self.on_cancel:
            self.on_cancel()

    def set_message(self, message, detail="", rate=""):
        self.message_var.set(message)
        self.detail_var.set(detail)
        self.rate_var.set(rate)

    def set_progress(self, done, total):
        if total:
            self.stop_indeterminate()
            self.progress.configure(maximum=total, value=done)
        else:
            self.start_indeterminate()

    def start_indeterminate(self):
        if self.progress["mode"] != "indeterminate":
            self.progress.configure(mode="indeterminate")
            self.progress.start(14)

    def stop_indeterminate(self):
        if self.progress["mode"] == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def set_busy(self, busy):
        self._busy = busy
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self.stop_indeterminate()
            self.progress.configure(value=0)

    def reset(self, message="Ready"):
        self.set_busy(False)
        self.set_message(message)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

class LabeledValue(ttk.Frame):
    """A caption above a value - the readable way to show a summary number."""

    def __init__(self, parent, caption, value="-", **kwargs):
        super().__init__(parent, style="Surface.TFrame", **kwargs)
        self.value_var = tk.StringVar(value=value)
        ttk.Label(self, text=caption.upper(), style="Muted.TLabel").pack(anchor="w")
        ttk.Label(self, textvariable=self.value_var,
                  style="Value.TLabel").pack(anchor="w")

    def set(self, value):
        self.value_var.set(value)


class SearchEntry(ttk.Frame):
    """An entry with placeholder text and a clear button."""

    def __init__(self, parent, placeholder="Search", on_change=None, width=24,
                 **kwargs):
        super().__init__(parent, style="Surface.TFrame", **kwargs)
        self.var = tk.StringVar()
        self.on_change = on_change
        self.placeholder = placeholder
        self._showing_placeholder = True

        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left")
        self.clear_btn = ttk.Button(self, text="x", width=2, style="Ghost.TButton",
                                    command=self.clear)
        self.clear_btn.pack(side="left", padx=(theme_mod.PAD_XS, 0))

        self.var.trace_add("write", self._changed)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Escape>", lambda e: self.clear())
        self._focus_out()

    @property
    def value(self):
        return "" if self._showing_placeholder else self.var.get().strip()

    def clear(self):
        self._showing_placeholder = False
        self.var.set("")
        self._focus_out()

    def _focus_in(self, _event=None):
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.var.set("")
            self.entry.configure(foreground="")

    def _focus_out(self, _event=None):
        if not self.var.get().strip():
            self._showing_placeholder = True
            self.var.set(self.placeholder)

    def _changed(self, *_args):
        if self.on_change and not self._showing_placeholder:
            self.on_change(self.value)


__all__ = ["Tooltip", "tip", "Card", "WellPlate", "LogView", "StatusBar",
           "LabeledValue", "SearchEntry"]
