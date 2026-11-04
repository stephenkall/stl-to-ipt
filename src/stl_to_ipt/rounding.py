"""Turn measurement noise into the numbers a person would have typed.

A mesh gives values such as 5.015 or 25.859, but the part was designed with a caliper in mind: dimensions are
mostly round (5, 12.5, 3.2) and never carry more than two decimals. ``nice`` snaps a value to the roundest
candidate that is still within measurement noise of it:

    multiples of 0.5 within 0.04  ->  multiples of 0.1 within 0.025  ->  multiples of 0.05 within 0.012
    otherwise the value rounded to two decimals

Dimensions that decide how parts fit together can use ``fit_nice`` (see ``fits``), which never moves a value
by more than half a tolerance step.
"""

NICE_STEPS = ((0.5, 0.04), (0.1, 0.025), (0.05, 0.012))


def nice(v):
    """Round ``v`` (mm) to a clean value; see the module documentation."""
    for step, tol in NICE_STEPS:
        r = round(v / step) * step
        if abs(v - r) <= tol:
            return round(r, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    return round(v, 2) + 0.0


def cluster_values(values, tol=0.06):
    """Group values that lie within ``tol`` of the first member of their group; return, for every input value,
    the clean value (``nice`` of the group mean). Equal coordinates measured with noise become exactly equal."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [None] * len(values)
    group = []
    for i in order:
        if group and values[i] - values[group[0]] > tol:
            _close_group(group, values, result)
            group = []
        group.append(i)
    if group:
        _close_group(group, values, result)
    return result


def _close_group(group, values, result):
    mean = sum(values[i] for i in group) / len(group)
    v = nice(mean)
    for i in group:
        result[i] = v


# --------------------------------------------------------------------------- spec level
_SCALARS = ("offset", "distance", "x", "y", "d", "width", "height", "x1", "y1", "x2", "y2", "depth", "head_d",
            "head_depth")


def round_spec(spec):
    """Apply ``nice`` to the plain numeric fields of every feature and shape (contours are regularized by the
    converter while they are fitted). The spec is modified in place and returned."""
    for f in spec["features"]:
        _round_fields(f)
        if f["type"] == "hole":
            f["points"] = [[nice(x), nice(y)] for x, y in f["points"]]
            if f.get("pattern"):
                f["pattern"]["cx"], f["pattern"]["cy"] = nice(f["pattern"]["cx"]), nice(f["pattern"]["cy"])
        if f["type"] == "gear":
            f["center"] = [nice(v) for v in f["center"]]
            f["phase"] = round(f["phase"], 2)
        for s in f.get("sketch", []) or []:
            _round_fields(s)
            if "pattern" in s:
                for k in ("dx", "dy", "cx", "cy"):
                    if k in s["pattern"]:
                        s["pattern"][k] = nice(s["pattern"][k])
            if s.get("shape") == "polygon":
                s["points"] = [[nice(x), nice(y)] for x, y in s["points"]]
    return spec


def _round_fields(d):
    for k in _SCALARS:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            d[k] = nice(float(v))
