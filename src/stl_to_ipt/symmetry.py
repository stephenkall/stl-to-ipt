"""Find mirrored features and replace the duplicate by a ``mirror`` feature.

A person who models a symmetric part draws one side and mirrors it. Given a symmetry plane (normal axis ``a`` at
coordinate ``c``) this module finds:

* pairs of features that are mirror images of each other (``Pad 0 to 3`` and ``Pad 67 to 70``): the second one is
  dropped and the first one is mirrored;
* single features whose shapes (or hole centers) come in mirror twins: only one of each twin is kept.

The base feature is never mirrored: it is drawn complete. Everything is geometry-based and tolerant of small
differences (``tol`` mm), because the features come from a mesh.
"""
import copy

from . import spec as sp

CENTER_TOL = 0.3  # a shape whose center is this close to the plane is "on" the plane


def _range_along(f, axis, extent):
    """Interval covered by an extrusion/hole feature along global ``axis`` when the plane normal is that axis."""
    off = f.get("offset", 0)
    d = f["depth"] if f["type"] == "hole" else f["distance"]
    if d == sp.THROUGH_ALL:
        return (0.0, extent)
    a, b = off, off + d * f.get("direction", 1)
    return (min(a, b), max(a, b))


def _region_difference(f1, f2):
    g1, g2 = sp.sketch_geometry(f1["sketch"]), sp.sketch_geometry(f2["sketch"])
    return g1.symmetric_difference(g2).area, max(g1.area, g2.area, 1e-9)


def equivalent(f1, f2, axis, extent, tol=0.15):
    """True when two features produce the same material change."""
    if f1["type"] != f2["type"] or f1["plane"] != f2["plane"]:
        return False
    n = sp.PLANE_AXES[f1["plane"]][0]
    if f1["type"] == "extrusion":
        if f1["operation"] != f2["operation"]:
            return False
        lo1, hi1 = _range_along(f1, n, extent[n])
        lo2, hi2 = _range_along(f2, n, extent[n])
        if abs(lo1 - lo2) > tol or abs(hi1 - hi2) > tol:
            return False
        diff, area = _region_difference(f1, f2)
        return diff <= max(0.01 * area, 0.5)
    if f1["type"] == "hole":
        if f1.get("subtype") != f2.get("subtype") or abs(f1["d"] - f2["d"]) > tol:
            return False
        if abs(f1["depth"] - f2["depth"]) > tol:
            return False
        lo1, hi1 = _range_along(f1, n, extent[n])
        lo2, hi2 = _range_along(f2, n, extent[n])
        if abs(lo1 - lo2) > tol or abs(hi1 - hi2) > tol:
            return False
        return _same_points(f1["points"], f2["points"], tol)
    return False


def _same_points(p1, p2, tol):
    if len(p1) != len(p2):
        return False
    return all(any(abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol for b in p2) for a in p1)


def _shape_center(s, k):
    """Coordinate of the shape's center along sketch axis ``k`` (0 = x, 1 = y)."""
    g = sp.shape_to_geometry(s) if "pattern" not in s else sp.shape_to_geometry(sp.expand_shape(s)[0])
    return g.centroid.x if k == 0 else g.centroid.y


