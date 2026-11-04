"""Shared spec utilities: loading, validation and expansion (no Inventor dependency).

A spec is a JSON document that describes a part as a list of features. In the ``"model"`` system every
feature uses global X/Y/Z coordinates with the mesh's minimum corner at the origin.
"""
import copy
import json
import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon

from . import gears

SHAPES = {"rectangle", "circle", "slot", "polygon", "contour"}
# Model system: plane name -> (normal axis, axis A, axis B) with 0=X, 1=Y, 2=Z
PLANE_AXES = {"XY": (2, 0, 1), "XZ": (1, 0, 2), "YZ": (0, 1, 2)}
OPERATIONS = {"new", "join", "cut"}
PLANES = {"XY", "XZ", "YZ"}
SELECTIONS = {"all", "vertical", "top", "bottom"}
# model system: the outer edges of the cap where the base extrusion starts / ends (the holes are left alone)
BASE_OUTLINE_SELECTIONS = ("base_start_outline", "base_end_outline")
THROUGH_ALL = "through_all"


def sample_arc(p0, pm, p1, n=24):
    """Points of the arc that passes through p0, pm (a point between the ends) and p1."""
    ax, ay = p0
    bx, by = pm
    cx, cy = p1
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [p0, pm, p1]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    tau = 2 * math.pi
    s_m, s_1 = (am - a0) % tau, (a1 - a0) % tau
    sweep = s_1 if s_m < s_1 else s_1 - tau
    return [(ux + r * math.cos(a0 + sweep * k / n), uy + r * math.sin(a0 + sweep * k / n))
            for k in range(n + 1)]


def contour_points(s, arc_steps=24):
    """Polyline of a ``contour`` shape (arcs are sampled with ``arc_steps`` segments)."""
    pts = [tuple(s["start"])]
    for seg in s["segments"]:
        if seg[0] == "L":
            pts.append((seg[1], seg[2]))
        else:  # ["A", mid_x, mid_y, end_x, end_y]
            pts.extend(sample_arc(pts[-1], (seg[1], seg[2]), (seg[3], seg[4]), arc_steps)[1:])
    return pts


def shape_to_geometry(s):
    """Shapely geometry of one sketch shape."""
    shape = s["shape"]
    if shape == "rectangle":
        x, y, w, h = s["x"], s["y"], s["width"], s["height"]
        if s.get("centered"):
            x, y = x - w / 2.0, y - h / 2.0
        return Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
    if shape == "circle":
        return Point(s["x"], s["y"]).buffer(s["d"] / 2.0, 64)
    if shape == "polygon":
        p = Polygon(s["points"])
        return p if p.is_valid else p.buffer(0)
    if shape == "slot":
        return LineString([(s["x1"], s["y1"]), (s["x2"], s["y2"])]).buffer(s["width"] / 2.0, 32)
    if shape == "contour":
        p = Polygon(contour_points(s, 96))  # fine arcs: 24 steps lose ~1% of the area of a large arc
        return p if p.is_valid else p.buffer(0)
    raise ValueError("unknown shape: %s" % shape)


def sketch_geometry(sketch):
    """Planar region of a sketch: nested contours become holes (XOR)."""
    g = None
    for s in sketch_shapes(sketch):
        p = shape_to_geometry(s)
        g = p if g is None else g.symmetric_difference(p)
    return g


def base_outline(base):
    """(filled outline, outline edges) of the base extrusion's sketch: the outer rings only, holes ignored."""
    from shapely.geometry import MultiLineString
    from shapely.ops import unary_union
    region = sketch_geometry(base["sketch"])
    polys = [p for p in getattr(region, "geoms", [region]) if p.geom_type == "Polygon"]
    filled = unary_union([Polygon(p.exterior) for p in polys])
    return filled, MultiLineString([p.exterior for p in polys])


def fillet_inset(radius, t):
    """How far a rolling-ball fillet of ``radius`` has eaten into the outline at distance ``t`` from the cap."""
    t = np.asarray(t, dtype=float)
    return np.where((t >= 0) & (t < radius), radius - np.sqrt(np.maximum(radius ** 2 - (radius - t) ** 2, 0.0)), 0.0)


