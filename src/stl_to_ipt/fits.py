"""Protect the fit between mating parts when dimensions are rounded.

Rounding 8.97 -> 9.0 on a pin and 9.18 -> 9.2 on its hole is harmless, but rounding is applied per dimension and
could, in an unlucky case, close a clearance or loosen it. The parts of a project live in one shared design frame
(each STL keeps its position in the assembly), so mating features can be found geometrically:

* a **shaft** is a circular boss (a circle that is not inside another shape of an additive feature);
* a **hole** is a circular cut, an inner ring of a profile, or a hole feature;
* a shaft and a hole of two different parts mate when they are coaxial in the shared frame, overlap along the
  axis, and the hole is at most ``MAX_CLEARANCE`` larger than the shaft.

For every pair the clearance measured on the mesh (before rounding, stored in ``notes.raw_diameters``) is compared
with the clearance of the rounded spec. A change above ``TOLERANCE`` (or a clearance that becomes <= 0) is
reported and, with ``apply``, repaired by resizing the hole to ``shaft + raw clearance`` (to 0.05 mm).
"""
import glob
import json
import os
from pathlib import Path

from . import spec as sp

MAX_CLEARANCE = 1.5   # mm: a hole more than this much larger than a shaft is not a fit
TOLERANCE = 0.05      # mm: largest accepted change of a clearance caused by rounding
COAXIAL_TOL = 0.6     # mm: centers closer than this (perpendicular to the axis) are coaxial


def raw_diameters(spec):
    """Diameters as measured (before rounding): [{"feature": i, "shape": j or None, "d": value}, ...]."""
    out = []
    for i, f in enumerate(spec["features"]):
        if f["type"] == "hole":
            out.append({"feature": i, "shape": None, "d": f["d"]})
        elif f["type"] == "extrusion":
            for j, s in enumerate(f["sketch"]):
                if s.get("shape") == "circle":
                    out.append({"feature": i, "shape": j, "d": s["d"]})
    return out


def _raw_lookup(spec):
    return {(r["feature"], r["shape"]): r["d"] for r in spec.get("notes", {}).get("raw_diameters", [])}


def _expanded(spec):
    """(index of the original feature, feature) with mirror features expanded into their mirrored copies."""
    index = {f.get("name"): i for i, f in enumerate(spec["features"])}
    out = []
    for f in sp.expand_mirrors(spec)["features"]:
        name = f.get("name", "")
        base = name[:-len(" (mirrored)")] if name.endswith(" (mirrored)") else name
        out.append((index.get(base), f))
    return out


def cylinders(part, spec):
    """Round features of one part in the shared design frame. Parts that were rotated before slicing are skipped
    (their frame no longer matches the assembly)."""
    notes = spec.get("notes", {})
    if "rotation_applied_before_slicing" in notes or "original_min" not in notes:
        return []
    omin = notes["original_min"]
    raw = _raw_lookup(spec)
    out = []
    for i, f in _expanded(spec):
        if f["type"] not in ("extrusion", "hole"):
            continue
        n, A, B = sp.PLANE_AXES[f["plane"]]
        off = f.get("offset", 0)
        direction = f.get("direction", 1)
        if f["type"] == "hole":
            span = sorted((off, off + direction * f["depth"]))
            for p in f["points"]:
                out.append(_cyl(part, i, None, "hole", n, A, B, omin, p, f["d"], raw.get((i, None), f["d"]), span, f["name"]))
            continue
        dist = f["distance"]
        span = (-1e9, 1e9) if dist == sp.THROUGH_ALL else sorted((off, off + direction * dist))
        others = [sp.shape_to_geometry(sh) for sh in sp.sketch_shapes(f["sketch"]) if sh["shape"] != "circle"]
        for j, s in enumerate(f["sketch"]):
            if s.get("shape") != "circle":
                continue
            for c in sp.expand_shape(s):
                if f["operation"] == "cut":
                    kind = "hole"
                else:
                    from shapely.geometry import Point
                    inside = any(g.contains(Point(c["x"], c["y"])) for g in others)
                    kind = "hole" if inside else "shaft"
                out.append(_cyl(part, i, j, kind, n, A, B, omin, (c["x"], c["y"]), c["d"],
                                raw.get((i, j), c["d"]), span, f["name"]))
    return out


def _cyl(part, i, j, kind, n, A, B, omin, center, d, d_raw, span, name):
    return {"part": part, "feature": i, "shape": j, "kind": kind, "axis": n, "name": name,
            "center": (center[0] + omin[A], center[1] + omin[B]), "d": d, "d_raw": d_raw,
            "span": (span[0] + omin[n], span[1] + omin[n]) if abs(span[0]) < 1e8 else span, "A": A, "B": B}


def find_pairs(cyls):
    """Shaft/hole pairs of different parts that mate (see the module documentation)."""
    shafts = [c for c in cyls if c["kind"] == "shaft"]
    holes = [c for c in cyls if c["kind"] == "hole"]
    pairs = []
    for s in shafts:
        for h in holes:
            if s["part"] == h["part"] or s["axis"] != h["axis"]:
                continue
            if (s["A"], s["B"]) != (h["A"], h["B"]):
                continue
            if abs(s["center"][0] - h["center"][0]) > COAXIAL_TOL or abs(s["center"][1] - h["center"][1]) > COAXIAL_TOL:
                continue
            if min(s["span"][1], h["span"][1]) - max(s["span"][0], h["span"][0]) < 1.0:
                continue
            clearance_raw = h["d_raw"] - s["d_raw"]
            if not 0.0 <= clearance_raw <= MAX_CLEARANCE:
                continue
            pairs.append({"shaft": s, "hole": h, "raw": clearance_raw, "rounded": h["d"] - s["d"]})
    return pairs


def judge(pair):
    """'ok' when rounding kept the clearance within TOLERANCE, else 'changed' or 'closed'."""
    if pair["rounded"] <= 0 < pair["raw"]:
        return "closed"
    return "ok" if abs(pair["rounded"] - pair["raw"]) <= TOLERANCE else "changed"


def fixed_hole_diameter(pair):
    """Hole diameter that restores the measured clearance on top of the (rounded) shaft."""
    return round(round((pair["shaft"]["d"] + pair["raw"]) / 0.05) * 0.05, 2)


def _set_diameter(spec, cyl, value):
    f = spec["features"][cyl["feature"]]
    if cyl["shape"] is None:
        f["d"] = value
    else:
        f["sketch"][cyl["shape"]]["d"] = value


def analyze_directory(spec_dir, apply=False):
    """Load every spec of a folder, check the fits between the parts and (with ``apply``) repair them.

    Return the list of pair reports: (shaft part, hole part, raw clearance, rounded clearance, verdict)."""
    specs = {}
    for path in sorted(glob.glob(os.path.join(spec_dir, "*.json"))):
        specs[Path(path).stem] = (path, sp.load(path))
    cyls = []
    for part, (_, spec) in specs.items():
        cyls += cylinders(part, spec)
    pairs = find_pairs(cyls)
    report, dirty = [], set()
    for pair in pairs:
        verdict = judge(pair)
        report.append((pair["shaft"]["part"], pair["hole"]["part"], round(pair["raw"], 2),
                       round(pair["rounded"], 2), verdict, pair["shaft"]["d"], pair["hole"]["d"]))
        if verdict != "ok" and apply:
            new_d = fixed_hole_diameter(pair)
            part = pair["hole"]["part"]
            _set_diameter(specs[part][1], pair["hole"], new_d)
            dirty.add(part)
    for part in dirty:
        path, spec = specs[part]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
    return report
