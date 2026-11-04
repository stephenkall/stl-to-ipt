"""Detect standard involute spur gears in a cross-section and describe them by their design parameters.

A gear is recognized when a standard involute tooth form (module ``m``, tooth count ``z``, pressure angle
``alpha``, addendum 1.0 m, dedendum 1.25 m) reproduces the section outline; the result is a ``gear`` feature
(module, teeth, pressure angle, phase, center) instead of hundreds of contour segments.

Geometry (all lengths in mm, angles in degrees unless noted):

* pitch radius  r  = m z / 2,      base radius rb = r cos(alpha)
* tip radius    ra = r + m,        root radius rf = r - 1.25 m
* a flank is the involute of the base circle: polar angle ``inv(a) = tan(a) - a`` at the radius where the
  involute meets the angle ``a = acos(rb / radius)``; the tooth is symmetric about its center line.
"""
import math

import numpy as np
from shapely.geometry import Polygon, box

STANDARD_MODULES = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
PRESSURE_ANGLES = (20.0, 14.5, 25.0)
ADDENDUM = 1.0
DEDENDUM = 1.25
MIN_SECTOR_MODULE = 1.0


def inv(a):
    """Involute function (radians)."""
    return math.tan(a) - a


def radii(m, z, alpha_deg, dedendum=DEDENDUM, addendum=ADDENDUM):
    r = m * z / 2.0
    rb = r * math.cos(math.radians(alpha_deg))
    return {"pitch": r, "base": rb, "tip": r + addendum * m, "root": r - dedendum * m}


def half_angle_at(radius, m, z, alpha_deg):
    """Half angular thickness (radians) of a tooth at ``radius`` (>= base radius)."""
    alpha = math.radians(alpha_deg)
    rb = radii(m, z, alpha_deg)["base"]
    a_r = math.acos(min(1.0, rb / radius))
    return math.pi / (2 * z) + inv(alpha) - inv(a_r)


def tooth_points(m, z, alpha_deg, n_flank=14, dedendum=DEDENDUM, addendum=ADDENDUM):
    """Outline of one tooth centered on +x (angle 0), counter-clockwise, in the gear's own frame: from the root
    circle on the lower side, up the lower flank, across the tip, and down the upper flank."""
    rr = radii(m, z, alpha_deg, dedendum, addendum)
    r_start = max(rr["base"], rr["root"])
    flank_r = np.linspace(r_start, rr["tip"], n_flank)
    lower = [(r * math.cos(-half_angle_at(r, m, z, alpha_deg)), r * math.sin(-half_angle_at(r, m, z, alpha_deg)))
             for r in flank_r]
    upper = [(r * math.cos(half_angle_at(r, m, z, alpha_deg)), r * math.sin(half_angle_at(r, m, z, alpha_deg)))
             for r in flank_r[::-1]]
    pts = []
    if rr["root"] < rr["base"]:  # radial run from the root circle to the base circle
        a0 = -half_angle_at(rr["base"], m, z, alpha_deg)
        pts.append((rr["root"] * math.cos(a0), rr["root"] * math.sin(a0)))
    pts += lower
    ta = half_angle_at(rr["tip"], m, z, alpha_deg)
    pts += [(rr["tip"] * math.cos(a), rr["tip"] * math.sin(a)) for a in np.linspace(-ta, ta, 5)[1:-1]]
    pts += upper
    if rr["root"] < rr["base"]:
        a1 = half_angle_at(rr["base"], m, z, alpha_deg)
        pts.append((rr["root"] * math.cos(a1), rr["root"] * math.sin(a1)))
    return pts


