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

from .. import channels as channel_mod
from . import theme as theme_mod
from .widgets import tip

#: Space around and between tiles, in pixels.
TILE_PAD = 10

#: How many columns to start with; the grid reflows to fit the window.
DEFAULT_COLUMNS = 3

#: Stable acquisition names.  Vessel-specific names remain visible in brackets,
#: but these three labels stop a green reporter being mistaken for the red path.
ACQUISITION_CHANNEL_LABELS = {
    channel_mod.PHASE: "Phase",
    channel_mod.GREEN: "Green",
    channel_mod.RED: "Red",
}


def preview_plate_shape(preview_set):
    """Return the physical ``(rows, columns)`` for a plate preview."""
    scans = list(getattr(preview_set, "scans", ()) or ())
    if scans:
        vessel = getattr(scans[0], "vessel", None)
        rows = int(getattr(vessel, "rows", 0) or 0)
        cols = int(getattr(vessel, "cols", 0) or 0)
        if rows > 0 and cols > 0:
            return rows, cols
    images = list(getattr(preview_set, "images", ()) or ())
    rows = max((int(image.row) for image in images), default=0) + 1
    cols = max((int(image.col) for image in images), default=0) + 1
    return max(1, rows), max(1, cols)


def preview_channel_options(preview_set):
    """Return ``[(image_type, label)]`` in Phase, Green, Red order."""
    numbers = {int(image.img_type)
               for image in (getattr(preview_set, "images", ()) or ())}
    scans = list(getattr(preview_set, "scans", ()) or ())
    for scan in scans:
        numbers.update(int(number)
                       for number in (getattr(scan, "channels", ()) or ()))
    vessel = getattr(scans[0], "vessel", None) if scans else None
    aliases = getattr(vessel, "channel_labels", {}) or {}
    options = []
    for number in sorted(numbers, key=channel_mod.image_type_sort_key):
        acquisition = ACQUISITION_CHANNEL_LABELS.get(
            number, channel_mod.image_type_label(number))
        alias = str(aliases.get(number, "") or "").strip()
        label = (f"{acquisition} ({alias})"
                 if alias and alias.casefold() != acquisition.casefold()
                 else acquisition)
        options.append((number, label))
    return options


def preview_site_options(preview_set):
    """Every image-position index available in this scan, in device order."""
    sites = {int(image.site)
             for image in (getattr(preview_set, "images", ()) or ())}
    for scan in (getattr(preview_set, "scans", ()) or ()):
        sites.update(int(site) for site in (getattr(scan, "sites", ()) or ()))
    return sorted(sites) or [0]


def preview_plane_images(images, channel, site):
    """One image per well for a selected channel and stack position."""
    by_position = {}
    for image in images or ():
        if int(image.img_type) != int(channel) or int(image.site) != int(site):
            continue
        # A multi-scan PreviewSet is newest-first.  Keep the first matching
        # image instead of laying several moments over the same plate slot.
        by_position.setdefault((int(image.row), int(image.col)), image)
    return [by_position[position] for position in sorted(by_position)]


def preferred_preview_channel(selected, available=()):
    """Choose Phase when selected, otherwise the first requested channel."""
    available = {int(number) for number in (available or ())}
    choices = {int(number) for number in (selected or ())}
    if available:
        choices &= available
    if not choices:
        choices = available
    if channel_mod.PHASE in choices:
        return channel_mod.PHASE
    ordered = sorted(choices, key=channel_mod.image_type_sort_key)
    return ordered[0] if ordered else channel_mod.PHASE