def edge_fillet_removed(f, base, X, Y, Z):
    """Mask of the material a ``base_*_outline`` fillet removes from the base extrusion, for point arrays."""
    import shapely
    n, A, B = PLANE_AXES[base["plane"]]
    co = [X, Y, Z]
    lo, step, dist = base.get("offset", 0.0), base.get("direction", 1), base["distance"]
    t = (co[n] - lo) * step if f["selection"] == "base_start_outline" else (lo + step * dist - co[n]) * step
    shape = np.broadcast(X, Y, Z).shape
    t = np.broadcast_to(t, shape)
    removed = np.zeros(shape, bool)
    near = (t >= 0) & (t < f["radius"])
    if not near.any():
        return removed
    idx = np.nonzero(near)
    pa, pb = np.broadcast_to(co[A], shape)[idx], np.broadcast_to(co[B], shape)[idx]
    filled, edges = base_outline(base)
    inside = shapely.contains_xy(filled, pa, pb) | shapely.intersects_xy(filled, pa, pb)
    removed[idx] = inside & (shapely.distance(edges, shapely.points(pa, pb)) < fillet_inset(f["radius"], t[idx]))
    return removed


def load(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    return normalize(spec)


def _resolve(obj, params):
    """Replace strings that name a parameter with its numeric value."""
    if isinstance(obj, str) and obj in params:
        return float(params[obj])
    if isinstance(obj, list):
        return [_resolve(o, params) for o in obj]
    if isinstance(obj, dict):
        return {k: _resolve(v, params) for k, v in obj.items()}
    return obj


def legacy_to_features(spec):
    """Convert the simple legacy format (``base`` + ``holes``) to a feature list."""
    feats = []
    base = spec["base"]
    if base["type"] == "rectangle":
        feats.append({
            "type": "extrusion", "plane": "XY", "operation": "new",
            "distance": base["thickness"],
            "sketch": [{"shape": "rectangle", "x": 0, "y": 0,
                        "width": base["width"], "height": base["height"]}],
        })
    elif base["type"] == "cylinder":
        feats.append({
            "type": "extrusion", "plane": "XY", "operation": "new",
            "distance": base["length"],
            "sketch": [{"shape": "circle", "x": 0, "y": 0, "d": base["diameter"]}],
        })
    else:
        raise ValueError("unknown base type: %s" % base["type"])
    if spec.get("holes"):
        feats.append({
            "type": "extrusion", "plane": "XY", "operation": "cut",
            "distance": THROUGH_ALL,
            "sketch": [{"shape": "circle", "x": f["x"], "y": f["y"], "d": f["d"]}
                       for f in spec["holes"]],
        })
    return feats


def normalize(spec):
    spec = dict(spec)
    params = spec.get("params", {})
    if "features" not in spec:
        spec["features"] = legacy_to_features(spec)
    spec["features"] = _resolve(spec["features"], params)
    return spec


def expand_shape(s):
    """Expand the optional ``pattern`` field (linear or circular) into plain shapes."""
    pat = s.get("pattern")
    if not pat:
        return [s]
    base = {k: v for k, v in s.items() if k != "pattern"}
    out = []
    if pat["type"] == "linear":
        nx, ny = int(pat.get("nx", 1)), int(pat.get("ny", 1))
        for i in range(nx):
            for j in range(ny):
                n = dict(base)
                n["x"] = base["x"] + i * pat.get("dx", 0)
                n["y"] = base["y"] + j * pat.get("dy", 0)
                if nx == 1 or ny == 1:  # 1D pattern: remember the link to the previous copy (parametric dims)
                    n["_pattern"] = (id(s), i + j)
                    n["_dx"], n["_dy"] = pat.get("dx", 0), pat.get("dy", 0)
                out.append(n)
    elif pat["type"] == "circular":
        cx, cy = pat.get("cx", 0), pat.get("cy", 0)
        r = math.hypot(base["x"] - cx, base["y"] - cy)
        a0 = math.atan2(base["y"] - cy, base["x"] - cx)
        count = int(pat["n"])
        for k in range(count):
            a = a0 + 2 * math.pi * k / count
            n = dict(base)
            n["x"] = cx + r * math.cos(a)
            n["y"] = cy + r * math.sin(a)
            out.append(n)
    else:
        raise ValueError("unknown pattern: %s" % pat["type"])
    return out


def sketch_shapes(sketch):
    res = []
    for s in sketch:
        res.extend(expand_shape(s))
    return res


def slot_points(s):
    """Return (a1, a2, mid_a2, b2, b1, mid_b1) in mm, with a = +normal side and b = -normal side."""
    x1, y1, x2, y2, w = s["x1"], s["y1"], s["x2"], s["y2"], s["width"]
    r = w / 2.0
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    a1 = (x1 + nx * r, y1 + ny * r)
    a2 = (x2 + nx * r, y2 + ny * r)
    b2 = (x2 - nx * r, y2 - ny * r)
    b1 = (x1 - nx * r, y1 - ny * r)
    mid2 = (x2 + ux * r, y2 + uy * r)
    mid1 = (x1 - ux * r, y1 - uy * r)
    return a1, a2, mid2, b2, b1, mid1


# ----------------------------------------------------------------------------- mirroring
def mirror_axis(plane):
    """Global axis (0=X, 1=Y, 2=Z) that is normal to the mirror plane ``plane``."""
    return PLANE_AXES[plane][0]


def _reflect_shape(s, axis_index, c):
    """Reflect one sketch shape about the line ``coordinate[axis_index] == c`` (axis_index 0 = sketch x,
    1 = sketch y). Return None for shapes that cannot be reflected."""
    s = copy.deepcopy(s)
    k = ("x", "y")[axis_index]
    shape = s["shape"]

    def r(v):
        return 2 * c - v

    pat = s.get("pattern")
    if pat and pat.get("type") != "linear":
        return None
    if shape == "circle":
        s[k] = r(s[k])
    elif shape == "rectangle":
        size = "width" if axis_index == 0 else "height"
        s[k] = r(s[k]) if s.get("centered") else r(s[k] + s[size])
    elif shape == "polygon":
        s["points"] = [[r(p[0]), p[1]] if axis_index == 0 else [p[0], r(p[1])] for p in s["points"]]
    elif shape == "slot":
        s[k + "1"], s[k + "2"] = r(s[k + "1"]), r(s[k + "2"])
    elif shape == "contour":
        def pt(x, y):
            return [r(x), y] if axis_index == 0 else [x, r(y)]
        s["start"] = pt(*s["start"])
        segs = []
        for sg in s["segments"]:
            if sg[0] == "L":
                segs.append(["L"] + pt(sg[1], sg[2]))
            else:
                segs.append(["A"] + pt(sg[1], sg[2]) + pt(sg[3], sg[4]))
        s["segments"] = segs
    else:
        return None
    if pat:
        d = "dx" if axis_index == 0 else "dy"
        if d in pat:
            pat[d] = -pat[d]
    return s


def reflect_feature(f, axis, c):
    """Mirror image of feature ``f`` about the plane whose normal is global ``axis`` at coordinate ``c``.
    Return None when the feature cannot be reflected."""
    f = copy.deepcopy(f)
    n, A, B = PLANE_AXES[f["plane"]]
    if f["type"] == "hole":
        if axis == n:
            f["offset"] = 2 * c - f.get("offset", 0)
            f["direction"] = -f.get("direction", 1)
        else:
            k = 0 if axis == A else 1
            f["points"] = [[2 * c - p[0], p[1]] if k == 0 else [p[0], 2 * c - p[1]] for p in f["points"]]
        return f
    if f["type"] != "extrusion":
        return None
    if axis == n:
        f["offset"] = 2 * c - f.get("offset", 0)
        f["direction"] = -f.get("direction", 1)
        return f
    k = 0 if axis == A else 1
    shapes = [_reflect_shape(s, k, c) for s in f["sketch"]]
    if any(s is None for s in shapes):
        return None
    f["sketch"] = shapes
    return f


def gear_to_extrusion(f):
    """Plain extrusion (polygon sketch) with the outline of a ``gear`` feature, for geometry evaluation."""
    poly = gears.gear_polygon(f["module"], int(f["teeth"]), f["pressure_angle"], f.get("phase", 0.0),
                              tuple(f["center"]), 12, f.get("dedendum", gears.DEDENDUM),
                              f.get("addendum", gears.ADDENDUM))
    pts = [[round(x, 4), round(y, 4)] for x, y in list(poly.exterior.coords)[:-1]]
    return {"type": "extrusion", "name": f.get("name", "Gear"), "plane": f["plane"], "operation": "new",
            "offset": f.get("offset", 0), "direction": f.get("direction", 1), "distance": f["distance"],
            "sketch": [{"shape": "polygon", "points": pts}]}


def expand_mirrors(spec):
    """Spec without ``mirror`` and ``gear`` features: mirrors are replaced by the mirrored copies of the features
    they list and gears by an extrusion with their outline. Used wherever the geometry has to be evaluated (IoU,
    previews)."""
    feats = spec["features"]
    if not any(f["type"] in ("mirror", "gear") for f in feats):
        return spec
    by_name = {}
    out = []
    for f in feats:
        if f["type"] == "gear":
            f = gear_to_extrusion(f)
        if f["type"] != "mirror":
            by_name[f.get("name")] = f
            out.append(f)
            continue
        axis, c = mirror_axis(f["plane"]), f.get("offset", 0)
        for name in f["features"]:
            r = reflect_feature(by_name[name], axis, c)
            if r is None:
                raise ValueError("feature '%s' cannot be mirrored" % name)
            r["name"] = name + " (mirrored)"
            out.append(r)
    new = dict(spec)
    new["features"] = out
    return new


def validate(spec):
    """Return ``(errors, warnings)``."""
    errors, warnings = [], []
    feats = spec.get("features", [])
    if not feats:
        errors.append("No features defined.")
    if feats and feats[0].get("operation", "new") == "cut":
        errors.append("The first feature cannot be a cut.")
    for i, f in enumerate(feats, 1):
        t = f.get("type")
        tag = "feature %d (%s)" % (i, t)
        if t == "extrusion":
            if f.get("plane", "XY") not in PLANES:
                errors.append("%s: invalid plane." % tag)
            if f.get("operation", "new") not in OPERATIONS:
                errors.append("%s: invalid operation." % tag)
            d = f.get("distance")
            if d != THROUGH_ALL and not (isinstance(d, (int, float)) and d > 0):
                errors.append("%s: distance must be a number > 0 or '%s'." % (tag, THROUGH_ALL))
            if d == THROUGH_ALL and f.get("operation") != "cut":
                errors.append("%s: '%s' is only valid for cuts." % (tag, THROUGH_ALL))
            if not f.get("sketch"):
                errors.append("%s: empty sketch." % tag)
            for s in sketch_shapes(f.get("sketch", [])):
                shape = s.get("shape")
                if shape not in SHAPES:
                    errors.append("%s: unknown shape '%s'." % (tag, shape))
                elif shape == "circle" and not s.get("d", 0) > 0:
                    errors.append("%s: circle with invalid diameter." % tag)
                elif shape == "rectangle" and not (s.get("width", 0) > 0 and s.get("height", 0) > 0):
                    errors.append("%s: rectangle with invalid dimensions." % tag)
                elif shape == "slot" and not (s.get("width", 0) > 0 and
                                              (s["x1"], s["y1"]) != (s["x2"], s["y2"])):
                    errors.append("%s: invalid slot." % tag)
                elif shape == "polygon" and len(s.get("points", [])) < 3:
                    errors.append("%s: polygon needs >= 3 points." % tag)
                elif shape == "contour" and not s.get("segments"):
                    errors.append("%s: contour without segments." % tag)
            if f.get("direction", 1) not in (1, -1):
                errors.append("%s: direction must be 1 or -1." % tag)
        elif t == "hole":
            if f.get("plane") not in PLANES or not f.get("points"):
                errors.append("%s: invalid plane/points." % tag)
            if f.get("subtype", "simple") not in ("simple", "countersink", "counterbore"):
                errors.append("%s: invalid subtype." % tag)
            if not (f.get("d", 0) > 0 and f.get("depth", 0) > 0):
                errors.append("%s: d and depth must be > 0." % tag)
            if f.get("subtype") in ("countersink", "counterbore") and not f.get("head_d", 0) > f.get("d", 0):
                errors.append("%s: head_d must be larger than d." % tag)
            if f.get("direction", 1) not in (1, -1):
                errors.append("%s: direction must be 1 or -1." % tag)
        elif t == "gear":
            if f.get("plane") not in PLANES:
                errors.append("%s: invalid plane." % tag)
            if not f.get("module", 0) > 0:
                errors.append("%s: module must be > 0." % tag)
            if int(f.get("teeth", 0)) < 6:
                errors.append("%s: a gear needs at least 6 teeth." % tag)
            if not 5 <= f.get("pressure_angle", 0) <= 30:
                errors.append("%s: pressure_angle must be between 5 and 30 degrees." % tag)
            if not f.get("distance", 0) > 0:
                errors.append("%s: distance (face width) must be > 0." % tag)
            if len(f.get("center", [])) != 2:
                errors.append("%s: center must be [x, y]." % tag)
        elif t == "mirror":
            if f.get("plane") not in PLANES:
                errors.append("%s: invalid plane." % tag)
            earlier = {g.get("name") for g in feats[:i - 1] if g.get("type") != "mirror"}
            listed = f.get("features") or []
            if not listed:
                errors.append("%s: no features to mirror." % tag)
            for name in listed:
                if name not in earlier:
                    errors.append("%s: unknown feature '%s' (it must come earlier in the list)." % (tag, name))
        elif t == "revolve":
            p = f.get("profile", [])
            if len(p) < 3:
                errors.append("%s: profile needs >= 3 points." % tag)
            if any(pt[0] < 0 for pt in p):
                errors.append("%s: negative radius in profile (x must be >= 0)." % tag)
        elif t in ("fillet", "chamfer"):
            selection = f.get("selection", "all")
            if selection in BASE_OUTLINE_SELECTIONS:
                base = feats[0]
                if t != "fillet" or spec.get("system") != "model" or base.get("type") != "extrusion" \
                        or base.get("operation") != "new" or i != 2:
                    errors.append("%s: %s needs a model-system fillet right after the base extrusion." % (
                        tag, selection))
                elif isinstance(base["distance"], (int, float)) and f.get("radius", 0) > base["distance"]:
                    errors.append("%s: the radius is larger than the base extrusion." % tag)
            elif selection not in SELECTIONS:
                errors.append("%s: invalid selection." % tag)
            v = f.get("radius" if t == "fillet" else "distance", 0)
            if not v > 0:
                errors.append("%s: value must be > 0." % tag)
        else:
            errors.append("%s: unknown feature type." % tag)

    # Hole outside the base rectangle (simple check, legacy format only)
    if feats and feats[0].get("type") == "extrusion" and spec.get("system") != "model":
        rects = [s for s in feats[0]["sketch"] if s.get("shape") == "rectangle"]
        if len(rects) == 1:
            r = rects[0]
            for j, f in enumerate(feats[1:], 2):
                if f.get("type") == "extrusion" and f.get("operation") == "cut":
                    for s in sketch_shapes(f["sketch"]):
                        if s.get("shape") == "circle":
                            rr = s["d"] / 2.0
                            if (s["x"] - rr < r["x"] or s["y"] - rr < r["y"] or
                                    s["x"] + rr > r["x"] + r["width"] or
                                    s["y"] + rr > r["y"] + r["height"]):
                                warnings.append("feature %d: hole at (%.1f, %.1f) leaves the base part." %
                                                (j, s["x"], s["y"]))
                            else:
                                m = min(s["x"] - rr - r["x"], s["y"] - rr - r["y"],
                                        r["x"] + r["width"] - s["x"] - rr,
                                        r["y"] + r["height"] - s["y"] - rr)
                                if m < s["d"] * 0.5:
                                    warnings.append("feature %d: hole at (%.1f, %.1f) leaves a thin wall "
                                                    "to the edge (%.1f mm)." % (j, s["x"], s["y"], m))
    return errors, warnings
