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
OPS = {"new": K_NEW_BODY, "join": K_JOIN, "cut": K_CUT}
EXTENT_DIRECTIONS = {"positive": K_POSITIVE, "negative": K_NEGATIVE, "symmetric": K_SYMMETRIC}
PLANE_INDEX = {"YZ": 1, "XZ": 2, "XY": 3}
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
        try:
            for p in comp.Parameters:
                self.used.add(p.Name)
        except Exception:
            pass

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


def _dimension(sk, tg, a, b, horizontal, ctx, name, expr=None):
    d = sk.DimensionConstraints.AddTwoPointDistance(
        a, b, K_HORIZONTAL_DIM if horizontal else K_VERTICAL_DIM, _text_point(tg, a, b, horizontal))
    if expr:
        d.Parameter.Expression = expr
        return expr
    return ctx.name_parameter(d, name)


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


def parametrize_contour(sk, tg, origin, ctx, ents, prefix, tag):
    """H/V constraints + chained dimensions (every X and Y 'step' gets one) + arc radii."""
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
            sk.GeometricConstraints.AddVertical(e)
            ux[root(ux, a)] = root(ux, b)
        elif abs(ga.Y - gb.Y) < 1e-7 and abs(ga.X - gb.X) > 1e-7:
            sk.GeometricConstraints.AddHorizontal(e)
            uy[root(uy, a)] = root(uy, b)
    for u, horizontal in ((ux, True), (uy, False)):
        reps = {}
        for i in range(len(pts)):
            reps.setdefault(root(u, i), i)
        order = sorted(reps.values(), key=lambda i: (pts[i].Geometry.X if horizontal else pts[i].Geometry.Y))
        prev = None
        for n_, i in enumerate(order, 1):
            label = "%s_%s%s%d" % (prefix, "H" if horizontal else "V", tag, n_)
            if prev is None:
                g = pts[i].Geometry
                if abs(g.X if horizontal else g.Y) < 1e-7:
                    align = (sk.GeometricConstraints.AddVerticalAlign if horizontal
                             else sk.GeometricConstraints.AddHorizontalAlign)
                    align(origin, pts[i])
                else:
                    _dimension(sk, tg, origin, pts[i], horizontal, ctx, label)
            else:
                _dimension(sk, tg, pts[prev], pts[i], horizontal, ctx, label)
            prev = i
    for a, b, e, kind in edges:
        if kind == "A":
            g = e.CenterSketchPoint.Geometry
            ctx.name_parameter(sk.DimensionConstraints.AddRadius(e, tg.CreatePoint2d(g.X + 0.5, g.Y + 0.5)),
                               "%s_R%s" % (prefix, tag))


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


def draw_shape(sk, P, s, arcs=True):
    """Draw a shape. Return items for parametrization: ('circle', c) | ('rect', lines) | ('contour', ents)."""
    shape = s["shape"]
    if shape == "rectangle":
        x, y, w, h = s["x"], s["y"], s["width"], s["height"]
        if s.get("centered"):
            x, y = x - w / 2.0, y - h / 2.0
        r = sk.SketchLines.AddAsTwoPointRectangle(P(x, y), P(x + w, y + h))
        return [("rect", [r.Item(i) for i in range(1, 5)])]
    if shape == "circle":
        return [("circle", sk.SketchCircles.AddByCenterRadius(P(s["x"], s["y"]), s["d"] * MM / 2.0))]
    if shape == "polygon":
        return [("contour", [(line, "L") for line in draw_polygon(sk, P, s["points"])])]
    if shape == "contour":
        repaired = repair_line_contour(s)
        if repaired is not None:
            return [("contour", [(line, "L") for line in draw_polygon(sk, P, pts)]) for pts in repaired]
        if arcs:
            return [("contour", draw_contour(sk, P, s))]
        pts = sp.contour_points(s, 16)  # fallback: arcs approximated by line segments
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
            pts = pts[:-1]
        return [("contour", [(line, "L") for line in draw_polygon(sk, P, pts)])]
    if shape == "slot":
        a1, a2, m2, b2, b1, m1 = sp.slot_points(s)
        l1 = sk.SketchLines.AddByTwoPoints(P(*a1), P(*a2))
        arc1 = sk.SketchArcs.AddByThreePoints(l1.EndSketchPoint, P(*m2), P(*b2))
        l2 = sk.SketchLines.AddByTwoPoints(arc1.EndSketchPoint, P(*b1))
        sk.SketchArcs.AddByThreePoints(l2.EndSketchPoint, P(*m1), l1.StartSketchPoint)
    return []