def _photo_image(array, master=None, max_size=None):
    """Turn a uint8 array into something Tk can draw.

    Pillow's ImageTk is the fast path; where it is missing (a Pillow built
    without Tk support) a PNG through ``tk.PhotoImage`` gets there too.
    """
    from PIL import Image

    image = Image.fromarray(array)
    if max_size:
        resampling = getattr(Image, "Resampling", Image)
        image.thumbnail((int(max_size), int(max_size)), resampling.LANCZOS)
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
    """A plate-shaped viewer with one channel and stack image on screen."""

    def __init__(self, parent, theme, preview_set, title=None,
                 columns=DEFAULT_COLUMNS):
        super().__init__(parent)
        self.theme = theme
        self.preview = preview_set
        self.photos = []          # Tk drops an image the moment nobody holds it
        self.tiles = []
        self._rows, self._cols = preview_plate_shape(preview_set)
        self._display_size = self._choose_display_size()
        self._channel_options = preview_channel_options(preview_set)
        self._channel_labels = dict(self._channel_options)
        self._sites = preview_site_options(preview_set)
        self._selected_wells = sorted({(int(image.row), int(image.col))
                                       for image in preview_set.images})
        self._plane_images = {}
        for image in preview_set.images:
            key = (int(image.img_type), int(image.site))
            self._plane_images.setdefault(key, []).append(image)
        initial_images = list(preview_set.images)
        self.current_channel = preferred_preview_channel(
            [image.img_type for image in initial_images],
            [number for number, _label in self._channel_options])
        initial_site = int(initial_images[0].site) if initial_images else self._sites[0]
        self._site_index = (self._sites.index(initial_site)
                            if initial_site in self._sites else 0)
        self._generation = 0
        self._slider_job = None
        self._poll_job = None
        self._load_cancel = threading.Event()
        self._results = queue.Queue()
        self._closing = False
        self._visible_images = []

        self.title(title or preview_set.title or "Preview")
        self.configure(background=theme["bg"])
        # On Windows a transient window of a hidden master is hidden with it,
        # and the master is hidden whenever this was opened from a script.
        if parent is not None and parent.winfo_viewable():
            self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Control-MouseWheel>", self._on_z_wheel)

        self._build(columns)
        self._size_to_content()
        self._poll_job = self.after(50, self._poll_results)
        self._show_selected_plane()

    # -- layout -----------------------------------------------------------

    def _build(self, _columns):
        header = ttk.Frame(self, style="Toolbar.TFrame", padding=theme_mod.PAD_M)
        header.pack(side="top", fill="x")
        header.columnconfigure(0, weight=1)
        heading = self.preview.title
        if len(self.preview.scans) == 1 and self.preview.scans[0].elapsed:
            heading += f"  +{self.preview.scans[0].elapsed}"
        ttk.Label(header, text=heading, style="Heading.TLabel",
                  anchor="w").grid(row=0, column=0, sticky="ew")
        save = ttk.Button(header, text="Save visible PNGs...", command=self._save)
        save.grid(row=0, column=1, padx=theme_mod.PAD_S)
        ttk.Button(header, text="Close", command=self._close).grid(
            row=0, column=2)
        tip(save, "Write the currently visible channel and stack image to a "
                  "folder as contrast-stretched PNG previews.", self.theme)

        selectors = ttk.Frame(self, style="Toolbar.TFrame",
                              padding=(theme_mod.PAD_M, 0, theme_mod.PAD_M,
                                       theme_mod.PAD_S))
        selectors.pack(side="top", fill="x")
        ttk.Label(selectors, text="Channel", style="Muted.TLabel").pack(
            side="left", padx=(0, theme_mod.PAD_S))
        self.channel_var = tk.IntVar(value=self.current_channel)
        for number, label in self._channel_options:
            ttk.Radiobutton(
                selectors, text=label, value=number,
                variable=self.channel_var, command=self._on_channel_change).pack(
                    side="left", padx=(0, theme_mod.PAD_M))

        stack = ttk.Frame(self, style="Toolbar.TFrame")
        stack.pack(side="top", fill="x",
                   padx=theme_mod.PAD_M, pady=(0, theme_mod.PAD_S))
        stack.columnconfigure(2, weight=1)
        ttk.Label(stack, text="Z-stack", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        self.previous_z = ttk.Button(
            stack, text="<", width=3, command=lambda: self._step_z(-1))
        self.previous_z.grid(row=0, column=1, padx=(theme_mod.PAD_S, 0))
        self.z_label_var = tk.StringVar()
        self.next_z = ttk.Button(
            stack, text=">", width=3, command=lambda: self._step_z(1))
        self.next_z.grid(row=0, column=3)
        ttk.Label(stack, textvariable=self.z_label_var, width=12, anchor="e",
                  style="Muted.TLabel").grid(
                      row=0, column=4, padx=(theme_mod.PAD_S, 0))
        self.z_position_var = tk.DoubleVar(value=self._site_index)
        self.z_scale = ttk.Scale(
            stack, from_=0, to=max(0, len(self._sites) - 1),
            variable=self.z_position_var, command=self._on_z_slider)
        self.z_scale.grid(row=0, column=2, sticky="ew", padx=theme_mod.PAD_S)
        self._update_z_label()
        if len(self._sites) <= 1:
            self.z_scale.state(["disabled"])
            self.previous_z.state(["disabled"])
            self.next_z.state(["disabled"])
        for widget in (self.z_scale, self.previous_z, self.next_z):
            widget.bind("<MouseWheel>", self._on_z_wheel)
            widget.bind("<Button-4>", self._on_z_wheel)
            widget.bind("<Button-5>", self._on_z_wheel)
        tip(self.z_scale, "Scroll here, use the arrow buttons, or press "
                          "Ctrl+mouse-wheel to move through the scan's image "
                          "positions.", self.theme)

        status = ttk.Frame(self, style="Toolbar.TFrame",
                           padding=(theme_mod.PAD_M, theme_mod.PAD_XS))
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var,
                  style="Muted.TLabel").pack(side="left")

        body = ttk.Frame(self, style="TFrame")
        body.pack(side="top", fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background=self.theme["bg"],
                                highlightthickness=0, borderwidth=0)
        vertical = ttk.Scrollbar(
            body, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(
            body, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set,
                              xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        self.grid_frame = ttk.Frame(self.canvas, style="TFrame")
        self._window = self.canvas.create_window(
            (0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._update_scroll_region)
        for widget in (self.canvas, self.grid_frame):
            widget.bind("<MouseWheel>", self._on_wheel)
            widget.bind("<Button-4>", self._on_wheel)
            widget.bind("<Button-5>", self._on_wheel)

    def _render_plate(self, images):
        """Put each well back at its physical row and column."""
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.photos = []
        self.tiles = []
        visible = preview_plane_images(
            images, self.current_channel, self._sites[self._site_index])
        by_position = {(image.row, image.col): image for image in visible}
        self._visible_images = visible

        tile_width = self._tile_width()
        tile_height = self._display_size + 52
        self.grid_frame.columnconfigure(0, minsize=34)
        self.grid_frame.rowconfigure(0, minsize=28)
        for col in range(self._cols):
            self.grid_frame.columnconfigure(col + 1, minsize=tile_width)
            ttk.Label(self.grid_frame, text=str(col + 1),
                      style="MutedBg.TLabel", anchor="center").grid(
                          row=0, column=col + 1, sticky="ew")
        for row in range(self._rows):
            self.grid_frame.rowconfigure(row + 1, minsize=tile_height)
            ttk.Label(self.grid_frame, text=chr(65 + row),
                      style="MutedBg.TLabel", anchor="center").grid(
                          row=row + 1, column=0, sticky="ns")

        for position in self._selected_wells:
            row, col = position
            image = by_position.get(position)
            tile = ttk.Frame(self.grid_frame, style="Card.TFrame",
                             padding=theme_mod.PAD_S)
            if image is not None and image.ok:
                photo = _photo_image(
                    image.array, master=self, max_size=self._display_size)
                self.photos.append(photo)
                holder = ttk.Label(tile, image=photo, style="Surface.TLabel")
            else:
                problem = (image.error[:100] if image is not None and image.error
                           else "not acquired at this position")
                holder = ttk.Label(
                    tile, style="Muted.TLabel", justify="center",
                    wraplength=self._display_size,
                    text=f"no image\n{problem}")
                holder.configure(padding=theme_mod.PAD_L)
            holder.pack(expand=True)
            caption = ttk.Label(
                tile, text=(image.well if image is not None
                            else f"{chr(65 + row)}{col + 1}"),
                style="Accent.TLabel")
            caption.pack(anchor="w", pady=(theme_mod.PAD_XS, 0))
            tile.grid(row=row + 1, column=col + 1,
                      padx=TILE_PAD // 2, pady=TILE_PAD // 2, sticky="n")
            for widget in (tile, holder, caption):
                widget.bind("<MouseWheel>", self._on_wheel)
                widget.bind("<Button-4>", self._on_wheel)
                widget.bind("<Button-5>", self._on_wheel)
            self.tiles.append(tile)

        if not self._selected_wells:
            ttk.Label(self.grid_frame, style="MutedBg.TLabel",
                      text="Nothing to show - no wells were requested.").grid(
                          row=1, column=1, padx=theme_mod.PAD_L,
                          pady=theme_mod.PAD_L)
        good = sum(1 for image in visible if image.ok)
        label = self._channel_labels.get(self.current_channel,
                                         str(self.current_channel))
        self.status_var.set(
            f"{good} of {len(self._selected_wells)} wells · {label} · "
            f"Z-stack image {self._site_index + 1} of {len(self._sites)}")
        self.after_idle(self._update_scroll_region)

    def _render_problem(self, message):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.photos = []
        self.tiles = []
        self._visible_images = []
        ttk.Label(self.grid_frame, style="MutedBg.TLabel", text=message,
                  justify="center").grid(row=0, column=0,
                                          padx=theme_mod.PAD_L,
                                          pady=theme_mod.PAD_L)
        self.status_var.set(message)
        self.after_idle(self._update_scroll_region)

    def _tile_width(self):
        return self._display_size + TILE_PAD + 4 * theme_mod.PAD_S

    def _choose_display_size(self):
        """Shrink common plates enough to show their whole physical shape."""
        requested = int(self.preview.size)
        try:
            width = self.winfo_screenwidth() - 220
            height = self.winfo_screenheight() - 320
            by_width = width // max(1, self._cols) - TILE_PAD - 4 * theme_mod.PAD_S
            by_height = height // max(1, self._rows) - 52
            return max(56, min(requested, by_width, by_height))
        except tk.TclError:
            return requested

    def _size_to_content(self):
        """Fit several plate columns while keeping large plates scrollable."""
        tile_width = self._tile_width()
        width = max(720, self._cols * tile_width + 90)
        height = max(520, self._rows * (self._display_size + 52) + 190)
        try:
            width = min(width, self.winfo_screenwidth() - 80)
            height = min(height, self.winfo_screenheight() - 120)
        except tk.TclError:
            pass
        self.geometry(f"{int(width)}x{int(height)}")
        self.minsize(620, 420)

    # -- changing channel / stack image ---------------------------------

    def _can_load_planes(self):
        return (len(self.preview.scans) == 1
                and getattr(self.preview.scans[0], "client", None) is not None)

    def _show_selected_plane(self):
        if self._closing:
            return
        key = (self.current_channel, self._sites[self._site_index])
        if key in self._plane_images:
            self._render_plate(self._plane_images[key])
            return
        if not self._can_load_planes():
            self._render_problem(
                "This channel and Z-stack image were not loaded.")
            return
        self._start_plane_load(key)

    def _start_plane_load(self, key):
        self._generation += 1
        generation = self._generation
        self._load_cancel.set()
        self._load_cancel = threading.Event()
        cancel = self._load_cancel
        channel, site = key
        self.status_var.set(
            f"Loading {self._channel_labels.get(channel, channel)} · "
            f"Z-stack image {self._site_index + 1} of {len(self._sites)} ...")
        threading.Thread(
            target=self._load_plane_worker,
            args=(generation, key, cancel), daemon=True,
            name="pyincucyte-plate-preview").start()

    def _load_plane_worker(self, generation, key, cancel):
        channel, site = key
        try:
            scan = self.preview.scans[0]
            recipe = self.preview.recipe
            result = scan.client.preview(
                scan, wells=set(self._selected_wells), channels=[channel],
                site=site, size=self.preview.size,
                contrast=self.preview.contrast,
                max_images=max(1, len(self._selected_wells)), cancel=cancel,
                calibrate=bool(getattr(recipe, "calibrate", False)),
                background=getattr(recipe, "background", "") or "",
                unmix=getattr(recipe, "unmix", "") or "")
            error = ""
        except Exception as exc:
            result = None
            error = str(exc)
        self._results.put((generation, key, result, error))

    def _poll_results(self):
        if self._closing:
            return
        try:
            while True:
                generation, key, result, error = self._results.get_nowait()
                if generation != self._generation:
                    continue
                if error:
                    self._render_problem(f"Could not load this preview:\n{error}")
                    continue
                self._plane_images[key] = list(result.images)
                selected = (self.current_channel,
                            self._sites[self._site_index])
                if key == selected:
                    self._render_plate(result.images)
        except queue.Empty:
            pass
        self._poll_job = self.after(50, self._poll_results)

    def _on_channel_change(self):
        self.current_channel = int(self.channel_var.get())
        self._show_selected_plane()

    def _update_z_label(self):
        self.z_label_var.set(
            f"{self._site_index + 1} / {len(self._sites)}")

    def _on_z_slider(self, value):
        index = max(0, min(len(self._sites) - 1,
                           int(round(float(value)))))
        self._site_index = index
        self.z_position_var.set(index)
        self._update_z_label()
        if self._slider_job is not None:
            self.after_cancel(self._slider_job)
        self._slider_job = self.after(90, self._finish_z_slide)

    def _finish_z_slide(self):
        self._slider_job = None
        self._show_selected_plane()

    def _step_z(self, amount):
        index = max(0, min(len(self._sites) - 1,
                           self._site_index + int(amount)))
        if index == self._site_index:
            return "break"
        if self._slider_job is not None:
            self.after_cancel(self._slider_job)
            self._slider_job = None
        self._site_index = index
        self.z_position_var.set(index)
        self._update_z_label()
        self._show_selected_plane()
        return "break"

    # -- events -----------------------------------------------------------

    def _update_scroll_region(self, _event=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    @staticmethod
    def _wheel_delta(event):
        delta = getattr(event, "delta", 0)
        if delta == 0:                      # X11 sends buttons, not a delta
            delta = 120 if getattr(event, "num", 5) == 4 else -120
        return delta

    def _on_z_wheel(self, event):
        return self._step_z(-1 if self._wheel_delta(event) > 0 else 1)

    def _on_wheel(self, event):
        if getattr(event, "state", 0) & 0x0004:       # Ctrl + wheel
            return self._on_z_wheel(event)
        delta = self._wheel_delta(event)
        if getattr(event, "state", 0) & 0x0001:       # Shift + wheel
            self.canvas.xview_scroll(int(-delta / 120), "units")
        else:
            self.canvas.yview_scroll(int(-delta / 120), "units")
        return "break"

    def _save(self):
        folder = filedialog.askdirectory(parent=self, title="Save previews to")
        if not folder:
            return
        try:
            paths = [image.save(f"{folder}/{image.filename()}")
                     for image in self._visible_images if image.ok]
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        messagebox.showinfo("Saved", f"{len(paths)} PNGs written to\n{folder}",
                            parent=self)

    def _close(self):
        if self._closing:
            return
        self._closing = True
        self._load_cancel.set()
        if self._slider_job is not None:
            try:
                self.after_cancel(self._slider_job)
            except tk.TclError:
                pass
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.destroy()

    def _on_destroy(self, event):
        if event.widget is not self or self._closing:
            return
        self._closing = True
        self._load_cancel.set()


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
