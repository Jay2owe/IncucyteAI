#!/usr/bin/env python3
"""The house figure style, as code rather than as prose to be re-read each time.

Import it instead of setting rcParams by hand, so two figures made months apart
by two different agents come out identical.

    import sys; sys.path.insert(0, str(Path.home() / ".claude/skills/plot-that/scripts"))
    from plot_style import apply, finish, save, save_many, COLORS, PALETTE, MUTED

    apply()
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, y, color=COLORS["blue"])
    ax.set_title("What the figure shows")     # bold, handled by apply()
    finish(ax)                                # drops top/right spines
    save(fig, bundle / "my-plot.svg")     # any visual ReproFig carrier + preview

Run it directly to write a swatch card of the palette:
    python plot_style.py /path/to/swatch.svg
"""

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt

# Saturated but restrained. Anything carrying meaning gets one of these.
#
# These are PyFLASH's values. Three places used to define "the colour of series
# one" — here, PyFLASH, and CircadianWorkbench — with blue and dark already
# identical and red, teal and orange each a few points apart. Nobody chose to
# have three; they accumulated one per project. Collapsed 2026-08-19 onto
# PyFLASH's, because PyFLASH is the published package. `analysis_kit.style`
# holds the same values and a conformance test fails if they part company.
COLORS = {
    "red": "#c0392b",
    "blue": "#4878A8",
    "teal": "#0e8f8f",
    "orange": "#d98a17",
    "dark": "#303030",
}

# Default cycle order for unlabelled series.
PALETTE = [COLORS["blue"], COLORS["red"], COLORS["teal"], COLORS["orange"], COLORS["dark"]]

# Not-significant, background, or "no data" states. Never use it for a finding.
MUTED = "#B0B0B0"

# Sans-serif preference order; matplotlib falls through to the first one present.
# The shared contract says bare "Arial"; plot-that keeps the fallbacks so a
# figure still renders on a machine without it.
FONTS = ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"]

AXIS_WIDTH = 2.0

# Data-line and legend-line weights. Not rcParams in the shared contract - pass
# them explicitly. PyFLASH theme values.
LINE_WIDTH = 2.4
LEGEND_LINE_WIDTH = 3.0
REGRESSION_LINE_WIDTH = 6.75

# In-graph annotation size. rcParams `font.size` governs bare ax.text(); the
# house rule is that anything written inside the axes is at least this big.
ANNOTATION_SIZE = 16

# Marker and scatter defaults.
POINT_SIZE = 9          # diameter in points; matplotlib scatter wants area = POINT_SIZE ** 2
SCATTER_ALPHA = 0.6
SCATTER_JITTER = 0.3
BAR_POINT_FILL = "white"
BAR_POINT_LINEWIDTH = 3

# Matrix and heatmap defaults.
MATRIX_CMAP = "coolwarm"
PVALUE_CMAP = "coolwarm"
QVALUE_CMAP = "viridis_r"
GRID = "#dfe4ea"

# Crowded categorical x axes.
TICK_ROTATION = 60
TICK_HA = "right"

# Significance stars. Ordered strictest first; the first threshold a p-value
# clears wins.
SIGNIFICANCE = {0.0001: "****", 0.001: "***", 0.01: "**", 0.05: "*"}
NS_LABEL = "ns"

# The 26 rcParams that ARE the shared house contract. Identical, key for key, to
# analysis_kit.style.rcparams("pyflash"), which in turn reproduces
# PyFLASH.aesthetics._matplotlib_rc_updates() and is tested against the installed
# PyFLASH. Aligned 2026-08-23 - plot-that had drifted to roughly half the type
# size, so its figures came out unreadable beside every other house figure.
# Change these only by changing analysis_kit first.
_HOUSE_CONTRACT = {
    "axes.linewidth": AXIS_WIDTH,
    "xtick.major.width": AXIS_WIDTH,
    "ytick.major.width": AXIS_WIDTH,
    "xtick.major.size": 11.0,
    "ytick.major.size": 11.0,
    "xtick.labelsize": 20.0,
    "ytick.labelsize": 20.0,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "axes.labelsize": 22.0,
    "axes.labelweight": "normal",
    "axes.titlesize": 20.0,
    "axes.titleweight": "bold",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "legend.fontsize": 15.0,
    "legend.title_fontsize": 15.0,
    "font.weight": "normal",
    "font.family": "Arial",
    "savefig.dpi": 600,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.1,
    "svg.fonttype": "none",
    "mathtext.default": "regular",
    "figure.figsize": (7.0, 5.0),
}