def gear_polygon(m, z, alpha_deg, phase_deg=0.0, center=(0.0, 0.0), n_flank=14, dedendum=DEDENDUM,
                 addendum=ADDENDUM):
    """Outline polygon of the whole gear (teeth joined by root-circle arcs), tooth 0 centered at ``phase_deg``."""
    rr = radii(m, z, alpha_deg, dedendum, addendum)
    base = tooth_points(m, z, alpha_deg, n_flank, dedendum, addendum)
    pts = []
    step = 360.0 / z
    for k in range(z):
        ang = math.radians(phase_deg + k * step)
        c, s = math.cos(ang), math.sin(ang)
        pts += [(center[0] + x * c - y * s, center[1] + x * s + y * c) for x, y in base]
        # root arc to the next tooth
        a_from = math.radians(phase_deg + k * step) + math.atan2(base[-1][1], base[-1][0])
        a_to = math.radians(phase_deg + (k + 1) * step) + math.atan2(base[0][1], base[0][0])
        if a_to < a_from:
            a_to += 2 * math.pi
        for a in np.linspace(a_from, a_to, 4)[1:-1]:
            pts.append((center[0] + rr["root"] * math.cos(a), center[1] + rr["root"] * math.sin(a)))
    poly = Polygon(pts)
    return poly if poly.is_valid else poly.buffer(0)


def _iou(a, b):
    u = a.union(b).area
    return a.intersection(b).area / u if u > 0 else 0.0


def estimate_teeth(outline, center):
    """Number of teeth from the dominant angular frequency of r(theta); None when there is no clear peak."""
    ring = np.asarray(outline.exterior.coords)
    r = np.hypot(ring[:, 0] - center[0], ring[:, 1] - center[1])
    th = np.arctan2(ring[:, 1] - center[1], ring[:, 0] - center[0])
    order = np.argsort(th)
    grid = np.linspace(-np.pi, np.pi, 4096, endpoint=False)
    rg = np.interp(grid, th[order], r[order], period=2 * np.pi)
    spec = np.abs(np.fft.rfft(rg - rg.mean()))
    k = int(np.argmax(spec[3:300])) + 3
    if spec[k] < 4.0 * np.median(spec[3:300]):
        return None
    return k


