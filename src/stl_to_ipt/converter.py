"""STL -> spec JSON (``"model"`` system) with CLEAN contours: lines, arcs and circles fitted to the mesh.

Strategy (few features, sketches on the part's natural plane):
  1. Pick the extrusion axis along which the part looks most like a prism (constant cross-section).
     On a tie prefer Z (sketch on XY), then Y, then X.
  2. Base body = cross-section (inner holes in the same sketch) extruded over the full length.
  3. Through cuts on the other axes = whatever lies outside the mesh silhouette (rounded corners,
     transverse holes). Only cuts that really remove material are kept.
  4. Contours: the mesh outline is segmented into lines and arcs by a tolerance fit (``--tol``, default
     0.12 mm); tessellation ripples vanish, near-horizontal/vertical lines become exact, circles become
     circles.
  5. Countersunk and counterbored holes are detected from the section stack and become hole features.
"""
import json
import math
import os
import warnings

import numpy as np
import shapely
from shapely.geometry import Polygon, box

from . import fits, gears, patterns, rounding
from . import spec as sp
from . import symmetry

warnings.filterwarnings("ignore")
PLANE_NAME = {2: "XY", 1: "XZ", 0: "YZ"}
AXIS_NAME = "XYZ"
THROUGH_CUT_PREFIX = "Through cut"


# ----------------------------------------------------------------------------- mesh
def load_mesh(path):
    import trimesh
    m = trimesh.load(path, force="mesh")
    m.merge_vertices()
    m.metadata["original_min"] = [float(v) for v in m.bounds[0]]
    m.apply_translation(-m.bounds[0])
    return m


def section(m, n, level):
    """2D region (A, B) of the mesh cross-section along axis ``n`` at ``level`` (mm)."""
    _, A, B = sp.PLANE_AXES[PLANE_NAME[n]]
    org = np.zeros(3)
    org[n] = level
    nor = np.zeros(3)
    nor[n] = 1
    s = m.section(plane_origin=org, plane_normal=nor)
    if s is None:
        return None
    g = None
    for loop in s.discrete:
        if len(loop) < 4:
            continue
        p = Polygon(loop[:, [A, B]])
        if not p.is_valid:
            p = p.buffer(0)
        if p.area < 1e-6:
            continue
        g = p if g is None else g.symmetric_difference(p)
    return g


