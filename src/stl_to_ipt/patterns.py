"""Detect circular patterns: points (hole centers, circle centers) equally spaced on a circle.

A ring of screw holes around a boss is drawn once and patterned around the boss axis by a person, not placed one
by one. ``circular_pattern`` recognizes that arrangement so that the spec can say so.
"""
import math

import numpy as np


def _fit_circle(pts):
    p = np.asarray(pts, float)
    A = np.c_[2 * p[:, 0], 2 * p[:, 1], np.ones(len(p))]
    b = (p ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0], sol[1]
    return float(cx), float(cy), math.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0))


def circular_pattern(points, radius_tol=0.2, angle_tol=1.5, min_count=3):
    """If ``points`` lie on one circle with equal angular spacing covering the full circle, return
    ``{"type": "circular", "n": N, "cx": ..., "cy": ...}``; otherwise None."""
    n = len(points)
    if n < min_count:
        return None
    cx, cy, r = _fit_circle(points)
    if not 0.5 < r < 5000:
        return None
    pts = np.asarray(points, float)
    if np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r).max() > radius_tol:
        return None
    angles = sorted(math.degrees(math.atan2(y - cy, x - cx)) % 360.0 for x, y in points)
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 360.0 - angles[-1]]
    step = 360.0 / n
    if any(abs(g - step) > angle_tol for g in gaps):
        return None
    return {"type": "circular", "n": n, "cx": round(cx, 3), "cy": round(cy, 3)}


def circle_ring(shapes, diameter_tol=0.05):
    """Among circle shapes (dicts with x, y, d) find groups of >= 3 equal circles in a circular pattern. Return a
    list of (indices, pattern dict)."""
    found, used = [], set()
    by_d = {}
    for i, s in enumerate(shapes):
        if s.get("shape") == "circle" and "pattern" not in s:
            by_d.setdefault(round(s["d"] / max(diameter_tol, 1e-6)), []).append(i)
    for idx in by_d.values():
        if len(idx) < 3:
            continue
        pat = circular_pattern([(shapes[i]["x"], shapes[i]["y"]) for i in idx])
        if pat and not used.intersection(idx):
            found.append((idx, pat))
            used.update(idx)
    return found
