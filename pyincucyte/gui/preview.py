"""The thumbnail window: a scrollable wall of wells, captioned.

Kept apart from :mod:`pyincucyte.gui.app` because it has to work on its own -
called from a script or a notebook there is no application to hang it off, so
it will build a Tk root, show itself, and clean up after.  Inside the running
app it is just another Toplevel and returns immediately.
"""

import base64
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import theme as theme_mod
from .widgets import tip

#: Space around and between tiles, in pixels.
TILE_PAD = 10

#: How many columns to start with; the grid reflows to fit the window.
DEFAULT_COLUMNS = 3


def _photo_image(array, master=None):
    """Turn a uint8 array into something Tk can draw.

    Pillow's ImageTk is the fast path; where it is missing (a Pillow built
    without Tk support) a PNG through ``tk.PhotoImage`` gets there too.
    """
    from PIL import Image

    image = Image.fromarray(array)
    try:
        from PIL import ImageTk

        return ImageTk.PhotoImage(image, master=master)
    except Exception:
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return tk.PhotoImage(
            master=master, data=base64.b64encode(buffer.getvalue()).decode("ascii"))


class PreviewWindow(tk.Toplevel):
    """A scrolling grid of well thumbnails with a caption under each."""

    def __init__(self, parent, theme, preview_set, title=None,
                 columns=DEFAULT_COLUMNS):
        super().__init__(parent)
        self.theme = theme
        self.preview = preview_set
        self.photos = []          # Tk drops an image the moment nobody holds it
        self.tiles = []
        self._columns = 0
        self._resize_job = None

        self.title(title or preview_set.title or "Preview")
        self.configure(background=theme["bg"])
        # On Windows a transient window of a hidden master is hidden with it,
        # and the master is hidden whenever this was opened from a script.
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)
        self.bind("<Escape>", lambda _event: self.destroy())

        self._build(columns)
        self._size_to_content(columns)

    # -- layout -----------------------------------------------------------

    def _build(self, columns):
        header = ttk.Frame(self, style="Toolbar.TFrame", padding=theme_mod.PAD_M)
        header.pack(side="top", fill="x")
        ttk.Label(header, text=self.preview.title,
                  style="Heading.TLabel").pack(side="left")
        if len(self.preview.scans) == 1 and self.preview.scans[0].elapsed:
            ttk.Label(header, text=f"+{self.preview.scans[0].elapsed}",
                      style="Muted.TLabel").pack(side="left",
                                                 padx=(theme_mod.PAD_S, 0))
        ttk.Button(header, text="Close", command=self.destroy).pack(side="right")
        save = ttk.Button(header, text="Save PNGs...", command=self._save)
        save.pack(side="right", padx=theme_mod.PAD_S)
        tip(save, "Write these thumbnails to a folder as PNGs. They are "
                  "contrast-stretched previews, not data.", self.theme)

        status = ttk.Frame(self, style="Toolbar.TFrame",
                           padding=(theme_mod.PAD_M, theme_mod.PAD_XS))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, text=self.preview.summary(),
                  style="Muted.TLabel").pack(side="left")

        body = ttk.Frame(self, style="TFrame")
        body.pack(side="top", fill="both", expand=True)

        self.canvas = tk.Canvas(body, background=self.theme["bg"],
                                highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.grid_frame = ttk.Frame(self.canvas, style="TFrame")
        self._window = self.canvas.create_window(
            (0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for widget in (self.canvas, self.grid_frame):
            widget.bind("<MouseWheel>", self._on_wheel)
            widget.bind("<Button-4>", self._on_wheel)
            widget.bind("<Button-5>", self._on_wheel)
        self.bind("<MouseWheel>", self._on_wheel)

        self._make_tiles()
        self._reflow(columns)

    def _make_tiles(self):
        """One card per image, built once and re-gridded on resize."""
        if not self.preview.images:
            ttk.Label(self.grid_frame, style="MutedBg.TLabel",
                      text="Nothing to show - the scan holds no images for "
                           "the wells that were asked for.").grid(
                row=0, column=0, padx=theme_mod.PAD_L, pady=theme_mod.PAD_L)
            return

        # When every tile came from the same moment, the header already says
        # when that was; repeating it under 24 thumbnails is just noise.
        many_times = len({image.scan_time for image in self.preview.images}) > 1

        for image in self.preview.images:
            tile = ttk.Frame(self.grid_frame, style="Card.TFrame",
                             padding=theme_mod.PAD_S)
            if image.ok:
                photo = _photo_image(image.array, master=self)
                self.photos.append(photo)
                holder = ttk.Label(tile, image=photo, style="Surface.TLabel")
            else:
                holder = ttk.Label(
                    tile, style="Muted.TLabel", justify="center",
                    wraplength=self.preview.size,
                    text=f"no image\n{image.error[:120]}")
                holder.configure(padding=theme_mod.PAD_L)
            holder.pack()
            caption = ttk.Label(tile, text=image.label, style="Accent.TLabel")
            caption.pack(anchor="w", pady=(theme_mod.PAD_XS, 0))
            if many_times:
                stamp = (image.scan_time or "")[:16].replace("T", " ")
                ttk.Label(tile, text=f"{stamp}  +{image.elapsed}".strip(),
                          style="Muted.TLabel").pack(anchor="w")
            for widget in (tile, holder, caption):
                widget.bind("<MouseWheel>", self._on_wheel)
            self.tiles.append(tile)

    def _reflow(self, columns):
        """Re-grid the tiles into ``columns`` columns."""
        columns = max(1, int(columns))
        if columns == self._columns or not self.tiles:
            return
        self._columns = columns
        for index, tile in enumerate(self.tiles):
            tile.grid(row=index // columns, column=index % columns,
                      padx=TILE_PAD // 2, pady=TILE_PAD // 2, sticky="n")

    def _tile_width(self):
        return int(self.preview.size) + TILE_PAD + 4 * theme_mod.PAD_S

    def _size_to_content(self, columns):
        """Open wide enough for ``columns`` tiles, but never off the screen."""
        columns = max(1, min(int(columns), max(1, len(self.tiles))))
        width = columns * self._tile_width() + 3 * theme_mod.PAD_L
        rows = max(1, -(-len(self.tiles) // columns))
        height = min(rows, 2) * (self.preview.size + 90) + 90
        try:
            width = min(width, self.winfo_screenwidth() - 80)
            height = min(height, self.winfo_screenheight() - 140)
        except tk.TclError:
            pass
        self.geometry(f"{int(width)}x{int(height)}")
        self.minsize(self._tile_width() + 40, 240)

    # -- events -----------------------------------------------------------

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        # Reflowing on every pixel of a drag is visibly slow; one settle is enough.
        self._resize_job = self.after(
            60, lambda: self._reflow(max(1, event.width // self._tile_width())))

    def _on_wheel(self, event):
        delta = event.delta
        if delta == 0:                      # X11 sends buttons, not a delta
            delta = 120 if getattr(event, "num", 5) == 4 else -120
        self.canvas.yview_scroll(int(-delta / 120), "units")

    def _save(self):
        folder = filedialog.askdirectory(parent=self, title="Save previews to")
        if not folder:
            return
        try:
            paths = self.preview.save(folder)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        messagebox.showinfo("Saved", f"{len(paths)} PNGs written to\n{folder}",
                            parent=self)


def show_preview(preview_set, parent=None, title=None, block=None, dark=None):
    """Show a :class:`~pyincucyte.preview.PreviewSet` in its own window.

    With no Tk application running this builds one and blocks until the window
    is closed - what a script wants.  Inside the desktop app it returns at once,
    so the app stays responsive.  ``block`` overrides either default.
    """
    root = parent if parent is not None else tk._default_root
    owns_root = root is None
    if owns_root:
        scale = theme_mod.enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        theme = theme_mod.Theme(root, dark=dark, scale=scale)
    else:
        theme = getattr(root, "_pyincucyte_theme", None)
        if theme is None:
            theme = theme_mod.Theme(root, dark=dark)

    window = PreviewWindow(root, theme, preview_set, title=title)
    if block is None:
        block = owns_root
    if owns_root:
        window.protocol("WM_DELETE_WINDOW", root.destroy)
        window.bind("<Escape>", lambda _event: root.destroy())
    if block:
        if owns_root:
            root.mainloop()
        else:
            window.wait_window()
    return window


__all__ = ["PreviewWindow", "show_preview", "TILE_PAD", "DEFAULT_COLUMNS"]