def _twin_split(f, axis, c, tol=0.15):
    """For a feature whose plane contains ``axis`` as a sketch axis: split its shapes (or hole points) into
    (kept, twins). Return None unless every shape is either paired with a mirror twin or centered on the plane
    and at least one pair exists."""
    n, A, B = sp.PLANE_AXES[f["plane"]]
    k = 0 if axis == A else 1
    if f["type"] == "hole":
        pts = f["points"]
        kept, used = [], set()
        pairs = 0
        for i, p in enumerate(pts):
            if i in used:
                continue
            if abs(p[k] - c) <= CENTER_TOL:
                kept.append(p)
                continue
            twin = next((j for j, q in enumerate(pts)
                         if j != i and j not in used and abs(q[k] - (2 * c - p[k])) <= tol and abs(q[1 - k] - p[1 - k]) <= tol), None)
            if twin is None:
                return None
            used.update((i, twin))
            kept.append(p)
            pairs += 1
        if pairs == 0:
            return None
        return {"points": kept}
    shapes = f["sketch"]
    # only independent cutters can be split: in a join/new sketch nested shapes are holes of their neighbor, and
    # mirroring "contour minus one hole" would fill the hole again
    if f.get("operation") != "cut" or len(shapes) < 2:
        return None
    reflected = [sp._reflect_shape(s, k, c) for s in shapes]
    if any(r is None for r in reflected):
        return None
    geoms = [sp.shape_to_geometry(s) for s in shapes]
    rgeoms = [sp.shape_to_geometry(r) for r in reflected]
    used, kept, pairs = set(), [], 0
    for i, g in enumerate(geoms):
        if i in used:
            continue
        center = g.centroid.x if k == 0 else g.centroid.y
        if abs(center - c) <= CENTER_TOL:
            if g.symmetric_difference(rgeoms[i]).area > max(0.02 * g.area, 0.3):
                return None  # on the plane but not symmetric by itself
            kept.append(i)
            continue
        twin = next((j for j in range(len(geoms)) if j != i and j not in used
                     and geoms[j].symmetric_difference(rgeoms[i]).area <= max(0.02 * g.area, 0.3)), None)
        if twin is None:
            return None
        used.update((i, twin))
        kept.append(i)
        pairs += 1
    if pairs == 0:
        return None
    return {"sketch": [shapes[i] for i in kept]}


def mirror_features(features, axis, c, extent):
    """Rewrite ``features`` using a mirror feature about the plane (normal ``axis``, coordinate ``c``).

    Return ``(new_features, saved)`` where ``saved`` counts the removed features and shapes, or ``(features, 0)``
    when nothing can be mirrored."""
    feats = copy.deepcopy(features)
    plane_name = next(p for p, v in sp.PLANE_AXES.items() if v[0] == axis)
    mirrored, drop = [], set()
    saved = 0
    for i in range(1, len(feats)):
        if i in drop or feats[i]["type"] not in ("extrusion", "hole"):
            continue
        f = feats[i]
        n = sp.PLANE_AXES[f["plane"]][0]
        r = sp.reflect_feature(f, axis, c)
        if r is None:
            continue
        # (c) one feature with shapes / points in mirror twins (checked first: such a feature is "symmetric" as
        #     a whole, but it can still be halved)
        if axis != n:
            split = _twin_split(f, axis, c)
            if split:
                before = len(f.get("points", f.get("sketch", [])))
                f.update(split)
                saved += before - len(f.get("points", f.get("sketch", [])))
                mirrored.append(i)
                continue
        # (a) the mirror image is another feature of the list
        partner = next((j for j in range(i + 1, len(feats)) if j not in drop
                        and feats[j]["type"] == f["type"] and equivalent(r, feats[j], axis, extent)), None)
        if partner is not None and not equivalent(f, r, axis, extent):
            drop.add(partner)
            mirrored.append(i)
            saved += 1
        # (b) a feature that is its own mirror image stays as it is
    if not mirrored:
        return features, 0
    last = max(mirrored)
    out = []
    names = [feats[i]["name"] for i in mirrored]
    for i, f in enumerate(feats):
        if i in drop:
            continue
        out.append(f)
        if i == last:
            out.append({"type": "mirror", "name": "Mirror %s" % plane_name, "plane": plane_name,
                        "offset": c, "features": names})
    return out, saved


def unique_names(features):
    """Give every feature a unique name (parameters and mirrors refer to features by name)."""
    seen = {}
    for f in features:
        base = f.get("name", f["type"])
        n = seen.get(base, 0) + 1
        seen[base] = n
        if n > 1:
            f["name"] = "%s %d" % (base, n)
    return features
