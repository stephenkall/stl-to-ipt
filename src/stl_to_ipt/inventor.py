"""Create a parametric part in Autodesk Inventor from a spec (Windows + Inventor + pywin32).

Two spec flavours are supported:
  - ``"system": "model"``: global X/Y/Z coordinates. Every feature has a plane (XY/XZ/YZ), an offset along
    the normal, a direction (+1/-1) and a sketch in plane coordinates (XY -> x,y  XZ -> x,z  YZ -> y,z).
    The real orientation of each plane is read from Inventor (``SketchToModelSpace``), so nothing depends
    on the axis conventions of the template.
  - legacy format (no ``system`` key): sketch coordinates are used as they are.

The Inventor COM object must be created with ``win32com.client.dynamic.Dispatch``. Do not use
``gencache.EnsureDispatch``: the generated wrappers make ``Documents.Add`` return generic documents
without ``ComponentDefinition``.
"""
import math
import os
import re
import unicodedata

from . import gears
from . import spec as sp

MM = 0.1  # mm -> cm (internal unit of the Inventor API)

# Inventor API constants
K_PART_DOCUMENT = 12290
K_POSITIVE = 20993
K_NEGATIVE = 20994
K_SYMMETRIC = 20995
K_JOIN = 20481
K_CUT = 20482
K_NEW_BODY = 20485
K_MILLIMETER_UNITS = 11269
K_COMPUTE_IDENTICAL = 47361  # PatternComputeTypeEnum.kIdenticalCompute (read from the Inventor type library)
OPS = {"new": K_NEW_BODY, "join": K_JOIN, "cut": K_CUT}
EXTENT_DIRECTIONS = {"positive": K_POSITIVE, "negative": K_NEGATIVE, "symmetric": K_SYMMETRIC}
PLANE_INDEX = {"YZ": 1, "XZ": 2, "XY": 3}
MAX_TANGENT_SEGMENTS = 40  # denser contours (gear teeth) keep plain vertex dimensions: tangents would be slow
K_HORIZONTAL_DIM = 19201  # DimensionOrientationEnum.kHorizontalDim
K_VERTICAL_DIM = 19202


# ----------------------------------------------------------------------------- plane mapping
def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sketch_basis(sk, tg):
    """Origin and unit vectors u, v of the sketch, in model coordinates (cm)."""
    o = sk.SketchToModelSpace(tg.CreatePoint2d(0, 0))
    pu = sk.SketchToModelSpace(tg.CreatePoint2d(1, 0))
    pv = sk.SketchToModelSpace(tg.CreatePoint2d(0, 1))
    return ((o.X, o.Y, o.Z),
            (pu.X - o.X, pu.Y - o.Y, pu.Z - o.Z),
            (pv.X - o.X, pv.Y - o.Y, pv.Z - o.Z))


def make_mapper(tg, basis, axis_a, axis_b):
    """Return P(a_mm, b_mm) -> sketch Point2d for model coordinates on axes A and B."""
    o, eu, ev = basis
    m00, m01, m10, m11 = eu[axis_a], ev[axis_a], eu[axis_b], ev[axis_b]
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-9:
        raise RuntimeError("The sketch plane is not aligned with the expected axes.")

    def P(a, b):
        da, db = a * MM - o[axis_a], b * MM - o[axis_b]
        s = (m11 * da - m01 * db) / det
        t = (-m10 * da + m00 * db) / det
        return tg.CreatePoint2d(s, t)

    return P


