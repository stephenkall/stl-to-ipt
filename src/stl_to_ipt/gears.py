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
from shapely.geometry import Polygon

STANDARD_MODULES = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
PRESSURE_ANGLES = (20.0, 14.5, 25.0)
ADDENDUM = 1.0
DEDENDUM = 1.25


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