def draw_sketch(sk, tg, P, sketch, arcs, ctx, prefix):
    """Draw all shapes and (when ``ctx`` is given) add the parametric constraints and dimensions."""
    origin = sk.AddByProjectingEntity(ctx.comp.WorkPoints.Item(1)) if ctx is not None else None
    items = []  # (item, shape dict)
    for s in sp.sketch_shapes(sketch):
        for it in draw_shape(sk, P, s, arcs):
            items.append((it, s))
    if ctx is None:
        return
    try:
        g0 = origin.Geometry
        if abs(g0.X) > 1e-7 or abs(g0.Y) > 1e-7:
            raise RuntimeError("sketch origin is not at (0, 0)")
        patterns = {}
        for i, ((kind, obj), s) in enumerate(items, 1):
            tag = str(i)
            pid, k = s.get("_pattern", (None, 0))
            pattern = None
            if pid is not None:
                pattern = patterns.setdefault(pid, {"dx": s["_dx"], "dy": s["_dy"]})
            if kind == "circle":
                parametrize_circle(sk, tg, origin, ctx, obj, prefix, tag, pattern, k)
            elif kind == "rect":
                parametrize_rectangle(sk, tg, origin, ctx, obj, prefix, tag, pattern, k)
            else:
                parametrize_contour(sk, tg, origin, ctx, obj, prefix, tag)
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


def add_extrusion(comp, tg, f, ctx=None):
    plane = f["plane"]
    n, A, B = sp.PLANE_AXES[plane]
    name = f.get("name", "Feature")
    prefix = _slug(name)[:24]
    wp = offset_plane(comp, tg, plane, f.get("offset", 0), ctx, prefix)

    sk = basis = prof = None
    for arcs in (True, False):
        sk = comp.Sketches.Add(wp)
        basis = sketch_basis(sk, tg)
        P = make_mapper(tg, basis, A, B)
        set_name(sk, "Sketch - " + name)
        draw_sketch(sk, tg, P, f["sketch"], arcs, ctx, prefix)
        try:
            prof = sk.Profiles.AddForSolid()
            break
        except Exception as e:
            sk.Delete()
            if not arcs:
                raise RuntimeError("Could not create the profile of sketch '%s': %s" % (name, e))
            print("  warning: profile with arcs failed in '%s'; redrawing with line segments" % name)

    op = OPS[f.get("operation", "new")]
    sketch_sign = 1 if cross(basis[1], basis[2])[n] > 0 else -1
    directions = [K_POSITIVE, K_NEGATIVE] if f.get("direction", 1) * sketch_sign > 0 else [K_NEGATIVE, K_POSITIVE]

    ext = comp.Features.ExtrudeFeatures
    before = part_volume(comp)
    feat = None
    last_error = None
    for attempt, direction in enumerate(directions):
        try:
            if f["distance"] == sp.THROUGH_ALL:
                d = ext.CreateExtrudeDefinition(prof, op)
                d.SetThroughAllExtent(direction)
                feat = ext.Add(d)
            else:
                feat = ext.AddByDistanceExtent(prof, f["distance"] * MM, direction, op)
        except Exception as e:
            last_error = e
            feat = None
            if attempt == 0:
                print("  warning: extrusion failed in '%s' (%s); trying the opposite direction" % (name, e))
            continue
        after = part_volume(comp)
        if before is None or after is None or abs(after - before) > 1e-9:
            break  # the volume changed: correct direction (or it cannot be checked)
        if attempt == 0:  # nothing changed: try the opposite direction
            try:
                feat.Delete()
            except Exception:
                break
            print("  warning: reversed direction in '%s', trying the opposite" % name)
    if feat is None:
        raise RuntimeError("Extrusion '%s' failed in both directions: %s" % (name, last_error))
    set_name(feat, name)
    if ctx is not None and f["distance"] != sp.THROUGH_ALL:
        try:
            ctx.name_parameter(feat.Definition.Extent.Distance, prefix + "_depth")
        except Exception:
            pass


def add_hole(app, comp, tg, f, ctx=None):
    """Inventor hole feature (simple, countersink or counterbore) at one or more positions."""
    plane = f["plane"]
    n, A, B = sp.PLANE_AXES[plane]
    name = f.get("name", "Hole")
    prefix = _slug(name)[:24]
    wp = offset_plane(comp, tg, plane, f.get("offset", 0), ctx, prefix)
    d, depth = f["d"] * MM, f["depth"] * MM
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
        pts = [sk.SketchPoints.Add(P(x, y), True) for x, y in f["points"]]
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
    for i, f in enumerate(spec["features"], 1):
        print("Feature %d: %s %s" % (i, f["type"], f.get("name", "")))
        if f["type"] == "extrusion":
            if model:
                add_extrusion(comp, tg, f, ctx)
            else:
                add_legacy_extrusion(comp, tg, f)
        elif f["type"] == "hole":
            add_hole(app, comp, tg, f, ctx)
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
