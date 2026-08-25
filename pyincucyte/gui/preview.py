"""The thumbnail window: a scrollable wall of wells, captioned.

Kept apart from :mod:`pyincucyte.gui.app` because it has to work on its own -
called from a script or a notebook there is no application to hang it off, so
it will build a Tk root, show itself, and clean up after.  Inside the running
app it is just another Toplevel and returns immediately.
"""

import base64
import queue
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
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


class TimelineWindow(tk.Toplevel):
    """One bounded image canvas with lazy time, well, and channel controls."""

    def __init__(self, parent, theme, timeline, title=None):
        super().__init__(parent)
        self.theme = theme
        self.timeline = timeline
        self.source = timeline.source
        self.photo = None               # exactly one live Tk image reference
        self.current_array = None
        self.current_index = 0
        self._generation = 0
        self._slider_job = None
        self._play_job = None
        self._poll_job = None
        self._closing = False
        self._cleanup_started = False
        self._cancel = threading.Event()
        self._results = queue.Queue()
        self._workers = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pyincucyte-preview")

        self.title(title or timeline.title)
        self.configure(background=theme["bg"])
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Left>", lambda _event: self._step(-1))
        self.bind("<Right>", lambda _event: self._step(1))
        self.bind("<space>", lambda _event: self._toggle_play())

        self._build()
        self.geometry("720x680")
        self.minsize(520, 480)
        self._poll_job = self.after(40, self._poll_results)
        self._request_current()

    def _build(self):
        header = ttk.Frame(self, style="Toolbar.TFrame", padding=theme_mod.PAD_M)
        header.pack(side="top", fill="x")
        ttk.Label(header, text=self.timeline.title,
                  style="Heading.TLabel").pack(side="left")
        ttk.Button(header, text="Close", command=self._close).pack(side="right")
        save = ttk.Button(header, text="Save frame...", command=self._save_frame)
        save.pack(side="right", padx=theme_mod.PAD_S)
        tip(save, "Save the displayed, contrast-stretched preview as PNG. "
                  "It is not quantitative data.", self.theme)

        selectors = ttk.Frame(self, padding=(theme_mod.PAD_M, theme_mod.PAD_S))
        selectors.pack(side="top", fill="x")
        selectors.columnconfigure(1, weight=1)
        selectors.columnconfigure(3, weight=1)
        selectors.columnconfigure(5, weight=1)

        self.well_var = tk.StringVar(value=self.timeline.well)
        self.channel_var = tk.StringVar(
            value=self.source.channel_labels.get(self.timeline.channel,
                                                  str(self.timeline.channel)))
        self.site_var = tk.StringVar(value=f"Site {self.timeline.site + 1}")
        self.contrast_var = tk.StringVar(value=self.timeline.contrast)
        self._channels_by_label = {
            self.source.channel_labels.get(number, str(number)): number
            for number in self.source.available_channels
        }
        self._sites_by_label = {
            f"Site {number + 1}": number for number in self.source.available_sites
        }

        ttk.Label(selectors, text="Well").grid(row=0, column=0, sticky="w")
        well = ttk.Combobox(
            selectors, textvariable=self.well_var,
            values=self.source.available_wells, state="readonly", width=10)
        well.grid(row=0, column=1, sticky="ew", padx=(theme_mod.PAD_XS,
                                                       theme_mod.PAD_M))
        ttk.Label(selectors, text="Channel").grid(row=0, column=2, sticky="w")
        channel = ttk.Combobox(
            selectors, textvariable=self.channel_var,
            values=list(self._channels_by_label), state="readonly", width=16)
        channel.grid(row=0, column=3, sticky="ew", padx=(theme_mod.PAD_XS,
                                                          theme_mod.PAD_M))
        ttk.Label(selectors, text="Site").grid(row=0, column=4, sticky="w")
        site = ttk.Combobox(
            selectors, textvariable=self.site_var,
            values=list(self._sites_by_label), state="readonly", width=9)
        site.grid(row=0, column=5, sticky="ew", padx=(theme_mod.PAD_XS, 0))

        ttk.Label(selectors, text="Contrast").grid(
            row=1, column=0, sticky="w", pady=(theme_mod.PAD_S, 0))
        contrast = ttk.Combobox(
            selectors, textvariable=self.contrast_var,
            values=("auto", "minmax", "raw"), state="readonly", width=10)
        contrast.grid(row=1, column=1, sticky="w",
                      padx=(theme_mod.PAD_XS, theme_mod.PAD_M),
                      pady=(theme_mod.PAD_S, 0))
        for widget in (well, channel, site, contrast):
            widget.bind("<<ComboboxSelected>>",
                        lambda _event: self._request_current())

        body = ttk.Frame(self, style="Card.TFrame", padding=theme_mod.PAD_S)
        body.pack(side="top", fill="both", expand=True,
                  padx=theme_mod.PAD_M, pady=(0, theme_mod.PAD_S))
        self.canvas = tk.Canvas(body, background=self.theme["surface"],
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self._image_item = self.canvas.create_image(0, 0, anchor="center")
        self._message_item = self.canvas.create_text(
            0, 0, text="Loading...", fill=self.theme["muted"],
            anchor="center", justify="center")
        self.canvas.bind("<Configure>", self._centre_canvas)

        controls = ttk.Frame(self, padding=(theme_mod.PAD_M, theme_mod.PAD_XS))
        controls.pack(side="top", fill="x")
        ttk.Button(controls, text="Previous", command=lambda: self._step(-1)).pack(
            side="left")
        self.play_button = ttk.Button(controls, text="Play",
                                      command=self._toggle_play)
        self.play_button.pack(side="left", padx=theme_mod.PAD_S)
        ttk.Button(controls, text="Next", command=lambda: self._step(1)).pack(
            side="left")
        self.position_var = tk.IntVar(value=0)
        self.slider = ttk.Scale(
            controls, from_=0, to=max(0, self.source.frame_count - 1),
            variable=self.position_var, command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True,
                         padx=theme_mod.PAD_M)
        self.position_label = ttk.Label(controls, style="Muted.TLabel", width=18,
                                        anchor="e")
        self.position_label.pack(side="right")

        status = ttk.Frame(self, style="Toolbar.TFrame",
                           padding=(theme_mod.PAD_M, theme_mod.PAD_S))
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value=self.timeline.summary())
        ttk.Label(status, textvariable=self.status_var,
                  style="Muted.TLabel").pack(side="left")

    def _selection(self):
        return (self.well_var.get(),
                self._sites_by_label.get(self.site_var.get(), 0),
                self._channels_by_label.get(self.channel_var.get(),
                                            self.timeline.channel),
                self.contrast_var.get())

    def _centre_canvas(self, event=None):
        width = event.width if event is not None else self.canvas.winfo_width()
        height = event.height if event is not None else self.canvas.winfo_height()
        centre = (max(1, width) // 2, max(1, height) // 2)
        self.canvas.coords(self._image_item, *centre)
        self.canvas.coords(self._message_item, *centre)

    def _invalidate(self):
        self._generation += 1
        return self._generation

    def _on_slider(self, value):
        if self._closing:
            return
        generation = self._invalidate()
        index = max(0, min(self.source.frame_count - 1,
                           int(round(float(value)))))
        self.position_label.configure(
            text=f"{index + 1:,} / {self.source.frame_count:,}")
        if self._slider_job is not None:
            self.after_cancel(self._slider_job)
        self._slider_job = self.after(
            80, lambda: self._request_index(index, generation))

    def _request_current(self):
        if self._closing:
            return
        index = max(0, min(self.source.frame_count - 1,
                           int(self.position_var.get())))
        self._request_index(index, self._invalidate())

    def _request_index(self, index, generation):
        self._slider_job = None
        if self._closing or generation != self._generation:
            return
        selection = self._selection()
        self.current_index = index
        self.position_var.set(index)
        self.position_label.configure(
            text=f"{index + 1:,} / {self.source.frame_count:,}")
        self.canvas.itemconfigure(self._message_item, text="Loading...", state="normal")
        self.status_var.set(self.source.frame_label(index))
        self._workers.submit(self._load_frame, generation, index, selection)

    def _load_frame(self, generation, index, selection):
        well, site, channel, contrast = selection
        try:
            array = self.source.render_frame(
                well, site, channel, index, size=self.timeline.size,
                contrast=contrast, cancel=self._cancel)
            info = self.source.frame_info(well, site, channel, index)
            self._results.put((generation, index, selection, array, info, ""))
        except Exception as exc:
            self._results.put((generation, index, selection, None, None, str(exc)))
            return
        if not self._cancel.is_set():
            self.source.prefetch(well, site, channel,
                                 self.source.neighbours(index),
                                 cancel=self._cancel)

    def _poll_results(self):
        if self._closing:
            return
        try:
            while True:
                generation, index, selection, array, info, error = (
                    self._results.get_nowait())
                if (generation != self._generation
                        or selection != self._selection()):
                    continue
                if error:
                    self.photo = None
                    self.current_array = None
                    self.canvas.itemconfigure(self._image_item, image="")
                    self.canvas.itemconfigure(
                        self._message_item, text=f"No image\n{error[:180]}",
                        state="normal")
                    self.status_var.set(error)
                    continue
                self.current_array = array
                self.photo = _photo_image(array, master=self)
                self.canvas.itemconfigure(self._image_item, image=self.photo)
                self.canvas.itemconfigure(self._message_item, state="hidden")
                source = info.source if info is not None else "preview"
                size = info.source_bytes if info is not None else 0
                self.status_var.set(
                    f"{self.source.frame_label(index)} - {source} - {size:,} bytes")
        except queue.Empty:
            pass
        self._poll_job = self.after(40, self._poll_results)

    def _step(self, amount):
        if self.source.frame_count <= 0:
            return
        index = max(0, min(self.source.frame_count - 1,
                           int(self.position_var.get()) + int(amount)))
        self.position_var.set(index)
        self._request_current()

    def _toggle_play(self):
        if self._play_job is not None:
            self.after_cancel(self._play_job)
            self._play_job = None
            self.play_button.configure(text="Play")
            return
        self.play_button.configure(text="Pause")
        self._play_tick()

    def _play_tick(self):
        if self._closing or self._play_job is None and self.play_button.cget("text") != "Pause":
            return
        index = int(self.position_var.get())
        if index >= self.source.frame_count - 1:
            self.position_var.set(0)
        else:
            self.position_var.set(index + 1)
        self._request_current()
        self._play_job = self.after(350, self._play_tick)

    def _save_frame(self):
        if self.current_array is None:
            messagebox.showinfo("Nothing to save", "Wait for a frame to load first.",
                                parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save preview frame", defaultextension=".png",
            filetypes=(("PNG image", "*.png"),))
        if not path:
            return
        try:
            from PIL import Image

            Image.fromarray(self.current_array).save(path, format="PNG")
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)

    def _close(self):
        if self._closing:
            return
        self._closing = True
        self._cancel.set()
        if self._slider_job is not None:
            self.after_cancel(self._slider_job)
        if self._play_job is not None:
            self.after_cancel(self._play_job)
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self.destroy()

        self._start_cleanup()

    def _on_destroy(self, event):
        """Release workers when the parent application destroys this window."""
        if event.widget is not self:
            return
        self._closing = True
        self._cancel.set()
        self._start_cleanup()

    def _start_cleanup(self):
        if self._cleanup_started:
            return
        self._cleanup_started = True

        def finish():
            self._workers.shutdown(wait=True, cancel_futures=True)
            self.timeline.close()

        threading.Thread(target=finish, name="pyincucyte-preview-close",
                         daemon=True).start()


def show_timeline(timeline, parent=None, title=None, block=None, dark=None):
    """Show a lazy :class:`~pyincucyte.timeline.TimelinePreview`."""
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
    window = TimelineWindow(root, theme, timeline, title=title)
    if block is None:
        block = owns_root
    if owns_root:
        window.protocol("WM_DELETE_WINDOW", lambda: (window._close(), root.destroy()))
        window.bind("<Escape>", lambda _event: (window._close(), root.destroy()))
    if block:
        if owns_root:
            root.mainloop()
        else:
            window.wait_window()
    return window


__all__ = [
    "PreviewWindow", "TimelineWindow", "show_preview", "show_timeline",
    "TILE_PAD", "DEFAULT_COLUMNS",
]