# ----------------------------------------------------------------------------- parameters / dimensions
def _slug(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "P"


class Context:
    """Parametrization state of a document: unique parameter names and already-named diameters."""

    def __init__(self, comp):
        self.comp = comp
        self.used = set()
        self.diameters = {}
        self.radii = {}
        try:
            for p in comp.Parameters:
                self.used.add(p.Name)
        except Exception:
            pass

    def update(self):
        """Recompute the document (sketch geometry follows parameter changes only after this)."""
        self.comp.Document.Update()

    def unique(self, base):
        base = _slug(base)[:40]
        name, k = base, 2
        while name in self.used:
            name, k = "%s_%d" % (base, k), k + 1
        self.used.add(name)
        return name

    def name_parameter(self, obj, base):
        """Name the parameter of a dimension/feature. Return the name, or None if it failed."""
        try:
            par = obj.Parameter if hasattr(obj, "Parameter") else obj
            name = self.unique(base)
            par.Name = name
            return name
        except Exception:
            return None


def _text_point(tg, a, b, horizontal):
    ga, gb = a.Geometry, b.Geometry
    mx, my = (ga.X + gb.X) / 2.0, (ga.Y + gb.Y) / 2.0
    return tg.CreatePoint2d(mx, my + 0.6) if horizontal else tg.CreatePoint2d(mx + 0.6, my)


def _tidy(dim):
    """Remove numeric dust from a driving dimension: 5.0009 mm becomes 5 mm (never more than 0.006 mm of change)."""
    try:
        par = dim.Parameter
        mm = par.Value / MM
        clean = round(mm, 2)
        if 1e-9 < abs(clean - mm) < 0.006:
            par.Value = clean * MM
    except Exception:
        pass


PROBE_STEP = 0.002  # cm (0.02 mm): size of the perturbation used to detect redundant dimensions


def _is_independent(dim, measure, update):
    """True when the dimension really drives the geometry. A redundant dimension is silently accepted by Inventor
    when tangent constraints are involved and later breaks the sketch, so it is tested by perturbation: the
    measured quantity must follow a small change of the value, and return when the value is restored."""
    par = dim.Parameter
    value = par.Value
    try:
        par.Value = value + PROBE_STEP
        update()  # the sketch geometry is only recomputed on a document update
        follows = abs(measure() - (value + PROBE_STEP)) < 1e-4
        par.Value = value
        update()
        return follows and abs(measure() - value) < 1e-4
    except Exception:
        try:
            par.Value = value
            update()
        except Exception:
            pass
        return False


def _dimension(sk, tg, a, b, horizontal, ctx, name, expr=None, check=False):
    d = sk.DimensionConstraints.AddTwoPointDistance(
        a, b, K_HORIZONTAL_DIM if horizontal else K_VERTICAL_DIM, _text_point(tg, a, b, horizontal))
    if check:
        def measure():
            ga, gb = a.Geometry, b.Geometry
            return abs(gb.X - ga.X) if horizontal else abs(gb.Y - ga.Y)
        if not _is_independent(d, measure, ctx.update):
            d.Delete()
            raise RuntimeError("redundant dimension")
    if expr:
        d.Parameter.Expression = expr
        return expr
    _tidy(d)
    return ctx.name_parameter(d, name)


def _radius_dimension(sk, tg, arc, ctx, name, check=False):
    """Radius dimension; arcs with the same radius share one parameter."""
    g = arc.CenterSketchPoint.Geometry
    dim = sk.DimensionConstraints.AddRadius(arc, tg.CreatePoint2d(g.X + 0.5, g.Y + 0.5))
    if check and not _is_independent(dim, lambda: arc.Radius, ctx.update):
        dim.Delete()
        raise RuntimeError("redundant radius")
    _tidy(dim)
    r_mm = round(dim.Parameter.Value / MM, 2)
    if r_mm in ctx.radii:
        dim.Parameter.Expression = ctx.radii[r_mm]
        return ctx.radii[r_mm]
    nm = ctx.name_parameter(dim, name)
    if nm:
        ctx.radii[r_mm] = nm
    return nm


def _fix_position(sk, tg, origin, pt, ctx, prefix, tag):
    """Fix a point relative to the sketch origin: horizontal/vertical dimension, or alignment if on an axis."""
    g = pt.Geometry
    if abs(g.X) < 1e-7:
        sk.GeometricConstraints.AddVerticalAlign(origin, pt)
    else:
        _dimension(sk, tg, origin, pt, True, ctx, "%s_x%s" % (prefix, tag))
    if abs(g.Y) < 1e-7:
        sk.GeometricConstraints.AddHorizontalAlign(origin, pt)
    else:
        _dimension(sk, tg, origin, pt, False, ctx, "%s_y%s" % (prefix, tag))


def _vertex_key(g):
    return (round(g.X, 5), round(g.Y, 5))


def parametrize_circle(sk, tg, origin, ctx, c, prefix, tag, pattern, k):
    cp = c.CenterSketchPoint
    if pattern is not None and k > 0:  # pattern copy: relate it to the previous one
        prev, dx = pattern["last"], pattern["dx"]
        if abs(dx) > 1e-9:
            pattern["pitch"] = _dimension(sk, tg, prev, cp, True, ctx, prefix + "_pitch", pattern.get("pitch"))
            sk.GeometricConstraints.AddHorizontalAlign(prev, cp)
        else:
            pattern["pitch"] = _dimension(sk, tg, prev, cp, False, ctx, prefix + "_pitch", pattern.get("pitch"))
            sk.GeometricConstraints.AddVerticalAlign(prev, cp)
        sk.DimensionConstraints.AddDiameter(c, _text_point(tg, cp, cp, True)).Parameter.Expression = pattern["d"]
        pattern["last"] = cp
        return
    _fix_position(sk, tg, origin, cp, ctx, prefix, tag)
    dim = sk.DimensionConstraints.AddDiameter(c, tg.CreatePoint2d(cp.Geometry.X + 0.8, cp.Geometry.Y + 0.8))
    _tidy(dim)
    d_mm = round(c.Radius * 20.0, 2)
    if d_mm in ctx.diameters:  # same diameter -> same parameter
        dim.Parameter.Expression = ctx.diameters[d_mm]
        name = ctx.diameters[d_mm]
    else:
        name = ctx.name_parameter(dim, "%s_D%s" % (prefix, tag))
        if name:
            ctx.diameters[d_mm] = name
    if pattern is not None:
        pattern["d"], pattern["last"] = name, cp


def _corners(lines):
    pts = []
    for line in lines:
        pts += [line.StartSketchPoint, line.EndSketchPoint]
    xs = [p.Geometry.X for p in pts]
    ys = [p.Geometry.Y for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

    def find(x, y):
        return next(p for p in pts if abs(p.Geometry.X - x) < 1e-7 and abs(p.Geometry.Y - y) < 1e-7)

    return find(x0, y0), find(x1, y0), find(x0, y1)


def parametrize_rectangle(sk, tg, origin, ctx, lines, prefix, tag, pattern, k):
    ll, lr, ul = _corners(lines)
    if pattern is not None and k > 0:
        prev, dx = pattern["last"], pattern["dx"]
        if abs(dx) > 1e-9:
            pattern["pitch"] = _dimension(sk, tg, prev, ll, True, ctx, prefix + "_pitch", pattern.get("pitch"))
            sk.GeometricConstraints.AddHorizontalAlign(prev, ll)
        else:
            pattern["pitch"] = _dimension(sk, tg, prev, ll, False, ctx, prefix + "_pitch", pattern.get("pitch"))
            sk.GeometricConstraints.AddVerticalAlign(prev, ll)
        _dimension(sk, tg, ll, lr, True, ctx, "", pattern["w"])
        _dimension(sk, tg, ll, ul, False, ctx, "", pattern["h"])
        pattern["last"] = ll
        return
    _fix_position(sk, tg, origin, ll, ctx, prefix, tag)
    w = _dimension(sk, tg, ll, lr, True, ctx, "%s_w%s" % (prefix, tag))
    h = _dimension(sk, tg, ll, ul, False, ctx, "%s_h%s" % (prefix, tag))
    if pattern is not None:
        pattern["w"], pattern["h"], pattern["last"] = w, h, ll


def _try(fn, *args):
    """Call an Inventor API function that may refuse because the sketch would become over-constrained."""
    try:
        return fn(*args)
    except Exception:
        return None


def _attempt(fn, *args):
    """Call an Inventor API function; True when it did not raise."""
    try:
        fn(*args)
        return True
    except Exception:
        return False


def parametrize_contour(sk, tg, origin, ctx, ents, prefix, tag, tangents=()):
    """Constraints and dimensions that express the design intent of a contour:

    * tangent junctions get a tangent constraint (a fillet stays a fillet when a dimension changes);
    * horizontal/vertical lines get H/V constraints;
    * dimensions: positions of ordinary corners (chained along X and Y), then arc radii. Points where
      segments flow tangentially into each other get no dimension of their own: tangency plus the radii already
      determine them, and an extra dimension there would over-constrain (and break) the sketch.
    """
    verts = {}
    pts = []

    def vertex_id(sp_):
        key = _vertex_key(sp_.Geometry)
        if key not in verts:
            verts[key] = len(pts)
            pts.append(sp_)
        return verts[key]

    edges = []
    for e, kind in ents:
        edges.append((vertex_id(e.StartSketchPoint), vertex_id(e.EndSketchPoint), e, kind))
    n_edges = len(edges)

    check = bool(tangents)  # redundancy only matters (and is only hidden) when tangent constraints are present
    tangent_vertices = set()
    for i in tangents:
        e1, e2 = edges[i][2], edges[(i + 1) % n_edges][2]
        # the junction is the vertex the two edges share (Inventor swaps start and end of clockwise arcs)
        shared = set(edges[i][:2]) & set(edges[(i + 1) % n_edges][:2])
        if not shared:
            continue
        tangent_vertices.update(shared)
        g = pts[next(iter(shared))].Geometry
        if _try(sk.GeometricConstraints.AddTangent, e1, e2, tg.CreatePoint2d(g.X, g.Y)) is None:
            _try(sk.GeometricConstraints.AddTangent, e1, e2)

    ux, uy = list(range(len(pts))), list(range(len(pts)))

    def root(u, i):
        while u[i] != i:
            u[i] = u[u[i]]
            i = u[i]
        return i

    for a, b, e, kind in edges:
        if kind != "L":
            continue
        ga, gb = pts[a].Geometry, pts[b].Geometry
        if abs(ga.X - gb.X) < 1e-7 and abs(ga.Y - gb.Y) > 1e-7:
            _try(sk.GeometricConstraints.AddVertical, e)
            ux[root(ux, a)] = root(ux, b)
        elif abs(ga.Y - gb.Y) < 1e-7 and abs(ga.X - gb.X) > 1e-7:
            _try(sk.GeometricConstraints.AddHorizontal, e)
            uy[root(uy, a)] = root(uy, b)

    anchored = {True: False, False: False}  # is the contour positioned against the origin along X / Y?
    free_reps = {True: [], False: []}       # tangent-only classes: candidates to anchor a floating contour
    for u, horizontal in ((ux, True), (uy, False)):
        classes = {}
        for i in range(len(pts)):
            classes.setdefault(root(u, i), []).append(i)
        reps = []
        for members in classes.values():
            ordinary = [i for i in members if i not in tangent_vertices]
            if ordinary:
                reps.append(ordinary[0])
            else:  # a class made only of tangent points is determined by the tangencies...
                free_reps[horizontal].append(members[0])  # ...unless nothing ties the contour to the origin
        key = (lambda i: pts[i].Geometry.X) if horizontal else (lambda i: pts[i].Geometry.Y)
        reps.sort(key=key)
        free_reps[horizontal].sort(key=key)
        anchor = None
        for n_, i in enumerate(reps, 1):
            label = "%s_%s%s%d" % (prefix, "H" if horizontal else "V", tag, n_)
            g = pts[i].Geometry
            if anchor is None:
                if abs(g.X if horizontal else g.Y) < 1e-7:
                    align = (sk.GeometricConstraints.AddVerticalAlign if horizontal
                             else sk.GeometricConstraints.AddHorizontalAlign)
                    anchored[horizontal] = _attempt(align, origin, pts[i]) or anchored[horizontal]
                else:
                    anchored[horizontal] = _attempt(_dimension, sk, tg, origin, pts[i], horizontal, ctx, label,
                                                    None, check) or anchored[horizontal]
            else:
                _try(_dimension, sk, tg, pts[anchor], pts[i], horizontal, ctx, label, None, check)
            anchor = i
    for a, b, e, kind in edges:
        if kind == "A":
            _try(_radius_dimension, sk, tg, e, ctx, "%s_R%s" % (prefix, tag), check)
    # a contour made only of tangent points would float: tie it to the origin with one dimension per direction
    for horizontal in (True, False):
        if anchored[horizontal]:
            continue
        for n_, i in enumerate(free_reps[horizontal], 1):
            g = pts[i].Geometry
            if abs(g.X if horizontal else g.Y) < 1e-7:
                align = (sk.GeometricConstraints.AddVerticalAlign if horizontal
                         else sk.GeometricConstraints.AddHorizontalAlign)
                done = _attempt(align, origin, pts[i])
            else:
                label = "%s_%s%s_pos" % (prefix, "H" if horizontal else "V", tag)
                done = _attempt(_dimension, sk, tg, origin, pts[i], horizontal, ctx, label, None, True)
            if done:
                break


def _segment_directions(contour):
    """For every segment of a contour: (start direction, end direction) as unit vectors, following the travel."""
    pts = [tuple(contour["start"])] + [(sg[-2], sg[-1]) for sg in contour["segments"]]
    out = []
    for i, sg in enumerate(contour["segments"]):
        p0, p1 = pts[i], pts[i + 1]
        if sg[0] == "L":
            d = (p1[0] - p0[0], p1[1] - p0[1])
            n = math.hypot(*d) or 1.0
            u = (d[0] / n, d[1] / n)
            out.append((u, u))
            continue
        pm = (sg[1], sg[2])
        ax, ay = p0
        bx, by = pm
        cx, cy = p1
        det = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(det) < 1e-12:
            d = (p1[0] - p0[0], p1[1] - p0[1])
            n = math.hypot(*d) or 1.0
            u = (d[0] / n, d[1] / n)
            out.append((u, u))
            continue
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / det
        uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / det
        ccw = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 0

        def tangent_at(p):
            rx, ry = p[0] - ux, p[1] - uy
            n = math.hypot(rx, ry) or 1.0
            t = (-ry / n, rx / n)
            return t if ccw else (-t[0], -t[1])

        out.append((tangent_at(p0), tangent_at(p1)))
    return out


def tangent_junctions(contour, max_angle_deg=4.0):
    """Indices ``i`` such that segment ``i`` flows tangentially into segment ``(i + 1) % n``."""
    dirs = _segment_directions(contour)
    n = len(dirs)
    limit = math.cos(math.radians(max_angle_deg))
    found = []
    for i in range(n):
        a = dirs[i][1]
        b = dirs[(i + 1) % n][0]
        if a[0] * b[0] + a[1] * b[1] >= limit:
            found.append(i)
    return found


# ----------------------------------------------------------------------------- drawing
def legacy_mapper(tg):
    return lambda x, y: tg.CreatePoint2d(x * MM, y * MM)


def draw_polygon(sk, P, points):
    pts = [P(x, y) for x, y in points]
    lines = [sk.SketchLines.AddByTwoPoints(pts[0], pts[1])]
    for p in pts[2:]:
        lines.append(sk.SketchLines.AddByTwoPoints(lines[-1].EndSketchPoint, p))
    lines.append(sk.SketchLines.AddByTwoPoints(lines[-1].EndSketchPoint, lines[0].StartSketchPoint))
    return lines


def _nearest_end(ent, target):
    """End point (SketchPoint) of the entity that is closest to ``target``.
    Inventor always creates arcs counter-clockwise, so on a clockwise arc start and end are swapped."""
    a, b = ent.StartSketchPoint, ent.EndSketchPoint
    da = math.hypot(a.Geometry.X - target.X, a.Geometry.Y - target.Y)
    db = math.hypot(b.Geometry.X - target.X, b.Geometry.Y - target.Y)
    return a if da <= db else b


def draw_contour(sk, P, s):
    """Closed contour of lines and 3-point arcs with connected ends. Return [(entity, 'L'|'A')]."""
    segs = s["segments"]
    p_start = P(*s["start"])
    prev = first = prev_target = None
    ents = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        start = p_start if prev is None else _nearest_end(prev, prev_target)
        if seg[0] == "L":
            target = P(seg[1], seg[2])
            end = _nearest_end(first, p_start) if last else target
            ent = sk.SketchLines.AddByTwoPoints(start, end)
        else:
            target = P(seg[3], seg[4])
            end = _nearest_end(first, p_start) if last else target
            ent = sk.SketchArcs.AddByThreePoints(start, P(seg[1], seg[2]), end)
        ents.append((ent, seg[0]))
        if first is None:
            first = ent
        prev, prev_target = ent, target
    return ents


def repair_line_contour(s):
    """A contour that touches or self-intersects (e.g. two bodies joined by a zero-width edge) becomes a list
    of valid polygons (inner holes included as nested loops). Return None when no repair is needed."""
    from shapely.geometry import Polygon
    from shapely.validation import make_valid
    if any(seg[0] != "L" for seg in s["segments"]):
        pts = [tuple(q) for q in sp.contour_points(s, 16)]  # arcs sampled into segments
    else:
        pts = [tuple(s["start"])] + [(seg[1], seg[2]) for seg in s["segments"]]
    # close with a tolerance: sampled arcs leave ~1e-14 of noise on the last point
    if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
        pts = pts[:-1]
    pol = Polygon(pts)
    if pol.is_valid:
        return None
    fixed = make_valid(pol)

    def flatten(g):
        if hasattr(g, "geoms"):
            for h in g.geoms:
                yield from flatten(h)
        else:
            yield g

    result = []
    for g in flatten(fixed):
        if g.geom_type != "Polygon" or g.area < 1e-6:
            continue
        result.append(list(g.exterior.coords)[:-1])
        for ring in g.interiors:
            if Polygon(ring).area >= 1e-6:
                result.append(list(ring.coords)[:-1])
    return result or None


def draw_shape(sk, P, s, arcs=True, tangents=True):
    """Draw a shape. Return items for parametrization: ('circle', c, None) | ('rect', lines, None) | ('contour', ents, tangents)."""
    shape = s["shape"]
    if shape == "rectangle":
        x, y, w, h = s["x"], s["y"], s["width"], s["height"]
        if s.get("centered"):
            x, y = x - w / 2.0, y - h / 2.0
        r = sk.SketchLines.AddAsTwoPointRectangle(P(x, y), P(x + w, y + h))
        return [("rect", [r.Item(i) for i in range(1, 5)], None)]
    if shape == "circle":
        return [("circle", sk.SketchCircles.AddByCenterRadius(P(s["x"], s["y"]), s["d"] * MM / 2.0), None)]
    if shape == "polygon":
        return [("contour", [(line, "L") for line in draw_polygon(sk, P, s["points"])], ())]
    if shape == "contour":
        repaired = repair_line_contour(s)
        if repaired is not None:
            return [("contour", [(line, "L") for line in draw_polygon(sk, P, pts)], ()) for pts in repaired]
        if arcs:
            use_tangents = tangents and len(s["segments"]) <= MAX_TANGENT_SEGMENTS
            return [("contour", draw_contour(sk, P, s), tangent_junctions(s) if use_tangents else ())]
        pts = sp.contour_points(s, 16)  # fallback: arcs approximated by line segments
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
            pts = pts[:-1]
        return [("contour", [(line, "L") for line in draw_polygon(sk, P, pts)], ())]
    if shape == "slot":
        a1, a2, m2, b2, b1, m1 = sp.slot_points(s)
        l1 = sk.SketchLines.AddByTwoPoints(P(*a1), P(*a2))
        arc1 = sk.SketchArcs.AddByThreePoints(l1.EndSketchPoint, P(*m2), P(*b2))
        l2 = sk.SketchLines.AddByTwoPoints(arc1.EndSketchPoint, P(*b1))
        sk.SketchArcs.AddByThreePoints(l2.EndSketchPoint, P(*m1), l1.StartSketchPoint)
    return []


def _linear_pattern_1d(s):
    """(count, step_mm, horizontal) of a one-dimensional linear pattern worth a sketch pattern, else None."""
    pat = s.get("pattern")
    if not pat or pat.get("type") != "linear" or s["shape"] not in ("rectangle", "circle"):
        return None
    nx, ny = int(pat.get("nx", 1)), int(pat.get("ny", 1))
    if nx >= 3 and ny == 1:
        return nx, float(pat.get("dx", 0)), True
    if ny >= 3 and nx == 1:
        return ny, float(pat.get("dy", 0)), False
    return None


def _circular_pattern_info(s):
    """(count, cx, cy) of a circle with a circular pattern, else None."""
    pat = s.get("pattern")
    if pat and pat.get("type") == "circular" and s["shape"] == "circle" and int(pat["n"]) >= 3 and "cx" in pat:
        return int(pat["n"]), pat["cx"], pat["cy"]
    return None


def make_sketch_circular_pattern(sk, tg, ctx, item, count, cx, cy, prefix, tag, P, origin):
    """Circular pattern of a drawn circle around the point (cx, cy); the count becomes a parameter."""
    kind, obj, _ = item
    col = sk.Application.TransientObjects.CreateObjectCollection()
    col.Add(obj)
    center = sk.SketchPoints.Add(P(cx, cy), False)
    for horizontal, value, key in ((True, cx, "x"), (False, cy, "y")):  # fix the center against the origin
        if abs(value) > 1e-9:
            _dimension(sk, tg, origin, center, horizontal, ctx, "%s_ring_%s%s" % (prefix, key, tag))
        else:
            (sk.GeometricConstraints.AddVerticalAlign if horizontal
             else sk.GeometricConstraints.AddHorizontalAlign)(origin, center)
    before = {q.Name for q in ctx.comp.Parameters}
    patterns = sk.CircularPatterns
    # unlike a circular pattern feature, a sketch pattern reads the angle in degrees; with Fitted=False the angle is
    # the spacing between instances (a fitted 360 degrees would put the last instance on the first)
    patterns.Add(patterns.CreateDefinition(col, center, True, count, 360.0 / count, False, True, False))
    for q in ctx.comp.Parameters:
        if q.Name not in before and str(q.Expression).strip().endswith("ul"):
            ctx.name_parameter(q, "%s_ring_count%s" % (prefix, tag))


def make_sketch_pattern(sk, tg, ctx, item, count, step_mm, horizontal, prefix, tag, P, base):
    """Rectangular pattern of a drawn rectangle/circle inside the sketch (count and pitch become parameters).

    The pattern direction is derived from the real sketch geometry: the sketch axes may be mirrored or swapped
    relative to the spec coordinates, so ``P`` maps the spec step into sketch space."""
    kind, obj, _ = item
    x, y = base["x"], base["y"]
    a = P(x, y)
    b = P(x + step_mm, y) if horizontal else P(x, y + step_mm)
    want = (b.X - a.X, b.Y - a.Y)  # intended direction of the pattern, in sketch space
    norm = (want[0] ** 2 + want[1] ** 2) ** 0.5 or 1.0
    want = (want[0] / norm, want[1] / norm)
    col = sk.Application.TransientObjects.CreateObjectCollection()
    natural = True
    if kind == "rect":
        direction = None
        for line in obj:
            col.Add(line)
            g0, g1 = line.StartSketchPoint.Geometry, line.EndSketchPoint.Geometry
            v = (g1.X - g0.X, g1.Y - g0.Y)
            n = (v[0] ** 2 + v[1] ** 2) ** 0.5 or 1.0
            dot = (v[0] * want[0] + v[1] * want[1]) / n
            if direction is None and abs(dot) > 0.99:
                direction, natural = line, dot > 0
    else:
        col.Add(obj)
        c = obj.CenterSketchPoint.Geometry
        end = tg.CreatePoint2d(c.X + want[0], c.Y + want[1])
        direction = sk.SketchLines.AddByTwoPoints(obj.CenterSketchPoint, end)
        direction.Construction = True
        (sk.GeometricConstraints.AddHorizontal if abs(want[0]) > abs(want[1])
         else sk.GeometricConstraints.AddVertical)(direction)
    if direction is None:
        raise RuntimeError("no edge parallel to the pattern direction")
    before = {q.Name for q in ctx.comp.Parameters}
    patterns = sk.RectangularPatterns
    # a number is read in document units (millimeters: create_ipt sets them); a string spacing fails silently
    definition = patterns.CreateDefinition(col, direction, count, natural, False, abs(step_mm))
    patterns.Add(definition)
    for q in ctx.comp.Parameters:  # name the two parameters Inventor created (count and pitch)
        if q.Name in before:
            continue
        try:
            if str(q.Expression).strip().endswith("ul"):
                ctx.name_parameter(q, "%s_count%s" % (prefix, tag))
            else:
                ctx.name_parameter(q, "%s_pitch%s" % (prefix, tag))
        except Exception:
            pass


def draw_sketch(sk, tg, P, sketch, arcs, ctx, prefix, tangents=True, patterns=True):
    """Draw all shapes and (when ``ctx`` is given) add the parametric constraints and dimensions."""
    origin = sk.AddByProjectingEntity(ctx.comp.WorkPoints.Item(1)) if ctx is not None else None
    items = []  # (item, shape dict)
    pending = []  # (item, count, step, horizontal): shapes drawn once and patterned afterwards
    base_of = {}  # id(item) -> the shape dict it was drawn from
    rings = []  # (item, count, cx, cy): circles drawn once and patterned around a center
    for s in sketch:
        ring = _circular_pattern_info(s) if (patterns and ctx is not None) else None
        if ring is not None:
            base = {k: v for k, v in s.items() if k != "pattern"}
            for it in draw_shape(sk, P, base, arcs, tangents):
                items.append((it, base))
                rings.append((it,) + ring)
            continue
        info = _linear_pattern_1d(s) if (patterns and ctx is not None) else None
        if info is not None:
            base = {k: v for k, v in s.items() if k != "pattern"}
            for it in draw_shape(sk, P, base, arcs, tangents):
                items.append((it, base))
                pending.append((it,) + info)
                base_of[id(it)] = base
            continue
        for e in sp.expand_shape(s):
            for it in draw_shape(sk, P, e, arcs, tangents):
                items.append((it, e))
    if ctx is None:
        return
    try:
        g0 = origin.Geometry
        if abs(g0.X) > 1e-7 or abs(g0.Y) > 1e-7:
            raise RuntimeError("sketch origin is not at (0, 0)")
        patterns_state = {}
        for i, ((kind, obj, extra), s) in enumerate(items, 1):
            tag = str(i)
            pid, k = s.get("_pattern", (None, 0))
            pattern = None
            if pid is not None:
                pattern = patterns_state.setdefault(pid, {"dx": s["_dx"], "dy": s["_dy"]})
            if kind == "circle":
                parametrize_circle(sk, tg, origin, ctx, obj, prefix, tag, pattern, k)
            elif kind == "rect":
                parametrize_rectangle(sk, tg, origin, ctx, obj, prefix, tag, pattern, k)
            else:
                parametrize_contour(sk, tg, origin, ctx, obj, prefix, tag, extra or ())
        for n, (it, count, step, horizontal) in enumerate(pending, 1):
            make_sketch_pattern(sk, tg, ctx, it, count, step, horizontal, prefix, str(n), P, base_of[id(it)])
        for n, (it, count, cx, cy) in enumerate(rings, 1):
            make_sketch_circular_pattern(sk, tg, ctx, it, count, cx, cy, prefix, str(n), P, origin)
    except Exception as e:
        print("  warning: sketch '%s' is not fully parametrized: %s" % (prefix, e))


# ----------------------------------------------------------------------------- features
def part_volume(comp):
    try:
        return float(comp.MassProperties.Volume)
    except Exception:
        return None


def plane_normal(comp, tg, wp):
    try:
        n = wp.Plane.Normal
        return (n.X, n.Y, n.Z)
    except Exception:
        sk = comp.Sketches.Add(wp)  # temporary sketch, only to measure the orientation
        _, eu, ev = sketch_basis(sk, tg)
        sk.Delete()
        return cross(eu, ev)


def set_name(obj, name):
    try:
        obj.Name = name
    except Exception:
        pass


def offset_plane(comp, tg, plane, offset, ctx, prefix):
    n, _, _ = sp.PLANE_AXES[plane]
    wp0 = comp.WorkPlanes.Item(PLANE_INDEX[plane])
    nrm = plane_normal(comp, tg, wp0)
    plane_sign = 1 if nrm[n] > 0 else -1
    if abs(offset) < 1e-9:
        return wp0
    wp = comp.WorkPlanes.AddByPlaneAndOffset(wp0, offset * plane_sign * MM)
    if ctx is not None:
        try:
            ctx.name_parameter(wp.Definition.Offset, prefix + "_position")
        except Exception:
            pass
    return wp


SKETCH_SCHEMES = ((True, True, True), (True, True, False), (True, False, False), (False, False, False))
# (arcs, tangent constraints, sketch patterns): from the most parametric scheme to plain line segments


def _profile_matches(prof, f, exact=True):
    """True when the profile has the area the spec says (a sketch that the solver deformed would not). Sketches
    that replace arcs by line segments are only approximately equal to the spec, so they get a looser limit."""
    try:
        area_mm2 = prof.RegionProperties.Area / (MM * MM)
    except Exception:
        return True  # cannot be checked
    expected = sp.sketch_geometry(f["sketch"]).area
    return abs(area_mm2 - expected) <= max((0.01 if exact else 0.04) * expected, 1.0)


def _extrude(comp, prof, f, op, directions, name):
    """Extrude in the preferred direction, falling back to the opposite one. Return the feature or None."""
    ext = comp.Features.ExtrudeFeatures
    before = part_volume(comp)
    feat = None
    for attempt, direction in enumerate(directions):
        try:
            if f["distance"] == sp.THROUGH_ALL:
                d = ext.CreateExtrudeDefinition(prof, op)
                d.SetThroughAllExtent(direction)
                feat = ext.Add(d)
            else:
                feat = ext.AddByDistanceExtent(prof, f["distance"] * MM, direction, op)
        except Exception as e:
            feat = None
            if attempt == 0:
                print("  warning: extrusion failed in '%s' (%s); trying the opposite direction" % (name, e))
            continue
        after = part_volume(comp)
        if before is None or after is None or abs(after - before) > 1e-9:
            return feat  # the volume changed: correct direction (or it cannot be checked)
        if attempt == 0:  # nothing changed: try the opposite direction
            try:
                feat.Delete()
            except Exception:
                return feat
            feat = None
            print("  warning: reversed direction in '%s', trying the opposite" % name)
    return feat


def add_extrusion(comp, tg, f, ctx=None):
    plane = f["plane"]
    n, A, B = sp.PLANE_AXES[plane]
    name = f.get("name", "Feature")
    prefix = _slug(name)[:24]
    wp = offset_plane(comp, tg, plane, f.get("offset", 0), ctx, prefix)
    op = OPS[f.get("operation", "new")]

    feat = None
    for arcs, tangents, patterns in SKETCH_SCHEMES:
        saved = (set(ctx.used), dict(ctx.diameters), dict(ctx.radii)) if ctx is not None else None
        sk = comp.Sketches.Add(wp)
        basis = sketch_basis(sk, tg)
        P = make_mapper(tg, basis, A, B)
        set_name(sk, "Sketch - " + name)
        draw_sketch(sk, tg, P, f["sketch"], arcs, ctx, prefix, tangents, patterns)
        reason = None
        try:
            prof = sk.Profiles.AddForSolid()
        except Exception as e:
            prof, reason = None, "no closed profile (%s)" % e
        if prof is not None and not _profile_matches(prof, f, exact=arcs):
            reason = "the sketch no longer matches the spec"
        if reason is None:
            sketch_sign = 1 if cross(basis[1], basis[2])[n] > 0 else -1
            directions = ([K_POSITIVE, K_NEGATIVE] if f.get("direction", 1) * sketch_sign > 0
                          else [K_NEGATIVE, K_POSITIVE])
            feat = _extrude(comp, prof, f, op, directions, name)
            if feat is not None:
                break
            reason = "extrusion failed"
        try:
            sk.Delete()
        except Exception:
            pass
        if ctx is not None:  # forget the parameter names of the discarded sketch
            ctx.used, ctx.diameters, ctx.radii = saved
        print("  warning: %s in '%s' (arcs=%s, tangents=%s, patterns=%s); trying a simpler sketch"
              % (reason, name, arcs, tangents, patterns))
    if feat is None:
        raise RuntimeError("Extrusion '%s' failed with every sketch scheme" % name)
    set_name(feat, name)
    if ctx is not None and f["distance"] != sp.THROUGH_ALL:
        try:
            ctx.name_parameter(feat.Definition.Extent.Distance, prefix + "_depth")
        except Exception:
            pass
    return feat


def add_hole(app, comp, tg, f, ctx=None):
    """Inventor hole feature (simple, countersink or counterbore) at one or more positions."""
    plane = f["plane"]
    n, A, B = sp.PLANE_AXES[plane]
    name = f.get("name", "Hole")
    prefix = _slug(name)[:24]
    wp = offset_plane(comp, tg, plane, f.get("offset", 0), ctx, prefix)
    d, depth = f["d"] * MM, f["depth"] * MM
    ring = f.get("pattern") if (f.get("pattern") or {}).get("type") == "circular" else None
    positions = f["points"][:1] if ring else f["points"]  # a ring of holes: one hole, then a circular pattern
    holes = comp.Features.HoleFeatures
    before = part_volume(comp)
    feat = None
    last_error = None
    for attempt in range(2):
        sk = comp.Sketches.Add(wp)  # a new sketch per attempt: a deleted hole's sketch cannot be reused
        origin = sk.AddByProjectingEntity(comp.WorkPoints.Item(1))
        basis = sketch_basis(sk, tg)
        P = make_mapper(tg, basis, A, B)
        set_name(sk, "Sketch - " + name)
        pts = [sk.SketchPoints.Add(P(x, y), True) for x, y in positions]
        if ctx is not None and attempt == 0:
            try:
                for i, p in enumerate(pts, 1):
                    _fix_position(sk, tg, origin, p, ctx, prefix, str(i))
            except Exception as e:
                print("  warning: positions of hole '%s' have no dimensions: %s" % (name, e))
        sketch_sign = 1 if cross(basis[1], basis[2])[n] > 0 else -1
        # for an Inventor hole, "positive" is opposite to the sketch normal (unlike an extrusion)
        preferred = K_NEGATIVE if f.get("direction", 1) * sketch_sign > 0 else K_POSITIVE
        direction = preferred if attempt == 0 else (K_POSITIVE if preferred == K_NEGATIVE else K_NEGATIVE)
        col = app.TransientObjects.CreateObjectCollection()
        for p in pts:
            col.Add(p)
        try:
            place = holes.CreateSketchPlacementDefinition(col)
            subtype = f.get("subtype", "simple")
            if subtype == "countersink":
                feat = holes.AddCSinkByDistanceExtent(place, d, depth, direction, f["head_d"] * MM,
                                                      math.radians(f["angle"]), True)
            elif subtype == "counterbore":
                feat = holes.AddCBoreByDistanceExtent(place, d, depth, direction, f["head_d"] * MM,
                                                      f["head_depth"] * MM, True)
            else:
                feat = holes.AddDrilledByDistanceExtent(place, d, depth, direction, True)
        except Exception as e:
            last_error, feat = e, None
            sk.Delete()
            continue
        try:
            comp.Parent.Update()
        except Exception:
            pass
        after = part_volume(comp)
        if before is None or after is None or abs(after - before) > 1e-9:
            break
        print("  warning: reversed direction in '%s', trying the opposite" % name)
        try:
            feat.Delete()
            sk.Delete()
        except Exception:
            break
        feat = None
    if feat is None:
        raise RuntimeError("Hole '%s' failed: %s" % (name, last_error))
    set_name(feat, name)
    if ctx is not None:
        for attr, tag in (("HoleDiameter", "D"), ("CSinkDiameter", "D_head"), ("CSinkAngle", "angle"),
                          ("CBoreDiameter", "D_head"), ("CBoreDepth", "head_depth")):
            try:
                ctx.name_parameter(getattr(feat, attr), "%s_%s" % (prefix, tag))
            except Exception:
                pass
        try:
            ctx.name_parameter(feat.Definition.Extent.Distance, prefix + "_depth")
        except Exception:
            pass
    if ring:
        feat = _pattern_around_axis(app, comp, tg, feat, f, ring, ctx, prefix)
    return feat


def _pattern_around_axis(app, comp, tg, feat, f, ring, ctx, prefix):
    """Circular pattern of a feature around the axis through (cx, cy) normal to the feature's plane."""
    n, A, B = sp.PLANE_AXES[f["plane"]]
    coords = [0.0, 0.0, 0.0]
    coords[A], coords[B], coords[n] = ring["cx"] * MM, ring["cy"] * MM, f.get("offset", 0) * MM
    direction = [0.0, 0.0, 0.0]
    direction[n] = 1.0
    axis = comp.WorkAxes.AddFixed(tg.CreatePoint(*coords), tg.CreateUnitVector(*direction))
    col = app.TransientObjects.CreateObjectCollection()
    col.Add(feat)
    pattern = comp.Features.CircularPatternFeatures.Add(col, axis, True, int(ring["n"]), 2 * math.pi, True,
                                                        K_COMPUTE_IDENTICAL)
    ctx.name_parameter(pattern.Count, prefix + "_count")
    set_name(pattern, "%s pattern" % f.get("name", "Hole"))
    return pattern


def add_mirror(app, comp, tg, f, ctx, built):
    """Mirror feature: mirrors the listed features (already built, looked up by name) about a plane."""
    name = f.get("name", "Mirror")
    prefix = _slug(name)[:24]
    wp = offset_plane(comp, tg, f["plane"], f.get("offset", 0), ctx, prefix)
    col = app.TransientObjects.CreateObjectCollection()
    for feature_name in f["features"]:
        col.Add(built[feature_name])
    feat = comp.Features.MirrorFeatures.Add(col, wp, False, K_COMPUTE_IDENTICAL)
    set_name(feat, name)
    return feat


# ----------------------------------------------------------------------------- gears
K_CIRCULAR_PARAMETRIC = 98305  # CurveEquationTypeEnum.kParametric
K_CARTESIAN = 98561            # CoordinateSystemTypeEnum.kCartesian
# Inventor refuses to extrude an involute when its equation contains pi with more than 9 digits (a parameter
# holding pi breaks it too), and equation curves only close exactly when every pi is the same literal.
PI_LITERAL = "3.14159265"
ROOT_OVERLAP_MM = 0.3  # the tooth reaches this far into the root cylinder so that the join is robust


def gear_parameters(comp, ctx, prefix, f):
    """Create the user parameters that define a spur gear and the derived radii. Return {key: parameter name}."""
    ups = comp.Parameters.UserParameters
    names = {}

    def up(key, expression, unit):
        name = ctx.unique("%s_%s" % (prefix, key))
        names[key] = name
        ups.AddByExpression(name, expression, unit)

    n = names
    up("module", "%g mm" % f["module"], "mm")
    up("teeth", "%d" % int(f["teeth"]), "ul")
    up("pressure_angle", "%g deg" % f["pressure_angle"], "deg")
    up("addendum", "%g" % f.get("addendum", 1.0), "ul")
    up("dedendum", "%g" % f.get("dedendum", 1.25), "ul")
    up("width", "%g mm" % f["distance"], "mm")
    up("phase", "%g deg" % f.get("phase", 0.0), "deg")
    up("cx", "%g mm" % f["center"][0], "mm")
    up("cy", "%g mm" % f["center"][1], "mm")
    up("pi", PI_LITERAL, "ul")
    up("pitch_r", "%s * %s / 2" % (n["module"], n["teeth"]), "mm")
    up("base_r", "%s * cos(%s)" % (n["pitch_r"], n["pressure_angle"]), "mm")
    up("tip_r", "%s + %s * %s" % (n["pitch_r"], n["addendum"], n["module"]), "mm")
    up("root_r", "%s - %s * %s" % (n["pitch_r"], n["dedendum"], n["module"]), "mm")
    up("root_inner_r", "%s - %g mm" % (n["root_r"], ROOT_OVERLAP_MM), "mm")
    up("inv_a", "(tan(%s) - %s / 1 rad) * 180 / %s" % (n["pressure_angle"], n["pressure_angle"], n["pi"]), "ul")
    up("psi", "90 / %s + %s" % (n["teeth"], n["inv_a"]), "ul")          # half tooth angle at the base circle (deg)
    up("psi_a", "%s * 1 deg" % n["psi"], "deg")
    up("roll_tip", "sqrt((%s / %s) ^ 2 - 1) * 180 / %s" % (n["tip_r"], n["base_r"], n["pi"]), "ul")  # deg
    return names


def gear_curve_expressions(n, mapping):
    """Equation-curve expressions (x, y, t_min, t_max) of the tooth flanks and the root arc, in sketch space.

    ``n`` maps parameter keys to names; ``mapping`` is the 2x2 integer matrix from gear space to sketch space.
    Angles inside the equations are degrees (Inventor reads trig arguments of equation curves as degrees)."""
    m00, m01, m10, m11 = mapping
    base = "%s / 1 mm" % n["base_r"]
    inv_x = "%s * (cos(t) + t * %s / 180 * sin(t))" % (base, PI_LITERAL)
    inv_y = "%s * (sin(t) - t * %s / 180 * cos(t))" % (base, PI_LITERAL)
    psi = n["psi_a"]
    lower = ("(%s) * cos(%s) + (%s) * sin(%s)" % (inv_x, psi, inv_y, psi),
             "-(%s) * sin(%s) + (%s) * cos(%s)" % (inv_x, psi, inv_y, psi))
    upper = (lower[0], "(%s) * sin(%s) - (%s) * cos(%s)" % (inv_x, psi, inv_y, psi))
    root = ("%s / 1 mm * cos(t)" % n["root_inner_r"], "%s / 1 mm * sin(t)" % n["root_inner_r"])

    def place(xy):
        x, y = xy
        phase = n["phase"]
        gx = "%s / 1 mm + (%s) * cos(%s) - (%s) * sin(%s)" % (n["cx"], x, phase, y, phase)
        gy = "%s / 1 mm + (%s) * sin(%s) + (%s) * cos(%s)" % (n["cy"], x, phase, y, phase)
        return ("(%d) * (%s) + (%d) * (%s)" % (m00, gx, m01, gy), "(%d) * (%s) + (%d) * (%s)" % (m10, gx, m11, gy))

    return {"lower": place(lower) + ("0", n["roll_tip"]),
            "upper": place(upper) + ("0", n["roll_tip"]),
            "root": place(root) + ("-" + n["psi"], n["psi"])}


def _sketch_matrix(tg, P):
    """Integer 2x2 matrix [[m00, m01], [m10, m11]] that maps spec-space (x, y) to sketch space (cm / MM)."""
    o, ex, ey = P(0, 0), P(1, 0), P(0, 1)
    m = [round((ex.X - o.X) / MM), round((ey.X - o.X) / MM), round((ex.Y - o.Y) / MM), round((ey.Y - o.Y) / MM)]
    if any(v not in (-1, 0, 1) for v in m):
        raise RuntimeError("the gear plane is not axis aligned")
    return m


def add_gear(app, comp, tg, f, ctx):
    """Spur gear: root cylinder + one involute tooth + a circular pattern, all driven by user parameters."""
    name = f.get("name", "Spur gear")
    prefix = _slug(name)[:24]
    rr = gears.radii(f["module"], int(f["teeth"]), f["pressure_angle"], f.get("dedendum", 1.25),
                     f.get("addendum", 1.0))
    if rr["root"] >= rr["base"]:
        raise RuntimeError("gears with the root circle outside the base circle are not supported")
    plane = f["plane"]
    n, A, B = sp.PLANE_AXES[plane]

    # 1) root cylinder
    cx, cy = f["center"]
    body_spec = {"type": "extrusion", "name": name, "plane": plane, "operation": "new", "offset": f.get("offset", 0),
                 "direction": f.get("direction", 1), "distance": f["distance"],
                 "sketch": [{"shape": "circle", "x": cx, "y": cy, "d": 2 * rr["root"]}]}
    body = add_extrusion(comp, tg, body_spec, ctx)
    names = gear_parameters(comp, ctx, prefix, f)
    _link(comp, "%s_D1" % prefix, "2 * %s" % names["root_r"])
    _link(comp, "%s_x1" % prefix, names["cx"])
    _link(comp, "%s_y1" % prefix, names["cy"])
    _link(comp, "%s_depth" % prefix, names["width"])

    # 2) one tooth, drawn with equation curves on the same plane
    tooth = _draw_tooth(comp, tg, ctx, f, name, names, body.Definition.Profile.Parent.PlanarEntity, A, B, n)

    # 3) circular pattern around the cylinder axis; the count follows the teeth parameter
    axis = comp.WorkAxes.AddByRevolvedFace(body.SideFaces.Item(1))
    col = app.TransientObjects.CreateObjectCollection()
    col.Add(tooth)
    pattern = comp.Features.CircularPatternFeatures.Add(col, axis, True, int(f["teeth"]), 2 * math.pi, True,
                                                        K_COMPUTE_IDENTICAL)
    try:
        pattern.Count.Expression = names["teeth"]
    except Exception:
        pass
    set_name(pattern, name + " teeth")
    return pattern


def _draw_tooth(comp, tg, ctx, f, name, names, plane_entity, A, B, n):
    """Sketch one involute tooth with equation curves on ``plane_entity`` and join-extrude it by the face width."""
    cx, cy = f["center"]
    sk = comp.Sketches.Add(plane_entity)
    basis = sketch_basis(sk, tg)
    P = make_mapper(tg, basis, A, B)
    set_name(sk, "Sketch - %s tooth" % name)
    m00, m01, m10, m11 = _sketch_matrix(tg, P)
    curves = {}
    for key, (x, y, lo, hi) in gear_curve_expressions(names, (m00, m01, m10, m11)).items():
        curves[key] = sk.SketchEquationCurves.Add(K_CIRCULAR_PARAMETRIC, K_CARTESIAN, x, y, lo, hi)
    origin = sk.AddByProjectingEntity(comp.WorkPoints.Item(1))
    center = sk.SketchPoints.Add(P(cx, cy), False)
    for horizontal, value, key in ((True, cx, "cx"), (False, cy, "cy")):  # the arc center follows the parameters
        if abs(value) > 1e-9:
            _dimension(sk, tg, origin, center, horizontal, ctx, "%s_%s" % (_slug(name)[:24], key), names[key])
        else:
            (sk.GeometricConstraints.AddVerticalAlign if horizontal
             else sk.GeometricConstraints.AddHorizontalAlign)(origin, center)
    ccw = (m00 * m11 - m01 * m10) > 0
    sk.SketchArcs.AddByCenterStartEndPoint(center, curves["lower"].EndSketchPoint, curves["upper"].EndSketchPoint, ccw)
    sk.SketchLines.AddByTwoPoints(curves["lower"].StartSketchPoint, curves["root"].StartSketchPoint)
    sk.SketchLines.AddByTwoPoints(curves["upper"].StartSketchPoint, curves["root"].EndSketchPoint)
    prof = sk.Profiles.AddForSolid()
    sketch_sign = 1 if cross(basis[1], basis[2])[n] > 0 else -1
    directions = [K_POSITIVE, K_NEGATIVE] if f.get("direction", 1) * sketch_sign > 0 else [K_NEGATIVE, K_POSITIVE]
    tooth = _extrude(comp, prof, {"distance": f["distance"]}, K_JOIN, directions, name + " tooth")
    if tooth is None:
        raise RuntimeError("could not extrude the tooth of '%s'" % name)
    set_name(tooth, name + " tooth")
    try:
        tooth.Definition.Extent.Distance.Expression = names["width"]
    except Exception:
        pass
    return tooth


def _link(comp, parameter_name, expression):
    """Drive an existing parameter by an expression (silently skipped when the parameter does not exist)."""
    if expression is None:
        return
    try:
        comp.Parameters.Item(parameter_name).Expression = expression
    except Exception:
        pass


def add_legacy_extrusion(comp, tg, f):
    wp = comp.WorkPlanes.Item(PLANE_INDEX[f.get("plane", "XY")])
    if f.get("offset"):
        wp = comp.WorkPlanes.AddByPlaneAndOffset(wp, f["offset"] * MM)
    sk = comp.Sketches.Add(wp)
    P = legacy_mapper(tg)
    for s in sp.sketch_shapes(f["sketch"]):
        draw_shape(sk, P, s)
    prof = sk.Profiles.AddForSolid()
    op = OPS[f.get("operation", "new")]
    direction = EXTENT_DIRECTIONS[f.get("extent_direction", "positive")]
    ext = comp.Features.ExtrudeFeatures
    if f["distance"] == sp.THROUGH_ALL:
        d = ext.CreateExtrudeDefinition(prof, op)
        d.SetThroughAllExtent(direction)
        ext.Add(d)
    else:
        ext.AddByDistanceExtent(prof, f["distance"] * MM, direction, op)


def add_revolve(comp, tg, f):
    """Profile [[radius, y], ...] revolved around the sketch y axis (x = 0)."""
    wp = comp.WorkPlanes.Item(PLANE_INDEX[f.get("plane", "XY")])
    if f.get("offset"):
        wp = comp.WorkPlanes.AddByPlaneAndOffset(wp, f["offset"] * MM)
    sk = comp.Sketches.Add(wp)
    P = legacy_mapper(tg)
    lines = draw_polygon(sk, P, f["profile"])
    axis = None
    for line in lines:
        if abs(line.StartSketchPoint.Geometry.X) < 1e-9 and abs(line.EndSketchPoint.Geometry.X) < 1e-9:
            axis = line
            break
    if axis is None:
        ys = [p[1] for p in f["profile"]]
        axis = sk.SketchLines.AddByTwoPoints(P(0, min(ys)), P(0, max(ys)))
        axis.Construction = True
    prof = sk.Profiles.AddForSolid()
    op = OPS[f.get("operation", "new")]
    rev = comp.Features.RevolveFeatures
    angle = f.get("angle", 360)
    if angle >= 360:
        rev.AddFull(prof, axis, op)
    else:
        rev.AddByAngle(prof, axis, math.radians(angle), K_POSITIVE, op)


def select_edges(app, comp, selection):
    body = comp.SurfaceBodies.Item(1)
    zs = []
    for e in body.Edges:
        try:
            zs.append(e.StartVertex.Point.Z)
            zs.append(e.StopVertex.Point.Z)
        except Exception:
            pass
    zmax, zmin = (max(zs), min(zs)) if zs else (0, 0)
    eps = 1e-6
    col = app.TransientObjects.CreateEdgeCollection()
    count = 0
    for e in body.Edges:
        try:
            p, q = e.StartVertex.Point, e.StopVertex.Point
        except Exception:
            continue
        if selection == "all":
            ok = True
        elif selection == "vertical":
            ok = abs(p.X - q.X) < eps and abs(p.Y - q.Y) < eps and abs(p.Z - q.Z) > eps
        elif selection == "top":
            ok = abs(p.Z - zmax) < eps and abs(q.Z - zmax) < eps
        elif selection == "bottom":
            ok = abs(p.Z - zmin) < eps and abs(q.Z - zmin) < eps
        else:
            ok = False
        if ok:
            col.Add(e)
            count += 1
    return col, count


def add_finish(app, comp, f):
    col, count = select_edges(app, comp, f.get("selection", "all"))
    if count == 0:
        print("  warning: no edge selected for", f["type"])
        return
    if f["type"] == "fillet":
        comp.Features.FilletFeatures.AddSimple(col, f["radius"] * MM)
    else:
        comp.Features.ChamferFeatures.AddUsingDistance(col, f["distance"] * MM)


def create_ipt(spec, output, app=None, close=False):
    """Build the part described by ``spec`` and save it as ``output`` (.ipt)."""
    if app is None:
        import win32com.client.dynamic  # late import: validation-only runs do not need pywin32
        app = win32com.client.dynamic.Dispatch("Inventor.Application")  # dynamic: independent of gen_py
    app.Visible = True
    tg = app.TransientGeometry
    template = app.FileManager.GetTemplateFile(K_PART_DOCUMENT)
    doc = app.Documents.Add(K_PART_DOCUMENT, template, True)
    comp = doc.ComponentDefinition
    try:  # document units in millimeters (the default template may be in inches)
        doc.UnitsOfMeasure.LengthUnits = K_MILLIMETER_UNITS
    except Exception:
        print("  warning: could not set the document units to mm")

    for name, value in spec.get("params", {}).items():
        comp.Parameters.UserParameters.AddByValue(name, float(value), "mm")

    model = spec.get("system") == "model"
    ctx = Context(comp) if model else None
    built = {}  # feature name -> Inventor feature (mirror features refer to them)
    for i, f in enumerate(spec["features"], 1):
        print("Feature %d: %s %s" % (i, f["type"], f.get("name", "")))
        if f["type"] == "extrusion":
            if model:
                built[f.get("name")] = add_extrusion(comp, tg, f, ctx)
            else:
                add_legacy_extrusion(comp, tg, f)
        elif f["type"] == "hole":
            built[f.get("name")] = add_hole(app, comp, tg, f, ctx)
        elif f["type"] == "gear":
            built[f.get("name")] = add_gear(app, comp, tg, f, ctx)
        elif f["type"] == "mirror":
            add_mirror(app, comp, tg, f, ctx, built)
        elif f["type"] == "revolve":
            add_revolve(comp, tg, f)
        elif f["type"] in ("fillet", "chamfer"):
            add_finish(app, comp, f)

    doc.Update()
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    doc.SaveAs(os.path.abspath(output), False)
    print("Saved:", output)
    if close:
        doc.Close(True)