def silhouette(m, n):
    """Exact union of the triangles projected along axis ``n``."""
    _, A, B = sp.PLANE_AXES[PLANE_NAME[n]]
    tri = m.triangles[:, :, [A, B]]
    area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) -
                        (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    tri = tri[area > 1e-9]
    polys = shapely.polygons(tri)
    g = shapely.union_all(polys)
    return g.buffer(0.02, 4).buffer(-0.02, 4)


# ----------------------------------------------------------------------------- contour fitting
def _fit_circle(pts):
    """Algebraic (Kasa) circle fit -> (cx, cy, r)."""
    p = np.asarray(pts, float)
    A = np.c_[2 * p[:, 0], 2 * p[:, 1], np.ones(len(p))]
    b = (p ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0], sol[1]
    r = math.sqrt(max(sol[2] + cx * cx + cy * cy, 0))
    return cx, cy, r


def _snap(v, step=0.05, tol=0.03):
    """Values within ``tol`` of a multiple of ``step`` become that multiple (6.998 -> 7.0)."""
    r = round(v / step) * step
    return round(r, 3) if abs(v - r) < tol else round(v, 3)


def _circle_deviation(P, cx, cy, r, closed=False):
    """Largest deviation from the circle, checking vertices AND edge midpoints (the vertices of a rectangle
    are concyclic; the edge midpoints reveal that it is not an arc)."""
    Q = np.vstack([P, P[:1]]) if closed else P
    M = (Q[:-1] + Q[1:]) / 2.0
    X = np.vstack([P, M])
    return float(np.abs(np.hypot(X[:, 0] - cx, X[:, 1] - cy) - r).max())


def _line_distance(p, a, b):
    ab = b - a
    length = np.hypot(*ab)
    if length < 1e-12:
        return np.hypot(*(p - a).T)
    return np.abs(ab[0] * (p[:, 1] - a[1]) - ab[1] * (p[:, 0] - a[0])) / length


def fit_run(P, tol, segs):
    """Approximate the open run ``P`` (N, 2) by lines/arcs; append the fitted items to ``segs`` (tuples).
    Each item is ("L", x, y) or ("A", mx, my, x, y) and always ends on the last point of the run."""
    n = len(P)
    a, b = P[0], P[-1]
    if n <= 2 or _line_distance(P, a, b).max() <= tol:
        segs.append(("L", b[0], b[1]))
        return
    if n >= 4:
        cx, cy, r = _fit_circle(P)
        if 0 < r < 3000:
            dev = _circle_deviation(P, cx, cy, r)
            ang = np.unwrap(np.arctan2(P[:, 1] - cy, P[:, 0] - cx))
            sweep = abs(ang[-1] - ang[0])
            steps = np.abs(np.diff(ang))
            # real arc: low error, sweep between 5 and 300 degrees, regular angular steps
            if (dev <= tol and math.radians(5) < sweep < math.radians(300) and
                    steps.max() < 2.5 * max(steps.mean(), 1e-9) + math.radians(2)):
                am = (ang[0] + ang[-1]) / 2.0
                segs.append(("A", cx + r * math.cos(am), cy + r * math.sin(am), b[0], b[1]))
                return
    k = int(np.argmax(_line_distance(P, a, b)))
    k = min(max(k, 1), n - 2)
    fit_run(P[:k + 1], tol, segs)
    fit_run(P[k:], tol, segs)


def _circle3(a, m, b):
    """Circle through 3 points -> (cx, cy, r), or None."""
    ax, ay = a
    bx, by = m
    cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _arc_mid(center, r, p0, p1, ccw):
    """Point halfway along the arc of circle (center, r) that goes from p0 to p1."""
    a0 = math.atan2(p0[1] - center[1], p0[0] - center[0])
    a1 = math.atan2(p1[1] - center[1], p1[0] - center[0])
    tau = 2 * math.pi
    sweep = (a1 - a0) % tau if ccw else -((a0 - a1) % tau)
    am = a0 + sweep / 2.0
    return center[0] + r * math.cos(am), center[1] + r * math.sin(am)


def _is_ccw(p0, pm, p1):
    return (pm[0] - p0[0]) * (p1[1] - p0[1]) - (pm[1] - p0[1]) * (p1[0] - p0[0]) > 0


def _same_circle(c1, c2, tol):
    """Same circle within ``tol`` mm, but never looser than 5% of the radius (tiny arcs must stay tiny)."""
    if not (c1 and c2):
        return False
    tol = min(tol, 0.05 * min(c1[2], c2[2]))
    return math.hypot(c1[0] - c2[0], c1[1] - c2[1]) < tol and abs(c1[2] - c2[2]) < tol


def _merge_arcs_and_tangents(p0, segs, tol=0.2, short=6.0):
    """Clean up a fitted contour (a closed list of ``("L", x, y)`` / ``("A", mx, my, x, y)``):

    (1) consecutive arcs on the same circle become one arc, also across the start vertex;
    (2) a short faceting line between a long line and an arc tangent to it disappears (the arc then starts
        at the tangent point);
    (3) two arcs on tangent circles that are separated by a few short pieces (tessellation debris around the
        inflection point of an S-curve) are joined directly at the tangent point.
    """
    pts = [np.array(p0, float)] + [np.array(sg[-2:], float) for sg in segs]
    segs = [list(sg) for sg in segs]

    def circle_of(k):
        return _circle3(pts[k], segs[k][1:3], pts[k + 1])

    changed = True
    while changed:
        changed = False
        n = len(segs)
        # (1a) wrap-around: last and first arc on the same circle -> rotate the start vertex
        if n >= 3 and segs[-1][0] == "A" and segs[0][0] == "A" and _same_circle(circle_of(n - 1), circle_of(0), tol):
            mid = sp.sample_arc(tuple(pts[n - 1]), tuple(pts[0]), tuple(pts[1]), 2)[1]
            merged = ["A", mid[0], mid[1], pts[1][0], pts[1][1]]
            pts = [pts[n - 1]] + pts[1:n]
            segs = [merged] + segs[1:n - 1]
            changed = True
            continue
        # (1b) consecutive arcs on the same circle
        for i in range(n - 1):
            if segs[i][0] == "A" and segs[i + 1][0] == "A" and _same_circle(circle_of(i), circle_of(i + 1), tol):
                mid = sp.sample_arc(tuple(pts[i]), tuple(pts[i + 1]), tuple(pts[i + 2]), 2)[1]
                segs[i] = ["A", mid[0], mid[1], pts[i + 2][0], pts[i + 2][1]]
                del segs[i + 1]
                del pts[i + 1]
                changed = True
                break
        if changed:
            continue
        # (3) bridge two tangent circles across a few short pieces
        for i in range(n):
            if segs[i][0] != "A":
                continue
            for j in range(i + 1, min(n, i + 4)):
                if segs[j][0] != "A":
                    continue
                gap = sum(float(np.hypot(*(pts[k + 1] - pts[k]))) for k in range(i + 1, j))
                if gap > short or j == i + 1:
                    continue
                c1, c2 = circle_of(i), circle_of(j)
                if not c1 or not c2:
                    continue
                d = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
                tol_t = min(0.4, 0.1 * min(c1[2], c2[2]))  # relative: small arcs need a tight match
                if abs(d - (c1[2] + c2[2])) < tol_t:  # external tangency (S-curve)
                    t = np.array([c1[0] + (c2[0] - c1[0]) * c1[2] / (c1[2] + c2[2]),
                                  c1[1] + (c2[1] - c1[1]) * c1[2] / (c1[2] + c2[2])])
                elif abs(c1[2] - c2[2]) > 0.3 and abs(d - abs(c1[2] - c2[2])) < tol_t:  # internal tangency
                    big, small = (c1, c2) if c1[2] > c2[2] else (c2, c1)
                    u = np.array([small[0] - big[0], small[1] - big[1]])
                    t = np.array([big[0], big[1]]) + u / max(np.hypot(*u), 1e-9) * big[2]
                else:
                    continue
                ccw_i = _is_ccw(pts[i], segs[i][1:3], pts[i + 1])
                ccw_j = _is_ccw(pts[j], segs[j][1:3], pts[j + 1])
                end_j = pts[j + 1]
                mi = _arc_mid(c1[:2], c1[2], pts[i], t, ccw_i)
                mj = _arc_mid(c2[:2], c2[2], t, end_j, ccw_j)
                segs[i] = ["A", mi[0], mi[1], t[0], t[1]]
                segs[j] = ["A", mj[0], mj[1], end_j[0], end_j[1]]
                pts[i + 1] = t
                del segs[i + 1:j]
                del pts[i + 2:j + 1]
                changed = True
                break
            if changed:
                break
        if changed:
            continue
        # (2) short faceting line between a long line and a tangent arc
        n = len(segs)
        for i in range(1, n - 1):
            if segs[i][0] != "L" or np.hypot(*(pts[i + 1] - pts[i])) > short:
                continue
            for line_i, arc_i in ((i - 1, i + 1), (i + 1, i - 1)):
                if segs[line_i][0] != "L" or segs[arc_i][0] != "A":
                    continue
                a, b = (pts[line_i], pts[line_i + 1])
                if np.hypot(*(b - a)) < 3 * np.hypot(*(pts[i + 1] - pts[i])):
                    continue
                c = _circle3(pts[arc_i], segs[arc_i][1:3], pts[arc_i + 1])
                if not c:
                    continue
                center = np.array(c[:2])
                dist = float(_line_distance(center[None, :], a, b)[0])
                if abs(dist - c[2]) > tol:
                    continue
                u = (b - a) / np.hypot(*(b - a))
                t = a + u * float(np.dot(center - a, u))  # foot of the perpendicular = tangent point
                pts[i] = t
                pts[i + 1] = t
                del segs[i]  # remove the short line
                del pts[i + 1 if arc_i == i + 1 else i]
                changed = True
                break
            if changed:
                break
    _slide_to_tangent_points(pts, segs, tol)
    out = []
    for k, sg in enumerate(segs):
        e = pts[k + 1]
        out.append(("L", float(e[0]), float(e[1])) if sg[0] == "L"
                   else ("A", float(sg[1]), float(sg[2]), float(e[0]), float(e[1])))
    return pts[0], out


def _slide_to_tangent_points(pts, segs, tol, max_slide=3.0):
    """A line and an arc whose circle touches the line meet exactly at the tangent point. Fitting often ends the
    arc a vertex or two early; slide the junction along the line to the true tangent point."""
    n = len(segs)
    for k in range(n):
        k2 = (k + 1) % n  # the last segment also meets the first one (the start vertex)
        kinds = (segs[k][0], segs[k2][0])
        if kinds not in (("L", "A"), ("A", "L")):
            continue
        li, ai = (k, k2) if kinds == ("L", "A") else (k2, k)
        a, b = pts[li], pts[li + 1]
        length = float(np.hypot(*(b - a)))
        c = _circle3(pts[ai], segs[ai][1:3], pts[ai + 1])
        if not c or length < 1e-6:
            continue
        center = np.array(c[:2])
        if abs(float(_line_distance(center[None, :], a, b)[0]) - c[2]) > tol:
            continue
        u = (b - a) / length
        t = a + u * float(np.dot(center - a, u))
        junction = pts[k + 1]
        slide = float(np.hypot(*(t - junction)))
        if slide < 0.02 or slide > max_slide or slide > 0.5 * length:
            continue
        ccw = _is_ccw(pts[ai], segs[ai][1:3], pts[ai + 1])
        pts[k + 1] = t
        if k + 1 == n:  # junction on the start vertex: keep the closing point in sync
            pts[0] = t
        mid = _arc_mid(c[:2], c[2], pts[ai], pts[ai + 1], ccw)
        segs[ai][1], segs[ai][2] = mid


def ring_to_shape(coords, tol, bounds):
    """Closed ring (list of points) -> spec shape (``circle`` or ``contour``)."""
    P = np.asarray(coords, float)
    if np.allclose(P[0], P[-1]):
        P = P[:-1]
    P = np.asarray(Polygon(P).simplify(0.02).exterior.coords)[:-1]
    # a whole circle?
    cx, cy, r = _fit_circle(P)
    if 0 < r < 3000 and _circle_deviation(P, cx, cy, r, True) <= tol and len(P) >= 8:
        return {"shape": "circle", "x": _snap(cx), "y": _snap(cy), "d": _snap(2 * r)}
    n = len(P)
    # corners: concentrated curvature (large turn angle)
    v1 = P - np.roll(P, 1, axis=0)
    v2 = np.roll(P, -1, axis=0) - P
    turn = np.degrees(np.abs(np.arctan2(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0],
                                        (v1 * v2).sum(1))))
    corners = [i for i in range(n) if turn[i] > 22]
    if not corners:  # smooth but not circular ring: split into 4 pieces
        corners = [0, n // 4, n // 2, 3 * n // 4]
    # start at the corner closest to the origin (lower left)
    first = min(corners, key=lambda i: P[i, 0] + P[i, 1])
    corners = sorted(corners, key=lambda i: (i - first) % n)
    segs = []
    for j, c in enumerate(corners):
        nxt = corners[(j + 1) % len(corners)]
        idx = [(c + k) % n for k in range(((nxt - c) % n or n) + 1)]
        fit_run(P[idx], tol, segs)
    p0, segs = _guarded(_merge_arcs_and_tangents, P[first], segs)
    p0, segs = _align(p0, segs, bounds)
    p0, segs = _regularize_if_faithful(p0, segs)
    return {"shape": "contour", "start": [round(float(p0[0]), 3), round(float(p0[1]), 3)],
            "segments": [[s[0]] + [round(float(v), 3) for v in s[1:]] for s in segs]}


def _contour_polygon(p0, segs):
    from shapely.geometry import Polygon
    c = {"start": [float(p0[0]), float(p0[1])],
         "segments": [[sg[0]] + [float(v) for v in sg[1:]] for sg in segs]}
    pts = sp.contour_points(c, 96)  # fine sampling: coarse chords would look like a change of shape
    poly = Polygon(pts)
    return poly if poly.is_valid else poly.buffer(0)


def _regularize_if_faithful(p0, segs, max_change=0.002):
    """Regularize (clean numbers, aligned centers) unless that changes the contour area by more than
    ``max_change`` (relative): dense contours with many tiny features, like gear teeth, must stay as fitted."""
    return _guarded(_regularize, p0, segs, max_change=max_change)


def _guarded(transform, p0, segs, max_change=0.002):
    """Apply ``transform(p0, segs) -> (p0, segs)`` only if the contour keeps its shape (area change below
    ``max_change`` of the area, or 0.3 mm2)."""
    new_p0, new_segs = transform(p0, segs)
    try:
        before, after = _contour_polygon(p0, segs), _contour_polygon(new_p0, new_segs)
        changed = before.symmetric_difference(after).area
        if changed > max(max_change * before.area, 0.3):
            return p0, segs
    except Exception:
        return p0, segs
    return new_p0, new_segs


def _regularize(p0, segs, cluster_tol=0.06):
    """Make a fitted contour internally consistent and made of clean numbers.

    Vertex coordinates and arc centers that agree within ``cluster_tol`` become exactly equal (so tangent points
    line up with the centers they belong to), every coordinate is rounded with ``rounding.nice`` and every arc
    radius too; arc mid points are recomputed on the rounded circle."""
    n = len(segs)
    pts = [np.array(p0, float)] + [np.array(sg[-2:], float) for sg in segs]
    circles = {}
    for k, sg in enumerate(segs):
        if sg[0] == "A":
            c = _circle3(pts[k], sg[1:3], pts[k + 1])
            if c:
                circles[k] = (c, _is_ccw(pts[k], sg[1:3], pts[k + 1]))
    # one coordinate pool per axis: the vertices (the closing vertex is the start vertex) and the arc centers
    refs = [(i, a) for i in range(n) for a in (0, 1)] + [(("c", k), a) for k in circles for a in (0, 1)]
    values = []
    for key, a in refs:
        values.append(float(pts[key][a]) if not isinstance(key, tuple) else float(circles[key[1]][0][a]))
    snapped = rounding.cluster_values(values, cluster_tol)
    new_pts = [np.zeros(2) for _ in range(n)]
    new_center = {}
    for (key, a), v in zip(refs, snapped):
        if isinstance(key, tuple):
            new_center.setdefault(key[1], [0.0, 0.0])[a] = v
        else:
            new_pts[key][a] = v
    new_pts.append(new_pts[0].copy())
    out = []
    for k, sg in enumerate(segs):
        e = new_pts[k + 1]
        if sg[0] == "L" or k not in circles:
            out.append(("L", float(e[0]), float(e[1])) if sg[0] == "L"
                       else ("A", float(sg[1]), float(sg[2]), float(e[0]), float(e[1])))
            continue
        (cx, cy, r), ccw = circles[k]
        c = new_center[k]
        d0 = math.hypot(new_pts[k][0] - c[0], new_pts[k][1] - c[1])
        d1 = math.hypot(e[0] - c[0], e[1] - c[1])
        r_new = rounding.nice(r)
        if abs(d0 - r_new) > 0.08 or abs(d1 - r_new) > 0.08:  # the rounded circle misses an end point
            r_new = rounding.nice((d0 + d1) / 2.0)
        mid = _arc_mid(c, r_new, new_pts[k], e, ccw)
        out.append(("A", mid[0], mid[1], float(e[0]), float(e[1])))
    return new_pts[0], out


def _align(p0, segs, bounds, eps=0.06):
    """Near-horizontal/vertical lines become exact; values near 0 / the extent snap to them;
    consecutive collinear lines are merged; the contour is closed on its start point."""
    pts = [np.array(p0, float)] + [np.array(s[-2:], float) for s in segs]
    for p in pts:  # nearly round values (multiples of 0.5 mm within 0.03 mm) become round
        for k in (0, 1):
            r = round(p[k] * 2) / 2.0
            if abs(p[k] - r) < 0.03:
                p[k] = r
    kinds = [s[0] for s in segs]
    slope = math.tan(math.radians(0.5))
    for i, t in enumerate(kinds):
        if t == "L":
            a, b = pts[i], pts[i + 1]
            dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
            if dx < max(eps, slope * dy) and dy > eps:
                b[0] = a[0]
            elif dy < max(eps, slope * dx) and dx > eps:
                b[1] = a[1]
    pts[-1] = pts[0].copy()
    for p in pts:
        for k in (0, 1):
            for target in bounds[k]:
                if abs(p[k] - target) < eps:
                    p[k] = target
    segs = list(segs)
    kinds = [sg[0] for sg in segs]
    found = True
    while found and len(kinds) > 3:  # lines shorter than 0.4 mm (corners chamfered by tessellation)
        found = False
        for i, t in enumerate(kinds):
            if t == "L" and np.hypot(*(pts[i + 1] - pts[i])) < 0.4:
                m = (pts[i] + pts[i + 1]) / 2.0
                pts[i] = m
                del pts[i + 1]
                del segs[i]
                del kinds[i]
                pts[0] = pts[0] if i != len(kinds) else m
                pts[-1] = pts[0].copy()
                found = True
                break
    out = []
    for i, s in enumerate(segs):
        e = pts[i + 1]
        if s[0] == "L":
            if out and out[-1][0] == "L":  # collinear with the previous one?
                a = pts[i - 1] if i > 0 else pts[0]
                start = pts[i]
                u, v = start - a, e - start
                if (np.hypot(*u) > 0 and np.hypot(*v) > 0 and
                        abs(u[0] * v[1] - u[1] * v[0]) < 1e-3 * np.hypot(*u) * np.hypot(*v) and
                        (u * v).sum() > 0):
                    out[-1] = ("L", e[0], e[1])
                    continue
            if np.hypot(*(e - pts[i])) < 1e-6:
                continue
            out.append(("L", e[0], e[1]))
        else:
            out.append(("A", s[1], s[2], e[0], e[1]))
    return pts[0], out


def _as_rectangle(f):
    """Contour of 4 axis-aligned lines -> ``rectangle`` dict (or None)."""
    if f["shape"] != "contour" or any(sg[0] != "L" for sg in f["segments"]) or len(f["segments"]) != 4:
        return None
    pts = [tuple(f["start"])] + [(sg[1], sg[2]) for sg in f["segments"]]
    xs = sorted({round(q[0], 3) for q in pts})
    ys = sorted({round(q[1], 3) for q in pts})
    if len(xs) != 2 or len(ys) != 2:
        return None
    for a, b in zip(pts, pts[1:]):
        if a[0] != b[0] and a[1] != b[1]:
            return None
    return {"shape": "rectangle", "x": xs[0], "y": ys[0], "width": round(xs[1] - xs[0], 3),
            "height": round(ys[1] - ys[0], 3)}


def compact(shapes):
    """Rectangles become ``rectangle``; equal shapes in a row with constant pitch become a linear ``pattern``."""
    shapes = [(_as_rectangle(f) or f) for f in shapes]

    def key(f):
        return (f["shape"], f.get("width"), f.get("height"), f.get("d"))

    used, out = set(), []
    for i, f in enumerate(shapes):
        if i in used:
            continue
        if f["shape"] in ("rectangle", "circle"):
            for axis, other in (("x", "y"), ("y", "x")):
                row = sorted([j for j, g in enumerate(shapes)
                              if j not in used and key(g) == key(f) and abs(g[other] - f[other]) < 1e-3],
                             key=lambda j: shapes[j][axis])
                if len(row) >= 3:
                    v = [shapes[j][axis] for j in row]
                    pitches = {round(b - a, 2) for a, b in zip(v, v[1:])}
                    if len(pitches) == 1:
                        base = dict(shapes[row[0]])
                        base["pattern"] = {"type": "linear", "nx": len(row) if axis == "x" else 1,
                                           "ny": len(row) if axis == "y" else 1,
                                           "dx": pitches.pop() if axis == "x" else 0,
                                           "dy": pitches.pop() if axis == "y" else 0}
                        used.update(row)
                        out.append(base)
                        break
        if i not in used:
            used.add(i)
            out.append(f)
    return _circle_rings(out)


def _circle_rings(shapes):
    """Equal circles equally spaced on a circle become one circle with a circular pattern."""
    groups = patterns.circle_ring(shapes)
    if not groups:
        return shapes
    drop = {i for idx, _ in groups for i in idx[1:]}
    out = []
    for i, s in enumerate(shapes):
        if i in drop:
            continue
        for idx, pat in groups:
            if idx[0] == i:
                s = dict(shapes[i])
                s["pattern"] = {"type": "circular", "n": pat["n"], "cx": pat["cx"], "cy": pat["cy"]}
        out.append(s)
    return out


def region_shapes(g, tol, bounds):
    """Shapely region (Polygon/Multi) -> list of shapes (outer ring + holes)."""
    shapes = []
    for poly in getattr(g, "geoms", [g]):
        if poly.geom_type != "Polygon" or poly.area < 1.0:
            continue
        shapes.append(ring_to_shape(poly.exterior.coords, tol, bounds))
        for ring in poly.interiors:
            if Polygon(ring).area >= 1.0:
                shapes.append(ring_to_shape(ring.coords, tol, bounds))
    return shapes


# ----------------------------------------------------------------------------- model built from the spec
def sketch_region(f):
    return sp.sketch_geometry(f["sketch"])


def make_grid(ext, step):
    return [np.arange(step / 2, e, step) for e in ext]


def hole_removed_mask(f, X, Y, Z):
    """Mask of the material removed by a ``hole`` feature (cylinder, countersink or counterbore)."""
    n, A, B = sp.PLANE_AXES[f["plane"]]
    co = [X, Y, Z]
    t = (co[n] - f["offset"]) * f["direction"]
    r = f["d"] / 2.0
    removed = np.zeros(np.broadcast(X, Y, Z).shape, bool)
    for px, py in f["points"]:
        rr = np.hypot(co[A] - px, co[B] - py)
        if f["subtype"] == "countersink":
            tan_half = math.tan(math.radians(f["angle"] / 2.0))
            head_h = (f["head_d"] / 2.0 - r) / tan_half
            limit = np.where(t < head_h, r + (head_h - t) * tan_half, r)
        elif f["subtype"] == "counterbore":
            limit = np.where(t < f["head_depth"], f["head_d"] / 2.0, r)
        else:
            limit = r
        removed |= (t >= 0) & (t <= f["depth"]) & (rr <= limit)
    return removed


def occupancy(spec, ext, step):
    """3D boolean occupancy (X, Y, Z) of the model described by the spec."""
    spec = sp.expand_mirrors(spec)
    ax = make_grid(ext, step)
    occ = np.zeros([len(a) for a in ax], bool)
    for f in spec["features"]:
        if f["type"] == "hole":
            X, Y, Z = np.meshgrid(ax[0], ax[1], ax[2], indexing="ij")
            occ &= ~hole_removed_mask(f, X, Y, Z)
            continue
        n, A, B = sp.PLANE_AXES[f["plane"]]
        g = sketch_region(f)
        ga, gb = np.meshgrid(ax[A], ax[B], indexing="ij")
        m2 = shapely.contains_xy(g, ga, gb) | shapely.intersects_xy(g, ga, gb)
        lo = f.get("offset", 0)
        dist = ext[n] if f["distance"] == sp.THROUGH_ALL else f["distance"]
        z = ax[n]
        mz = (z >= lo) & (z <= lo + dist) if f.get("direction", 1) > 0 else (z <= lo) & (z >= lo - dist)
        # build the 3D mask by broadcasting (axes A, B, n)
        m3 = np.expand_dims(m2.reshape([len(ax[A]), len(ax[B])]), 2)
        m3 = np.broadcast_to(m3, (len(ax[A]), len(ax[B]), len(ax[n])))
        m3 = np.moveaxis(m3, [0, 1, 2], [A, B, n])
        mz3 = np.moveaxis(np.broadcast_to(mz, (len(ax[A]), len(ax[B]), len(mz))), [0, 1, 2], [A, B, n])
        reg = m3 & mz3
        if f["operation"] == "cut":
            occ &= ~reg
        else:
            occ |= reg
    return occ


def contains(spec, ext, pts):
    """Exact (shapely) membership of 3D points (N, 3) in the spec's model."""
    spec = sp.expand_mirrors(spec)
    res = np.zeros(len(pts), bool)
    for f in spec["features"]:
        if f["type"] == "hole":
            res &= ~hole_removed_mask(f, pts[:, 0], pts[:, 1], pts[:, 2])
            continue
        n, A, B = sp.PLANE_AXES[f["plane"]]
        g = sketch_region(f)
        d2 = shapely.contains_xy(g, pts[:, A], pts[:, B]) | shapely.intersects_xy(g, pts[:, A], pts[:, B])
        lo = f.get("offset", 0)
        dist = ext[n] if f["distance"] == sp.THROUGH_ALL else f["distance"]
        z = pts[:, n]
        dz = (z >= lo) & (z <= lo + dist) if f.get("direction", 1) > 0 else (z <= lo) & (z >= lo - dist)
        reg = d2 & dz
        if f["operation"] == "cut":
            res &= ~reg
        else:
            res |= reg
    return res


def iou(m, spec, ext, n=30000, seed=1):
    """Intersection over union between the mesh and the spec's model, on random points."""
    rng = np.random.default_rng(seed)
    pts = rng.uniform([0, 0, 0], ext, size=(n, 3))
    inside = np.zeros(n, bool)
    for i in range(0, n, 2500):
        inside[i:i + 2500] = m.contains(pts[i:i + 2500])
    model = contains(spec, ext, pts)
    u = (inside | model).sum()
    return float((inside & model).sum() / u) if u else 1.0


# ----------------------------------------------------------------------------- conversion
def _clean(g):
    if g is None or g.is_empty:
        return Polygon()
    return g.buffer(0).simplify(0.01)


def _delta(a, b):
    return a.symmetric_difference(b).area


def _threshold(ref):
    return max(1.0, 0.004 * max(ref.area, 1.0))


def _circular_rings(reg):
    out = []
    for poly in getattr(reg, "geoms", [reg]):
        if poly.geom_type != "Polygon":
            continue
        for ring in poly.interiors:
            P = np.asarray(ring.coords)[:-1]
            if len(P) < 8:
                continue
            cx, cy, r = _fit_circle(P)
            if 0 < r < 200 and _circle_deviation(P, cx, cy, r, True) <= 0.12:
                out.append((cx, cy, 2 * r))
    return out


def _without_rings(reg, centers):
    """Remove the holes (inner rings) centered on ``centers``."""
    if not centers:
        return reg
    pols = []
    for poly in getattr(reg, "geoms", [reg]):
        if poly.geom_type != "Polygon":
            continue
        rings = [it for it in poly.interiors
                 if min(math.hypot(Polygon(it).centroid.x - c[0], Polygon(it).centroid.y - c[1])
                        for c in centers) > 0.5]
        pols.append(Polygon(poly.exterior, rings))
    return shapely.union_all(pols) if pols else reg


COUNTERSINK_ANGLES = (60, 82, 90, 100, 110, 118, 120)


def detect_holes(pos, regs, step, vertex_levels):
    """Look for countersunk/counterbored holes: a circular ring whose diameter changes along the axis."""
    tracks = []
    for i, reg in enumerate(regs):
        for (cx, cy, d) in _circular_rings(reg):
            for t in tracks:
                if t["idx"][-1] == i - 1 and math.hypot(t["c"][0] - cx, t["c"][1] - cy) < 0.4:
                    t["idx"].append(i)
                    t["d"].append(d)
                    t["c"] = (cx, cy)
                    break
            else:
                tracks.append({"idx": [i], "d": [d], "c": (cx, cy)})
    holes = []
    for t in tracks:
        if len(t["idx"]) < 4 or max(t["d"]) - min(t["d"]) < 0.8:
            continue
        z = np.array([pos[i] for i in t["idx"]])
        d = np.array(t["d"])
        direction = 1
        if d[:2].mean() < d[-2:].mean():  # the wide end is the "face": orient from the face to the bottom
            z, d, direction = z[::-1], d[::-1], -1
        d_small = float(np.median(d[-max(2, len(d) // 4):]))
        k = next((q for q, v in enumerate(d) if abs(v - d_small) < 0.12), len(d) - 1)
        if k < 1:
            continue

        def pin(x):
            c = vertex_levels[np.argmin(np.abs(vertex_levels - x))]
            return float(c) if abs(c - x) <= 1.5 * step else x

        face = pin(z[0] - direction * step / 2.0)
        bottom = pin(z[-1] + direction * step / 2.0)
        depth = abs(bottom - face)
        tt = np.abs(z[:k] - face)
        base = dict(x=round(t["c"][0], 3), y=round(t["c"][1], 3), d=round(d_small, 2), face=round(face, 3),
                    direction=direction, depth=round(depth, 3), idx=(t["idx"][0], t["idx"][-1]))
        if k >= 3 and np.ptp(d[:k]) > 0.8:  # cone: linear fit d(t)
            b, a = np.polyfit(tt, d[:k], 1)
            res = d[:k] - (a + b * tt)
            if b < 0 and np.abs(res).max() < 0.25:
                ang = 2 * math.degrees(math.atan(abs(b) / 2.0))
                if min(abs(q - ang) for q in COUNTERSINK_ANGLES) < 4:
                    ang = min(COUNTERSINK_ANGLES, key=lambda q: abs(q - ang))
                holes.append(dict(base, subtype="countersink", head_d=round(float(a), 2),
                                  angle=round(float(ang), 1)))
        elif k <= 2:  # step: counterbore
            zc = pin(z[k] - direction * step / 2.0)
            holes.append(dict(base, subtype="counterbore", head_d=round(float(np.median(d[:k])), 2),
                              head_depth=round(abs(zc - face), 3)))
    return holes


def slice_and_group(m, ext, n, K=None, max_groups=6, merge_interruptions=False):
    """Slice the part along axis ``n`` and group consecutive slices with the same section.
    Return (runs, holes): runs = list of (z0, z1, region) with boundaries pinned to mesh vertices;
    countersunk/counterbored holes are removed from the region (they become hole features)."""
    length = ext[n]
    K = K or int(min(200, max(60, length / 0.15)))
    step = length / K
    pos = [step * (i + 0.5) for i in range(K)]
    raw = [_clean(section(m, n, z)) for z in pos]
    vertex_levels = np.unique(np.round(m.vertices[:, n], 2))
    holes = detect_holes(pos, raw, step, vertex_levels)

    def centers_at(i):
        return [(f["x"], f["y"]) for f in holes if f["idx"][0] <= i <= f["idx"][1]]

    regs = [_clean(_without_rings(r, centers_at(i))) for i, r in enumerate(raw)]

    def clean_section(z):
        i = min(max(int(z / step), 0), K - 1)
        return _clean(_without_rings(section(m, n, z), centers_at(i)))

    groups = [[0]]
    for i in range(1, K):
        ref = regs[groups[-1][0]]
        if _delta(ref, regs[i]) <= _threshold(ref):
            groups[-1].append(i)
        else:
            groups.append([i])

    def dist(g1, g2):
        return _delta(regs[g1[len(g1) // 2]], regs[g2[len(g2) // 2]])

    if merge_interruptions:  # slot/interruption: the middle group sits inside its neighbors, which are equal
        changed = True
        while changed:
            changed = False
            for t in range(1, len(groups) - 1):
                a, b, c = (regs[groups[q][len(groups[q]) // 2]] for q in (t - 1, t, t + 1))
                if (_delta(a, c) <= _threshold(a) and b.area < a.area and
                        _delta(a.intersection(b), b) <= _threshold(b)):
                    groups[t - 1:t + 2] = [groups[t - 1] + groups[t] + groups[t + 1]]
                    changed = True
                    break
    while len(groups) > max_groups:  # merge the most similar neighboring pair
        k = min(range(len(groups) - 1), key=lambda t: dist(groups[t], groups[t + 1]))
        groups[k:k + 2] = [groups[k] + groups[k + 1]]
    rep = [regs[g[len(g) // 2]] for g in groups]
    edges = [0.0]
    for t in range(len(groups) - 1):
        lo, hi = pos[groups[t][-1]], pos[groups[t + 1][0]]
        for _ in range(7):  # bisection
            mid = (lo + hi) / 2
            r = clean_section(mid)
            if _delta(r, rep[t]) <= max(_threshold(rep[t]), 0.5 * _delta(rep[t], rep[t + 1])):
                lo = mid
            else:
                hi = mid
        b = (lo + hi) / 2
        cand = vertex_levels[np.argmin(np.abs(vertex_levels - b))]
        edges.append(float(cand) if abs(cand - b) <= 2 * step else round(b, 1))
    edges.append(length)
    runs = [(edges[t], edges[t + 1], rep[t]) for t in range(len(groups)) if edges[t + 1] - edges[t] > 0.05]
    return runs, holes


def hole_features(holes, plane):
    """Hole dicts (from ``detect_holes`` / ``find_blind_holes``) -> hole features; identical holes share one."""
    groups = {}
    for f in holes:  # identical holes become a single feature with several centers
        key = tuple((k, f[k]) for k in sorted(f) if k not in ("x", "y", "idx"))
        groups.setdefault(key, []).append(f)
    feats = []
    for key, fs in groups.items():
        f0 = fs[0]
        name = {"countersink": "Countersunk hole", "counterbore": "Counterbored hole",
                "simple": "Blind hole"}[f0["subtype"]]
        feat = {"type": "hole", "name": name, "subtype": f0["subtype"], "plane": plane, "offset": f0["face"],
                "direction": f0["direction"], "points": [[f["x"], f["y"]] for f in fs], "d": f0["d"],
                "depth": f0["depth"]}
        for k in ("head_d", "angle", "head_depth"):
            if k in f0:
                feat[k] = f0[k]
        ring = patterns.circular_pattern(feat["points"])
        if ring:  # a ring of identical holes: the spec says so, the builder places one hole and patterns it
            feat["pattern"] = ring
        feats.append(feat)
    return feats


def find_blind_holes(m, ext, a, silhouette_geom=None):
    """Blind round holes whose axis is parallel to axis ``a`` (not visible in the silhouette along ``a``).

    A circular inner ring that keeps the same diameter over several slices is a hole. One end must be open
    (the center is empty just beyond the track) and the other closed (material beyond it): that is a blind hole
    with a flat bottom. The face is the outermost mesh coordinate around the hole."""
    from shapely.geometry import Point
    length = ext[a]
    K = int(min(160, max(50, length / 0.25)))
    step = length / K
    pos = [step * (i + 0.5) for i in range(K)]
    regs = [_clean(section(m, a, z)) for z in pos]
    vertex_levels = np.unique(np.round(m.vertices[:, a], 2))
    _, A, B = sp.PLANE_AXES[PLANE_NAME[a]]
    if silhouette_geom is None:
        silhouette_geom = silhouette(m, a)

    def face_level(center, r, high):
        v = m.vertices
        near = np.hypot(v[:, A] - center[0], v[:, B] - center[1]) < r + 1.5
        if not near.any():
            return None
        return float(v[near, a].max() if high else v[near, a].min())

    return _blind_holes_from_slices(pos, regs, step, silhouette_geom, vertex_levels, face_level)


def _blind_holes_from_slices(pos, regs, step, silhouette_geom, vertex_levels, face_level):
    from shapely.geometry import Point
    tracks = []
    for i, reg in enumerate(regs):
        for (cx, cy, d) in _circular_rings(reg):
            for t in tracks:
                if t["idx"][-1] == i - 1 and math.hypot(t["c"][0] - cx, t["c"][1] - cy) < 0.4:
                    t["idx"].append(i)
                    t["d"].append(d)
                    t["c"] = (cx, cy)
                    break
            else:
                tracks.append({"idx": [i], "d": [d], "c": (cx, cy)})

    def pin(x):
        c = vertex_levels[np.argmin(np.abs(vertex_levels - x))]
        return float(c) if abs(c - x) <= 1.5 * step else round(x, 2)

    holes = []
    for t in tracks:
        d = np.array(t["d"])
        if len(t["idx"]) < 3 or np.ptp(d) > 0.3 or np.median(d) < 0.8:
            continue
        center = Point(t["c"])
        if not center.within(silhouette_geom):  # a through hole: handled by the silhouette cut
            continue
        i0, i1 = t["idx"][0], t["idx"][-1]
        low_open = i0 == 0 or not regs[i0 - 1].contains(center)
        high_open = i1 == len(regs) - 1 or not regs[i1 + 1].contains(center)
        if low_open == high_open:  # through, or an internal cavity
            continue
        r = float(np.median(d)) / 2.0
        face = face_level(t["c"], r, high_open)
        if face is None:
            continue
        if high_open:
            bottom = pin((pos[i0] + pos[i0 - 1]) / 2.0 if i0 > 0 else pos[i0] - step / 2.0)
            face = max(pin(face), pos[i1])
            direction = -1
        else:
            bottom = pin((pos[i1] + pos[i1 + 1]) / 2.0 if i1 + 1 < len(pos) else pos[i1] + step / 2.0)
            face = min(pin(face), pos[i0])
            direction = 1
        holes.append(dict(x=round(t["c"][0], 3), y=round(t["c"][1], 3), d=round(float(np.median(d)), 2),
                          face=round(face, 3), direction=direction, depth=round(abs(face - bottom), 3),
                          subtype="simple", idx=(i0, i1)))
    return holes


def plan(m, ext, n, tol, merge_interruptions=False):
    """Body features (base + runs) and hole features for extrusion axis ``n``."""
    runs, holes = slice_and_group(m, ext, n, merge_interruptions=merge_interruptions)
    plane = PLANE_NAME[n]
    _, A, B = sp.PLANE_AXES[plane]
    bounds = {0: [0.0, ext[A]], 1: [0.0, ext[B]]}
    # base = the run whose section needs the least editing for the others (length-weighted)
    cost = [sum((z1 - z0) * _delta(r, r2) for (z0, z1, r2) in runs) for (_, _, r) in runs]
    b = int(np.argmin(cost))
    base_region = runs[b][2]
    feats = [{"type": "extrusion", "name": "Base profile", "plane": plane, "offset": 0.0, "direction": 1,
              "operation": "new", "distance": ext[n],
              "sketch": compact(region_shapes(base_region, tol, bounds))}]
    for t, (z0, z1, R) in enumerate(runs):
        if t == b:
            continue
        cut = base_region.difference(R).buffer(-0.15).buffer(0.15)
        extra = R.difference(base_region).buffer(-0.15).buffer(0.15)
        # the cut goes first; the join uses the whole run section (a simpler sketch than "R minus base")
        for oper, reg, label, wanted in (("cut", cut, "Recess", cut.area >= 1.0),
                                         ("join", R, "Pad", extra.area >= 1.0)):
            if not wanted:
                continue
            feats.append({"type": "extrusion", "name": "%s %.1f to %.1f" % (label, z0, z1), "plane": plane,
                          "offset": round(z0, 2), "direction": 1, "operation": oper,
                          "distance": round(z1 - z0, 2),
                          "sketch": compact(region_shapes(reg.simplify(0.02), tol, bounds))})
    feats += hole_features(holes, plane)
    return feats, len(runs)


def sample_points(m, ext, n_pts=6000, seed=0):
    rng = np.random.default_rng(seed)
    pts = rng.uniform([0, 0, 0], ext, size=(n_pts, 3))
    inside = np.zeros(n_pts, bool)
    for i in range(0, n_pts, 2500):
        inside[i:i + 2500] = m.contains(pts[i:i + 2500])
    return pts, inside


def _iou_points(spec, ext, pts, inside):
    model = contains(spec, ext, pts)
    u = (inside | model).sum()
    return float((inside & model).sum() / u) if u else 1.0


def _prismatic(m):
    """How much the mesh looks like a prism (best axis): IoU between the sections at 30/50/70%."""
    ext = [float(e) for e in m.extents]
    best = 0.0
    for n in (0, 1, 2):
        rs = [section(m, n, ext[n] * f) for f in (0.3, 0.5, 0.7)]
        if any(r is None or r.is_empty for r in rs):
            continue
        sc = min(r.intersection(rs[1]).area / max(r.union(rs[1]).area, 1e-9) for r in (rs[0], rs[2]))
        best = max(best, sc)
    return best


def rotate(m, axis, degrees):
    import trimesh
    v = np.zeros(3)
    v[axis] = 1
    g = m.copy()
    g.apply_transform(trimesh.transformations.rotation_matrix(math.radians(degrees), v, g.centroid))
    g.apply_translation(-g.bounds[0])
    return g


def best_rotation(m):
    base = _prismatic(m)
    best = (base, None)
    for axis in (0, 1, 2):
        for degrees in range(-45, 50, 5):
            if degrees == 0:
                continue
            sc = _prismatic(rotate(m, axis, degrees))
            if sc > best[0] + 0.02:
                best = (sc, (axis, degrees))
    return best[1]


def convert(stl_path, tol=0.12):
    """Convert an STL file. Return ``(mesh, extents, spec)`` (the mesh may be rotated to align it)."""
    m = load_mesh(stl_path)
    part = os.path.splitext(os.path.basename(stl_path))[0]
    r = _convert_mesh(m, tol, part)
    if r[3] < 0.97:
        rot = best_rotation(m)
        if rot:
            m2 = rotate(m, *rot)
            r2 = _convert_mesh(m2, tol, part)
            if r2[3] - 0.01 * len(r2[2]["features"]) > r[3] - 0.01 * len(r[2]["features"]):
                r2[2]["notes"]["rotation_applied_before_slicing"] = {"axis": AXIS_NAME[rot[0]],
                                                                    "degrees": rot[1]}
                r = r2
    return r[:3]


def _silhouette_cuts(m, ext, feats, n, tol):
    """Through cuts on the other two axes: what lies outside the mesh silhouette (computed on the base body)."""
    step = max(ext) / 160.0
    for a in [x for x in (2, 1, 0) if x != n]:
        pl = PLANE_NAME[a]
        _, A2, B2 = sp.PLANE_AXES[pl]
        S = silhouette(m, a)
        rect = box(0, 0, ext[A2], ext[B2])
        rest_axes = [x for x in (0, 1, 2) if x != a]
        # the silhouette S lives in (rest_axes[0], rest_axes[1]); the plane uses (A2, B2)
        if (A2, B2) != tuple(rest_axes):
            S = shapely.affinity.affine_transform(S, [0, 1, 1, 0, 0, 0])
        outside = rect.difference(S)
        gr = make_grid(ext, step)
        occ = occupancy({"features": feats[:1]}, ext, step).any(axis=a)  # base body only
        ga, gb = np.meshgrid(gr[A2], gr[B2], indexing="ij")
        if (A2, B2) != tuple(rest_axes):
            occ = occ.T
        kept = []
        for poly in getattr(outside, "geoms", [outside]):
            if poly.geom_type != "Polygon" or poly.area < 1.0:
                continue
            here = shapely.contains_xy(poly, ga, gb) & occ
            if here.sum() * step * step >= max(1.0, 0.0005 * rect.area):
                kept.append(poly)
        if not kept:
            continue
        shapes = []
        for p in kept:
            shapes += region_shapes(p, tol, {0: [0.0, ext[A2]], 1: [0.0, ext[B2]]})
        feats.append({"type": "extrusion", "name": "%s axis %s" % (THROUGH_CUT_PREFIX, AXIS_NAME[a]),
                      "plane": pl, "offset": 0.0, "direction": 1, "operation": "cut",
                      "distance": sp.THROUGH_ALL, "sketch": compact(shapes)})


def _prune(spec, ext, pts2, inside2):
    """Remove features that barely change the IoU. Steps first (the silhouette/hole is usually simpler),
    then through cuts. Screw holes and the base are never pruned."""
    feats = spec["features"]
    pruned = []
    current = _iou_points(spec, ext, pts2, inside2)
    initial = current
    order = ([f for f in feats[1:] if f["type"] == "extrusion" and not f["name"].startswith(THROUGH_CUT_PREFIX)] +
             [f for f in feats[1:] if f["name"].startswith(THROUGH_CUT_PREFIX)])
    for f in order:
        k = feats.index(f)
        feats.pop(k)
        new = _iou_points(spec, ext, pts2, inside2)
        is_step = not f["name"].startswith(THROUGH_CUT_PREFIX)
        # steps: 0.4% slack each and 0.6% in total; through cuts: 0.2%
        if (current - new < (0.004 if is_step else 0.002)) and (not is_step or initial - new < 0.006):
            pruned.append(f["name"])
            current = max(current, new)
        else:
            feats.insert(k, f)
    return current, pruned


def _apply_gear(m, spec, ext, pts, inside):
    """Replace the base profile by a ``gear`` feature when it is a standard involute spur gear. The inner shapes
    of the base sketch (the bore) become a through cut. Kept only if the IoU does not drop by more than 0.01."""
    base = spec["features"][0]
    if base["type"] != "extrusion" or base.get("operation") != "new":
        return None
    shapes = base["sketch"]
    if not any(s_["shape"] == "contour" and len(s_["segments"]) >= 12 for s_ in shapes):
        return None  # a gear outline has many segments; skip the (cheap) test otherwise
    # fit on the real mesh section (the fitted contour is already an approximation of it)
    n = sp.PLANE_AXES[base["plane"]][0]
    level = base.get("offset", 0.0) + base["distance"] * 0.5 * base.get("direction", 1)
    region = section(m, n, level)
    if region is None:
        return None
    polys = list(getattr(region, "geoms", [region]))
    outline = max(polys, key=lambda g: g.area)
    # the contour shape that is the outline; everything else in the sketch is a hole (bore)
    geoms = [sp.shape_to_geometry(s_) for s_ in shapes]
    outer = max(range(len(shapes)), key=lambda i: geoms[i].area)
    inner = [s_ for i, s_ in enumerate(shapes) if i != outer]
    fit = gears.fit_full_gear(outline)
    extra = []
    if not fit:
        fit = gears.fit_sector_gear(outline)
        extra = _sector_features(fit, geoms[outer], base) if fit else None
        if not extra:
            return None
    gear = {"type": "gear", "name": "Spur gear", "plane": base["plane"], "offset": base.get("offset", 0.0),
            "direction": base.get("direction", 1), "distance": base["distance"], "module": fit["module"],
            "teeth": fit["teeth"], "pressure_angle": fit["pressure_angle"], "addendum": fit["addendum"],
            "dedendum": fit["dedendum"], "phase": fit["phase"], "center": fit["center"]}
    new = [gear] + extra
    if inner:
        new.append({"type": "extrusion", "name": "Bore", "plane": base["plane"], "offset": base.get("offset", 0.0),
                    "direction": base.get("direction", 1), "operation": "cut", "distance": sp.THROUGH_ALL,
                    "sketch": inner})
    before = _iou_points(spec, ext, pts, inside)
    trial = {"system": "model", "features": new + spec["features"][1:]}
    rounding.round_spec(trial)
    # a sector is a standard tooth form on top of a mesh with rounded roots: it may lose a little more
    if _iou_points(trial, ext, pts, inside) < before - (0.03 if extra else 0.01):
        return None
    spec["features"] = trial["features"]
    return {"module": gear["module"], "teeth": gear["teeth"], "pressure_angle": gear["pressure_angle"],
            "fit_iou": fit.get("iou", fit.get("agreement")), "sector": bool(extra)}


def _sector_features(fit, silhouette, base):
    """Join and cut features that turn a full gear into the part: a plate for what is not gear, and cuts that
    remove the gear outside the silhouette. None when the plate would not be a simple rectangle."""
    gear = dict(fit, center=tuple(fit["center"]))
    bx0, by0, bx1, by1 = silhouette.bounds
    _, plate, exact = gears.compose_sector(silhouette, gear, silhouette.bounds)
    if exact:
        return None
    common = {"plane": base["plane"], "offset": base.get("offset", 0.0), "direction": base.get("direction", 1)}
    feats = []
    if plate:
        p = plate[0]
        px0, py0, px1, py1 = p.bounds
        feats.append(dict(common, type="extrusion", name="Base plate", operation="join", distance=base["distance"],
                          sketch=[{"shape": "rectangle", "x": px0, "y": py0, "width": px1 - px0,
                                   "height": py1 - py0}]))
    cx, cy = fit["center"]
    tip = fit["module"] * (fit["teeth"] + 2 * fit["addendum"]) / 2.0 + 1.0
    gx0, gy0, gx1, gy1 = cx - tip, cy - tip, cx + tip, cy + tip
    strips = [(gx0, gy0, bx0, gy1), (bx1, gy0, gx1, gy1), (bx0, gy0, bx1, by0), (bx0, by1, bx1, gy1)]
    cuts = [{"shape": "rectangle", "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
            for x0, y0, x1, y1 in strips if x1 - x0 > 0.05 and y1 - y0 > 0.05]
    if cuts:
        feats.append(dict(common, type="extrusion", name="Trim gear", operation="cut", distance=sp.THROUGH_ALL,
                          sketch=cuts))
    return feats


def _apply_mirror(spec, ext, pts, inside):
    """Replace mirrored duplicates by a mirror feature when that keeps the IoU. Return a note for the spec."""
    base_iou = _iou_points(spec, ext, pts, inside)
    best = None
    for axis in (0, 1, 2):
        c = rounding.nice(ext[axis] / 2.0)
        feats, saved = symmetry.mirror_features(spec["features"], axis, c, ext)
        if saved <= 0:
            continue
        trial = {"system": "model", "features": feats}
        value = _iou_points(trial, ext, pts, inside)
        if value >= base_iou - 0.001 and (best is None or saved > best[0]):
            best = (saved, feats, axis, c)
    if best is None:
        return None
    spec["features"] = best[1]
    return {"plane": next(p for p, v in sp.PLANE_AXES.items() if v[0] == best[2]), "offset": best[3],
            "removed_duplicates": best[0]}


def _convert_mesh(m, tol, part):
    ext = [round(float(e), 3) for e in m.extents]
    pts, inside = sample_points(m, ext)
    pts2, inside2 = sample_points(m, ext, 12000, seed=7)
    best = None
    for n, bonus in ((2, 0.006), (1, 0.003), (0, 0.0)):
        feats, _ = plan(m, ext, n, tol)
        sc = _iou_points({"system": "model", "features": feats}, ext, pts, inside)
        score = sc - 0.012 * len(feats) + bonus
        if best is None or score > best[0]:
            best = (score, n)
    n = best[1]
    # blind holes across the extrusion axis are independent of the run plan: detect them once
    blind_features = []
    for a in [x for x in (2, 1, 0) if x != n]:
        blind_features += hole_features(find_blind_holes(m, ext, a), PLANE_NAME[a])
    results = []
    for merge in (False, True):
        feats, n_runs = plan(m, ext, n, tol, merge)
        if merge and n_runs == results[0][3]:  # nothing to merge: same result
            continue
        _silhouette_cuts(m, ext, feats, n, tol)
        feats += [dict(f) for f in blind_features]
        spec = {"system": "model", "features": feats}
        value, pruned = _prune(spec, ext, pts2, inside2)
        results.append((value - 0.006 * len(feats), spec, value, n_runs, pruned, merge))
    _, spec, value, _, pruned, merge = max(results, key=lambda r: r[0])
    gear_note = _apply_gear(m, spec, ext, pts2, inside2)
    raw_diameters = fits.raw_diameters(spec)
    rounding.round_spec(spec)
    symmetry.unique_names(spec["features"])
    mirror_note = _apply_mirror(spec, ext, pts2, inside2)
    spec["notes"] = {"part": part, "estimated_iou": round(value, 4),
                     "origin": "minimum corner of the mesh bounding box at the origin",
                     "original_min": [round(float(v), 3) for v in m.metadata.get("original_min", [0, 0, 0])],
                     "extrusion_axis": AXIS_NAME[n], "tolerance_mm": tol, "pruned_features": pruned,
                     "merged_interruptions": merge, "raw_diameters": raw_diameters, "mirror": mirror_note,
                     "gear": gear_note}
    return m, ext, spec, value


def convert_file(stl_path, out_dir, tol=0.12, n_points=30000):
    """Convert one STL and write ``<part>.json`` to ``out_dir``. Return ``(json_path, spec)``."""
    m, ext, spec = convert(stl_path, tol)
    spec["notes"]["iou"] = round(iou(m, spec, ext, n_points), 4)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, spec["notes"]["part"] + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    return out, spec
