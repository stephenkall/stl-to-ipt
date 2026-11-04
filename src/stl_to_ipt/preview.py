"""2D preview (PNG) of a spec's sketches, one cell per feature."""
import math
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from . import spec as sp  # noqa: E402

COLORS = {"new": "tab:blue", "join": "tab:green", "cut": "tab:red"}
AXIS_NAME = "XYZ"


def _draw(ax, g, color):
    for p in (list(g.geoms) if hasattr(g, "geoms") else [g]):
        if p.is_empty:
            continue
        xs, ys = p.exterior.xy
        ax.fill(xs, ys, color=color, alpha=0.18)
        ax.plot(xs, ys, color=color, lw=1.2)
        for h in p.interiors:
            hx, hy = h.xy
            ax.fill(hx, hy, color="white")
            ax.plot(hx, hy, color=color, lw=1.2)


def render(spec_path, out_png):
    spec = sp.expand_mirrors(sp.load(spec_path))
    model = spec.get("system") == "model"
    feats = [f for f in spec["features"] if f["type"] == "extrusion"]
    revolves = [f for f in spec["features"] if f["type"] == "revolve"]
    count = max(len(feats) + len(revolves), 1)
    cols = min(count, 3)
    rows = math.ceil(count / cols)
    fig, axs = plt.subplots(rows, cols, figsize=(5 * cols, 4.5 * rows), squeeze=False)
    for ax in axs.flat:
        ax.axis("off")
    cells = iter(axs.flat)
    for i, f in enumerate(feats, 1):
        ax = next(cells)
        ax.axis("on")
        ax.set_aspect("equal")
        ax.grid(True, alpha=0.3)
        _draw(ax, sp.sketch_geometry(f["sketch"]), COLORS[f.get("operation", "new")])
        d = f["distance"]
        depth = d if d == sp.THROUGH_ALL else "%g mm" % d
        if model:
            n_, A, B = sp.PLANE_AXES[f["plane"]]
            ax.set_xlabel("%s (mm)" % AXIS_NAME[A])
            ax.set_ylabel("%s (mm)" % AXIS_NAME[B])
            title = "%d. %s | %s | plane %s at %s=%g | %s" % (
                i, f.get("name", f["operation"]), f.get("operation", "new"), f["plane"],
                AXIS_NAME[n_], f.get("offset", 0), depth)
        else:
            title = "%d. %s | plane %s | %s" % (i, f.get("operation", "new"), f.get("plane", "XY"), depth)
        ax.set_title(title, fontsize=8)
    for f in revolves:
        ax = next(cells)
        ax.axis("on")
        ax.set_aspect("equal")
        p = f["profile"] + [f["profile"][0]]
        xs, ys = zip(*p)
        ax.plot(xs, ys, color="tab:purple")
        ax.plot([-x for x in xs], ys, color="tab:purple", alpha=0.35)
        ax.axvline(0, color="k", ls="--", lw=0.8)
        ax.set_title("revolve | axis = dashed line", fontsize=8)
    fig.suptitle(spec.get("notes", {}).get("part", os.path.basename(spec_path)), fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    return out_png