def _circle_through(p1, p2, p3):
    """Center and radius of the circle through three points; None when they are (nearly) collinear."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _fit_circle(pts):
    """Least-squares circle through points (Kasa fit): center and radius."""
    x, y = pts[:, 0], pts[:, 1]
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    sol = np.linalg.lstsq(a, x * x + y * y, rcond=None)[0]
    return sol[0], sol[1], math.sqrt(max(sol[2] + sol[0] ** 2 + sol[1] ** 2, 0.0))


def tip_circle(outline, tol=0.3, trials=4000):
    """Circle that most vertices of the convex hull lie on: the tip circle of a gear whose teeth stick out of the
    outline. Return (cx, cy, radius, hull vertices on the circle) or None."""
    hv = np.asarray(outline.convex_hull.exterior.coords)[:-1]
    if len(hv) < 8:
        return None
    rng = np.random.default_rng(0)
    best = None
    for _ in range(trials):
        i = rng.choice(len(hv), 3, replace=False)
        c = _circle_through(*hv[i])
        if c is None or not 5.0 < c[2] < 500.0:
            continue
        count = int((np.abs(np.hypot(hv[:, 0] - c[0], hv[:, 1] - c[1]) - c[2]) < tol).sum())
        if best is None or count > best[0]:
            best = (count, c)
    if best is None or best[0] < 8:
        return None
    cx, cy, r = best[1]
    for _ in range(3):  # refine on the inliers
        near = hv[np.abs(np.hypot(hv[:, 0] - cx, hv[:, 1] - cy) - r) < tol]
        if len(near) < 8:
            return None
        cx, cy, r = _fit_circle(near)
    near = hv[np.abs(np.hypot(hv[:, 0] - cx, hv[:, 1] - cy) - r) < tol]
    return cx, cy, r, near


def _plate_shapes(residual, outline):
    """Simple join shapes for the part of the outline that is not gear: the bounding box of all of it when that box
    stays inside the outline, else None (the caller falls back to the exact residual)."""
    if residual.is_empty:
        return []
    env = box(*residual.bounds)
    if env.difference(outline).area <= 0.015 * outline.area:
        return [env]
    return None


def shapely_union(pieces):
    from shapely.ops import unary_union
    return unary_union(pieces) if pieces else Polygon()


def compose_sector(outline, gear, bounds):
    """The sector design: full gear clipped to ``bounds`` plus the plate that is not gear. Return (polygon, plate
    polygons, exact) where ``exact`` tells whether the plate is the raw residual rather than a bounding box."""
    clip = box(*bounds)
    full = gear_polygon(gear["module"], gear["teeth"], gear["pressure_angle"], gear["phase"], tuple(gear["center"]),
                        8, gear["dedendum"], gear["addendum"])
    kept = full.intersection(clip)
    residual = outline.difference(full).buffer(-0.25).buffer(0.25)  # drop slivers along the gear flanks
    residual = residual.intersection(outline)
    # what is left in the gaps (the mesh has rounded roots, the model flat ones) is fitting noise, not a plate
    pieces = [p for p in getattr(residual, "geoms", [residual]) if p.area >= 0.03 * outline.area]
    residual = shapely_union(pieces)
    plate = _plate_shapes(residual, outline)
    exact = plate is None
    if exact:
        plate = list(getattr(residual, "geoms", [residual]))
    union = kept
    for p in plate:
        union = union.union(p)
    return union, plate, exact


def teeth_present(outline, center, tip_angles, m, z, phase_deg):
    """True when the outline really alternates teeth and gaps at the pitch circle over the angular span of the tips.

    A round boss with some tip-like vertices has no gaps; a gear has material at the tooth centers and none halfway
    between them."""
    from shapely.geometry import Point
    ref = np.angle(np.exp(1j * tip_angles).mean())
    ang = ref + (tip_angles - ref + np.pi) % (2 * np.pi) - np.pi
    phase = math.radians(phase_deg)
    idx = np.round((ang - phase) * z / (2 * math.pi)).astype(int)
    k0, k1 = int(idx.min()), int(idx.max())
    r = m * z / 2.0 + 0.4 * m  # above the pitch circle: a tooth is solid there, a gap is empty, a plain boss is not
    step = 2 * math.pi / z
    solid = outline.buffer(0.15)
    shrunk = outline.buffer(-0.15)
    teeth = [solid.contains(Point(center[0] + r * math.cos(phase + k * step),
                                  center[1] + r * math.sin(phase + k * step))) for k in range(k0, k1 + 1)]
    gaps = [not shrunk.contains(Point(center[0] + r * math.cos(phase + (k + 0.5) * step),
                                      center[1] + r * math.sin(phase + (k + 0.5) * step))) for k in range(k0, k1)]
    ok = len(teeth) >= 3 and len(gaps) >= 2 and sum(teeth) >= 0.75 * len(teeth) and sum(gaps) >= 0.75 * len(gaps)
    return (k0, k1) if ok else None


def tooth_zone(center, m, z, phase_deg, k0, k1, addendum=ADDENDUM):
    """Region where the teeth alone define the outline: the ring between just under the pitch circle and the tip
    circle, over the angular span of the inner teeth (the first and last tooth may be cut by a plate)."""
    from shapely.geometry import Point
    lo, hi = (k0 + 1, k1 - 1) if k1 - k0 >= 4 else (k0, k1)
    step = 2 * math.pi / z
    a0 = math.radians(phase_deg) + (lo - 0.5) * step
    a1 = math.radians(phase_deg) + (hi + 0.5) * step
    r_out = m * z / 2.0 + (addendum + 0.5) * m
    arc = [(center[0] + (r_out + 1) * math.cos(a), center[1] + (r_out + 1) * math.sin(a))
           for a in np.linspace(a0, a1, max(8, int((a1 - a0) / 0.05)))]
    wedge = Polygon([tuple(center)] + arc)
    ring = Point(center).buffer(r_out, 64).difference(Point(center).buffer(m * z / 2.0 - 0.2 * m, 64))
    return wedge.intersection(ring)


def _edge_points(geometry, zone, spacing=0.25):
    """Points every ``spacing`` mm along the boundary of ``geometry`` that lies inside ``zone``."""
    lines = geometry.boundary.intersection(zone)
    pts = []
    for line in getattr(lines, "geoms", [lines]):
        if line.geom_type != "LineString" or line.length == 0:
            continue
        for d in np.arange(0.0, line.length, spacing):
            p = line.interpolate(d)
            pts.append((p.x, p.y))
    return pts


def edge_agreement(gear_poly, outline, zone, tol=0.4):
    """Fraction of the edge points of each shape (inside ``zone``) that lie within ``tol`` mm of the other's edge.
    Unlike an area ratio this does not collapse on the thin strip that the teeth occupy."""
    from shapely.geometry import Point
    hits = total = 0
    for source, target in ((gear_poly, outline), (outline, gear_poly)):
        edge = target.boundary
        for x, y in _edge_points(source, zone):
            total += 1
            hits += edge.distance(Point(x, y)) <= tol
    return hits / total if total else 0.0


def fit_sector_gear(outline, min_agreement=0.8):
    """Fit a standard spur gear that is only partly present in the outline (a sector, plus a plate or hub around
    it): the full gear is placed on the section and what is not used is cut away. Return a dict with the gear
    parameters, the clip ``bounds`` (the outline bounding box) and ``iou``, or None.

    The tip circle comes from the hull vertices, the tooth count from the angular coherence of the tips, and the
    module has to be a standard one."""
    outline = Polygon(outline.exterior)
    found = tip_circle(outline)
    if not found:
        return None
    cx, cy, r_tip, tips = found
    theta = np.arctan2(tips[:, 1] - cy, tips[:, 0] - cx)
    candidates = []
    for m in STANDARD_MODULES:
        if m < MIN_SECTOR_MODULE:
            continue  # the teeth would be as small as the measurement noise
        for z in range(max(8, int(2 * r_tip / m) - 4), int(2 * r_tip / m) + 1):
            if abs(m * (z + 2) / 2.0 - r_tip) / r_tip >= 0.04:
                continue
            mean = np.exp(1j * z * theta).mean()
            teeth = len(set(np.round((theta - np.angle(mean) / z) * z / (2 * math.pi)).astype(int)))
            phase0 = math.degrees(np.angle(mean) / z)
            span = teeth_present(outline, (cx, cy), theta, m, z, phase0) if teeth >= 3 else None
            if span:
                candidates.append((abs(mean), m, z, phase0, span))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = None
    for _, m, z, phase0, (k0, k1) in candidates[:6]:
        step = 360.0 / z
        zone = tooth_zone((cx, cy), m, z, phase0, k0, k1)
        ded = _measured_dedendum(outline, (cx, cy), m, z, k0, k1, phase0)
        for alpha in PRESSURE_ANGLES:
            for dphase in (-0.015, 0.0, 0.015):
                phase = phase0 + dphase * step
                g = {"module": m, "teeth": z, "pressure_angle": alpha, "dedendum": ded, "addendum": ADDENDUM,
                     "phase": phase % step, "center": (cx, cy)}
                full = gear_polygon(m, z, alpha, phase, (cx, cy), 8, ded, ADDENDUM)
                score = edge_agreement(full, outline, zone)
                if best is None or score > best[0]:
                    best = (score, g)
    if best is None or best[0] < min_agreement:
        return None
    score, g = best
    # the roots are mostly hidden by the teeth: settle the dedendum on how well the whole design matches
    top = max(((_iou(compose_sector(outline, dict(g, dedendum=d), outline.bounds)[0], outline), d)
               for d in (1.25, 1.35, 1.45, 1.55, g["dedendum"])))
    g["dedendum"] = top[1]
    return dict(g, center=[round(float(cx), 3), round(float(cy), 3)], phase=round(g["phase"], 3),
                agreement=round(score, 3))


def _measured_dedendum(outline, center, m, z, k0, k1, phase_deg):
    """Dedendum (in modules, steps of 0.05) from the deepest point of the outline between the inner teeth."""
    zone = tooth_zone(center, m, z, phase_deg, k0, k1)
    from shapely.geometry import Point
    ring = Point(center).buffer(m * z / 2.0 + 0.5 * m, 64)
    pts = _edge_points(outline.intersection(ring), ring.buffer(0.1), 0.2)
    lo, hi = (k0 + 1, k1 - 1) if k1 - k0 >= 4 else (k0, k1)
    step = 2 * math.pi / z
    a0 = math.radians(phase_deg) + (lo - 0.5) * step
    a1 = math.radians(phase_deg) + (hi + 0.5) * step
    depth = []
    for x, y in pts:
        a = math.atan2(y - center[1], x - center[0])
        a = a0 + (a - a0) % (2 * math.pi)
        if a <= a1:
            depth.append(math.hypot(x - center[0], y - center[1]))
    if not depth:
        return DEDENDUM
    ded = (m * z / 2.0 - min(depth)) / m
    return round(float(min(max(round(ded / 0.05) * 0.05, 1.0), 1.6)), 2)


def fit_full_gear(outline, min_iou=0.985):
    """Fit a complete spur gear to a section outline (a shapely Polygon without holes considered).

    Return a dict with ``module``, ``teeth``, ``pressure_angle``, ``phase``, ``center`` and ``iou``, or None."""
    outline = Polygon(outline.exterior)
    minx, miny, maxx, maxy = outline.bounds
    ring = np.asarray(outline.exterior.coords)
    # the center of a full gear: the center of its tip circle; start from the bounding box and refine
    center = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
    z0 = estimate_teeth(outline, center)
    if not z0 or z0 < 6:
        return None
    r_tip = float(np.hypot(ring[:, 0] - center[0], ring[:, 1] - center[1]).max())
    r_root = float(np.hypot(ring[:, 0] - center[0], ring[:, 1] - center[1]).min())
    best = None
    z = z0
    m_raw = 2 * r_tip / (z + 2)
    # a real gear has teeth: the tip-to-root depth must be at least half of the nominal 2.25 m (= 4.5 / (z + 2) of
    # the tip radius), otherwise this is a plain round part with a ripple on its outline
    if (r_tip - r_root) / r_tip < 0.5 * 4.5 / (z + 2):
        return None
    candidates = [m for m in STANDARD_MODULES if abs(m - m_raw) / m_raw < 0.04]  # only standard modules
    for m in candidates:
        measured_ded = (m * z / 2.0 - r_root) / m
        if not 0.9 <= measured_ded <= 1.5:
            continue
        deds = sorted({DEDENDUM, round(round(measured_ded / 0.05) * 0.05, 2)})
        for alpha in PRESSURE_ANGLES:
            for ded in deds:
                for phase in np.arange(0.0, 360.0 / z, 360.0 / z / 24.0):
                    score = _iou(gear_polygon(m, z, alpha, phase, center, 8, ded), outline)
                    if best is None or score > best[0]:
                        best = (score, m, z, alpha, float(phase), ded)
    if best is None:
        return None
    score, m, z, alpha, phase, ded = best
    step = 360.0 / z / 24.0
    for _ in range(3):  # refine the phase
        step /= 4.0
        for ph in (phase - 2 * step, phase - step, phase + step, phase + 2 * step):
            s_ = _iou(gear_polygon(m, z, alpha, ph, center, 8, ded), outline)
            if s_ > score:
                score, phase = s_, ph
    if score < min_iou:
        return None
    return {"module": m, "teeth": int(z), "pressure_angle": alpha, "dedendum": ded, "addendum": ADDENDUM,
            "phase": round(phase % (360.0 / z), 3), "center": [round(center[0], 3), round(center[1], 3)],
            "iou": round(score, 4)}