def apply() -> None:
    """Set the house rcParams on the global matplotlib state."""
    mpl.rcParams.update({
        **_HOUSE_CONTRACT,
        # --- plot-that's own additions, on top of the contract ---

        # Editable text in every vector format, not outlined paths.
        "pdf.fonttype": 42,
        "ps.fonttype": 42,

        # Transparent everywhere - the figure sits on whatever page it lands on.
        # The shared contract paints white; plot-that deliberately does not.
        "figure.facecolor": "none",
        "figure.edgecolor": "none",
        "axes.facecolor": "none",
        "savefig.facecolor": "none",
        "savefig.edgecolor": "none",
        "savefig.transparent": True,

        # Keep the fallbacks the bare contract font name does not carry.
        "font.family": "sans-serif",
        "font.sans-serif": FONTS,

        # Bare ax.text() lands at the in-graph annotation floor rather than at
        # matplotlib's 10pt, which would be half the tick size. Not part of the
        # contract - the contract does not set font.size at all.
        "font.size": ANNOTATION_SIZE,

        # Suptitle sits just above the axes title.
        "figure.titlesize": 22.0,
        "figure.titleweight": "bold",

        "axes.edgecolor": "black",
        "xtick.color": "black",
        "ytick.color": "black",
        "xtick.minor.width": AXIS_WIDTH / 2,
        "ytick.minor.width": AXIS_WIDTH / 2,

        "legend.handlelength": 1.6,
        "legend.borderaxespad": 0.4,

        # no ornament
        "axes.grid": False,
        "grid.color": GRID,
        "legend.shadow": False,
        "patch.force_edgecolor": False,

        "axes.prop_cycle": mpl.cycler(color=PALETTE),
        "lines.linewidth": LINE_WIDTH,
        "image.cmap": "viridis",
        "figure.dpi": 110,
    })


def finish(*axes, keep_spines: tuple[str, ...] = ()) -> None:
    """Strip top and right spines and re-assert widths after a plotting call has
    overridden them. Pass keep_spines=("top",) when a spine carries meaning."""
    for ax in axes:
        for side in ("top", "right"):
            ax.spines[side].set_visible(side in keep_spines)
        for side in ("left", "bottom", *keep_spines):
            if ax.spines[side].get_visible():
                ax.spines[side].set_linewidth(AXIS_WIDTH)
                ax.spines[side].set_color("black")
        ax.tick_params(width=AXIS_WIDTH, color="black", labelcolor="black")


def save(
    fig,
    artifact_path,
    preview: bool = True,
    *,
    claim: str | None = None,
    grammar: str | None = None,
    producer: str | Path | None = None,
    statistics_status: str | None = None,
    dpi: float | None = None,
    render_preset: str | None = "line_art",
    width: float | None = None,
    height: float | None = None,
    format_options: dict | None = None,
    allow_reencode: bool = False,
    proof: bool = False,
    proof_policy: dict | None = None,
) -> Path:
    """Write a master ReproFig visual carrier and, by default, a PNG preview.

    The fixed plot-that bundle files must already exist. Registration later
    refreshes the record with the final claim, grammar, and explicit statistics
    status, while preserving the figure identity created here. Raster output
    defaults to the 600-DPI ``line_art`` preset; an explicit ``dpi`` wins.
    """

    return save_many(
        fig,
        [artifact_path],
        preview=preview,
        claim=claim,
        grammar=grammar,
        producer=producer,
        statistics_status=statistics_status,
        dpi=dpi,
        render_preset=render_preset,
        width=width,
        height=height,
        format_options=format_options,
        allow_reencode=allow_reencode,
        proof=proof,
        proof_policy=proof_policy,
    )[0]


