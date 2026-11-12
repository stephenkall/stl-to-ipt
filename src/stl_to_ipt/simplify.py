"""Design-intent cleanup of fitted contours.

A scanned or exported mesh carries many tiny edge breaks (0.5 mm chamfers, 45-degree transitions).
Fitting them literally gives contours with dozens of short segments, each of which would need its own
dimension in the CAD sketch. A person modelling the same part would draw the simple outline and break the
edges afterwards, so this pass removes the micro chamfers from contours and leaves a clean outline of
long horizontal/vertical lines and arcs.
"""
import math

AXIS_TOL = 1e-3


def _is_horizontal(a, b):
    return abs(a[1] - b[1]) <= AXIS_TOL and abs(a[0] - b[0]) > AXIS_TOL


def _is_vertical(a, b):
    return abs(a[0] - b[0]) <= AXIS_TOL and abs(a[1] - b[1]) > AXIS_TOL


def drop_micro_chamfers(contour, max_leg=0.7):
    """Remove short diagonal edges that join a horizontal and a vertical line.

    ``contour`` is a spec ``contour`` shape (``start`` + ``segments``). Return ``(contour, removed)``;
    ``contour`` is modified in place. Each removed chamfer has legs of at most ``max_leg`` mm.
    """
    segs = contour["segments"]
    ends = [list(map(float, s[-2:])) for s in segs]
    kinds = [s[0] for s in segs]
    edges = [list(s) for s in segs]
    removed = 0
    changed = True
    while changed and len(edges) > 4:
        changed = False
        n = len(edges)
        for k in range(n):
            if kinds[k] != "L":
                continue
            p, q = ends[k - 1], ends[k]
            dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
            if dx <= AXIS_TOL or dy <= AXIS_TOL or dx > max_leg or dy > max_leg:
                continue
            prev_k, next_k = (k - 1) % n, (k + 1) % n
            if kinds[prev_k] != "L" or kinds[next_k] != "L":
                continue
            before, after = ends[(k - 2) % n], ends[next_k]
            if _is_horizontal(before, p) and _is_vertical(q, after):
                corner = [q[0], p[1]]
            elif _is_vertical(before, p) and _is_horizontal(q, after):
                corner = [p[0], q[1]]
            else:
                continue
            ends[prev_k] = corner
            edges[prev_k][-2:] = [corner[0], corner[1]]
            for lst in (ends, kinds, edges):
                del lst[k]
            removed += 1
            changed = True
            break
    if removed:
        contour["start"] = [round(ends[-1][0], 3), round(ends[-1][1], 3)]
        contour["segments"] = [[e[0]] + [round(float(v), 3) for v in e[1:]] for e in edges]
    return contour, removed


def simplify_spec(spec, max_leg=0.7):
    """Apply the cleanup to every contour of every feature sketch. Return the number of removed chamfers."""
    total = 0
    for f in spec.get("features", []):
        sketch = f.get("sketch")
        if not isinstance(sketch, list):
            continue
        for shape in sketch:
            if isinstance(shape, dict) and shape.get("shape") == "contour":
                total += drop_micro_chamfers(shape, max_leg)[1]
    return total
