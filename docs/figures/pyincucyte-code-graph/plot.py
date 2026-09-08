#!/usr/bin/env python3
"""Render the audited automation code graph from figure_data.csv."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from reprofig import save_figure, source_reference


BUNDLE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def _load_style():
    spec = importlib.util.spec_from_file_location(
        "plot_that_style", BUNDLE / "src_plot_style.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _boundary(node, other):
    dx = other["x"] - node["x"]
    dy = other["y"] - node["y"]
    scale = 1.0 / max(
        abs(dx) / (node["width"] / 2.0),
        abs(dy) / (node["height"] / 2.0),
    )
    return node["x"] + dx * scale, node["y"] + dy * scale


def main():
    style = _load_style()
    style.apply()
    rows = list(csv.DictReader(
        (BUNDLE / "figure_data.csv").open(encoding="utf-8", newline="")
    ))
    meta = {row["id"]: row["label"] for row in rows
            if row["record_type"] == "meta"}
    nodes = {}
    for row in rows:
        if row["record_type"] != "node":
            continue
        node = dict(row)
        for key in ("x", "y", "width", "height"):
            node[key] = float(node[key])
        nodes[node["id"]] = node

    fills = {
        "caller": "#f7f7f7",
        "public": "#f6eedc",
        "core": "#e5edf7",
        "watch": "#e2f2f2",
        "engine": "#f7eadc",
        "model": "#ececec",
    }
    edges = {
        "caller": "#8a94a6",
        "public": style.COLORS["orange"],
        "core": style.COLORS["blue"],
        "watch": style.COLORS["teal"],
        "engine": style.COLORS["orange"],
        "model": style.COLORS["dark"],
    }

    fig, ax = plt.subplots(figsize=(10.5, 7.6))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 10.25)
    ax.axis("off")

    for row in rows:
        if row["record_type"] != "edge":
            continue
        source = nodes[row["source"]]
        target = nodes[row["target"]]
        start = _boundary(source, target)
        end = _boundary(target, source)
        colour = edges[row["group"]]
        arrow = FancyArrowPatch(
            start, end, arrowstyle="-|>", mutation_scale=13,
            linewidth=1.8, color=colour, zorder=1,
            connectionstyle="arc3,rad=0.0",
        )
        ax.add_patch(arrow)
        if row["edge_label"]:
            mx = (start[0] + end[0]) / 2.0
            my = (start[1] + end[1]) / 2.0
            ax.text(
                mx, my, row["edge_label"], ha="center", va="center",
                fontsize=7.2, color=colour, zorder=2,
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.2},
            )

    for node in nodes.values():
        group = node["group"]
        box = FancyBboxPatch(
            (node["x"] - node["width"] / 2.0,
             node["y"] - node["height"] / 2.0),
            node["width"], node["height"],
            boxstyle="round,pad=0.04,rounding_size=0.08",
            facecolor=fills[group], edgecolor=edges[group],
            linewidth=1.8, zorder=3,
        )
        ax.add_patch(box)
        ax.text(
            node["x"], node["y"] + 0.13, node["label"],
            ha="center", va="center", fontsize=9.4, fontweight="bold",
            fontfamily="monospace", color="#20252d", zorder=4,
        )
        ax.text(
            node["x"], node["y"] - 0.20, node["detail"],
            ha="center", va="center", fontsize=7.6,
            color="#596579", zorder=4,
        )

    ax.text(0.35, 9.95, meta["title"], ha="left", va="top",
            fontsize=20, fontweight="bold", color="#20252d")
    ax.text(0.35, 9.52, meta["subtitle"], ha="left", va="top",
            fontsize=9.3, color="#68758b")
    for row in rows:
        if row["record_type"] == "section":
            ax.text(float(row["x"]), float(row["y"]), row["label"],
                    ha="left", va="center", fontsize=7.4,
                    fontweight="bold", color="#7b879a")
    ax.text(0.35, 0.16, meta["footer"], ha="left", va="bottom",
            fontsize=7.2, color="#68758b")

    claim = meta["claim"]
    data = rows
    sources = [source_reference(path, project_root=BUNDLE)
               for path in sorted(BUNDLE.glob("src_*"))]
    reproduction = {
        "command": "python plot.py",
        "script": (BUNDLE / "plot.py").read_text(encoding="utf-8"),
    }
    master = BUNDLE / meta["master"]
    record = save_figure(
        fig, master, data=data, sources=sources, claim=claim,
        grammar="flow", producer={"package": "matplotlib", "function": "plot.py"},
        project_root=BUNDLE, reproduction=reproduction,
        statistics_status="not_applicable", render_preset="line_art",
        savefig_kwargs={"transparent": True, "bbox_inches": "tight"},
    )
    save_figure(
        fig, master.with_suffix(".png"), record=record, dpi=300,
        render_preset=None,
        savefig_kwargs={"transparent": False, "facecolor": "white",
                        "bbox_inches": "tight"},
    )
    save_figure(
        fig, BUNDLE / "preview.png", record=record, dpi=180,
        render_preset=None,
        savefig_kwargs={"transparent": False, "facecolor": "white",
                        "bbox_inches": "tight"},
    )
    plt.close(fig)


if __name__ == "__main__":
    main()