def save_many(
    fig,
    artifact_paths,
    preview: bool = True,
    *,
    claim: str | None = None,
    grammar: str | None = None,
    producer: str | Path | None = None,
    statistics_status: str | None = None,
    dpi: float | None = None,
    render_preset: str | None = "line_art",
    width: float | None = None,
    height: float | None = None,
    format_options: dict | None = None,
    allow_reencode: bool = False,
    proof: bool = False,
    proof_policy: dict | None = None,
) -> list[Path]:
    """Write several visual carriers with one shared ReproFig figure identity."""

    from reprofig_bundle import save_matplotlib_figure

    paths = [Path(path) for path in artifact_paths]
    if not paths:
        raise ValueError("save_many needs at least one output path")
    parent = paths[0].parent.resolve()
    stem = paths[0].stem
    if any(path.parent.resolve() != parent or path.stem != stem for path in paths[1:]):
        raise ValueError(
            "save_many outputs must share one folder and stem so one figure "
            "identity cannot be assigned to unrelated plots"
        )
    record = None
    for artifact_path in paths:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        is_jpeg = artifact_path.suffix.lower() in {".jpg", ".jpeg", ".jpe"}
        savefig_kwargs = {
            "transparent": not is_jpeg,
            "bbox_inches": "tight",
        }
        if is_jpeg:
            savefig_kwargs["facecolor"] = "white"
        record = save_matplotlib_figure(
            fig,
            artifact_path,
            record=record,
            claim=claim,
            grammar=grammar,
            producer=producer,
            statistics_status=statistics_status,
            dpi=dpi,
            render_preset=render_preset,
            width=width,
            height=height,
            format_options=format_options,
            allow_reencode=allow_reencode,
            proof=proof,
            proof_policy=proof_policy,
            savefig_kwargs=savefig_kwargs,
        )
    if preview:
        preview_path = paths[0].parent / "preview.png"
        save_matplotlib_figure(
            fig,
            preview_path,
            record=record,
            dpi=200,
            render_preset=None,
            proof=proof,
            proof_policy=proof_policy,
            savefig_kwargs={
                "transparent": False,
                "facecolor": "white",
                "bbox_inches": "tight",
            },
        )
    return paths


def _swatch(out: Path) -> None:
    apply()
    fig, ax = plt.subplots(figsize=(6, 2.2))
    items = [*COLORS.items(), ("muted (n.s.)", MUTED)]
    for i, (name, hexv) in enumerate(items):
        ax.bar(i, 1, color=hexv, width=0.8)
        ax.text(i, -0.12, f"{name}\n{hexv}", ha="center", va="top", fontsize=8)
    ax.set_xlim(-0.7, len(items) - 0.3)
    ax.set_ylim(0, 1.15)
    ax.set_title("plot-that house palette")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    from reprofig import save_figure, source_reference

    producer_path = Path(__file__).resolve()
    producer_source = producer_path.read_text(encoding="utf-8")

    save_figure(
        fig,
        out,
        title="plot-that house palette",
        producer={"package": "plot-that", "function": "plot_style._swatch"},
        plotted_data=[{"name": name, "hex": hexv} for name, hexv in items],
        sources=[source_reference(
            producer_path, role="producer", project_root=producer_path.parent
        )],
        data_status="complete",
        statistics_status="not_applicable",
        reproduction={
            "command": "python plot_style.py <output>",
            "script": producer_source,
        },
        render_preset="line_art",
        savefig_kwargs={
            "transparent": out.suffix.lower() not in {".jpg", ".jpeg", ".jpe"},
            "bbox_inches": "tight",
        },
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    import sys
    _swatch(Path(sys.argv[1] if len(sys.argv) > 1 else "plot-that-palette.svg"))
