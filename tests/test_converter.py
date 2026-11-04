import json
import math

import numpy as np
import pytest
from shapely.geometry import Point, box

trimesh = pytest.importorskip("trimesh")

from stl_to_ipt import converter as cv  # noqa: E402
from stl_to_ipt import spec as sp  # noqa: E402


def _save(mesh, tmp_path, name):
    path = tmp_path / (name + ".stl")
    mesh.export(path)
    return str(path)


def test_box_becomes_a_single_extruded_rectangle(tmp_path):
    stl = _save(trimesh.creation.box(extents=(40, 30, 5)), tmp_path, "plate")
    m, ext, spec = cv.convert(stl)
    assert len(spec["features"]) == 1
    f = spec["features"][0]
    assert f["plane"] == "XY" and f["operation"] == "new" and f["distance"] == pytest.approx(5)
    assert f["sketch"][0]["shape"] == "rectangle"
    assert f["sketch"][0]["width"] == pytest.approx(40) and f["sketch"][0]["height"] == pytest.approx(30)
    assert cv.iou(m, spec, ext, 6000) > 0.98


def test_cylinder_becomes_a_circle_sketch(tmp_path):
    stl = _save(trimesh.creation.cylinder(radius=5, height=20, sections=64), tmp_path, "pin")
    _, _, spec = cv.convert(stl)
    f = spec["features"][0]
    assert f["sketch"][0]["shape"] == "circle"
    assert f["sketch"][0]["d"] == pytest.approx(10, abs=0.2)


def test_convert_file_writes_spec_json(tmp_path):
    stl = _save(trimesh.creation.box(extents=(20, 20, 10)), tmp_path, "cube")
    path, spec = cv.convert_file(stl, str(tmp_path / "out"), n_points=4000)
    written = json.loads(open(path, encoding="utf-8").read())
    assert written["notes"]["part"] == "cube"
    assert written["notes"]["iou"] > 0.98
    assert sp.validate(sp.normalize(written))[0] == []


def test_detects_countersunk_hole_in_slices():
    step = 0.5
    n = 20
    pos = [step * (i + 0.5) for i in range(n)]
    plate = box(0, 0, 40, 40)
    regs = []
    for i in range(n):
        # 90-degree countersink: the diameter shrinks by 2 per unit of depth until it reaches 3 mm
        d = max(3.0, 8.0 - 2 * pos[i])
        regs.append(plate.difference(Point(20, 20).buffer(d / 2.0, 32)))
    vertex_levels = np.array([0.0, n * step])
    holes = cv.detect_holes(pos, regs, step, vertex_levels)
    assert len(holes) == 1
    h = holes[0]
    assert h["subtype"] == "countersink"
    assert h["d"] == pytest.approx(3.0, abs=0.2)
    assert h["angle"] == pytest.approx(90, abs=1)
    assert (h["x"], h["y"]) == (pytest.approx(20, abs=0.1), pytest.approx(20, abs=0.1))


def test_plain_hole_is_not_a_countersink():
    step = 0.5
    pos = [step * (i + 0.5) for i in range(12)]
    regs = [box(0, 0, 40, 40).difference(Point(20, 20).buffer(1.5, 32)) for _ in pos]
    assert cv.detect_holes(pos, regs, step, np.array([0.0, 6.0])) == []


def test_arc_fitting_replaces_tessellated_circle_run():
    angles = np.linspace(0, math.pi / 2, 12)
    run = np.c_[10 * np.cos(angles), 10 * np.sin(angles)]
    segs = []
    cv.fit_run(run, 0.12, segs)
    assert len(segs) == 1 and segs[0][0] == "A"


def test_consecutive_arcs_on_the_same_circle_are_merged():
    c = (0.0, 0.0)
    r = 10.0
    pts = [(r * math.cos(a), r * math.sin(a)) for a in (0, math.pi / 4, math.pi / 2)]
    mids = [(r * math.cos(a), r * math.sin(a)) for a in (math.pi / 8, 3 * math.pi / 8)]
    segs = [("A", mids[0][0], mids[0][1], pts[1][0], pts[1][1]),
            ("A", mids[1][0], mids[1][1], pts[2][0], pts[2][1]),
            ("L", 0.0, 0.0)]
    p0, merged = cv._merge_arcs_and_tangents(pts[0], segs)
    assert [s[0] for s in merged] == ["A", "L"]
    assert c == (0.0, 0.0)


def test_rectangles_are_recognized_and_rows_become_patterns():
    squares = [{"shape": "contour", "start": [x, 0.0],
                "segments": [["L", x + 2, 0.0], ["L", x + 2, 2.0], ["L", x, 2.0], ["L", x, 0.0]]}
               for x in (0.0, 5.0, 10.0)]
    out = cv.compact(squares)
    assert len(out) == 1
    assert out[0]["shape"] == "rectangle"
    assert out[0]["pattern"] == {"type": "linear", "nx": 3, "ny": 1, "dx": 5.0, "dy": 0}


def _plate_with_hole(has_hole):
    plate = box(0, 0, 40, 40)
    return plate.difference(Point(20, 20).buffer(1.5, 32)) if has_hole else plate


def test_blind_hole_across_the_extrusion_axis_is_found():
    step = 0.5
    pos = [step * (i + 0.5) for i in range(12)]
    # hole open at the low end (slices 0-5), solid bottom afterwards (slices 6-11)
    regs = [_plate_with_hole(i < 6) for i in range(12)]
    holes = cv._blind_holes_from_slices(pos, regs, step, box(0, 0, 40, 40), np.array([0.0, 3.0, 6.0]),
                                        lambda center, r, high: 0.0 if not high else 6.0)
    assert len(holes) == 1
    h = holes[0]
    assert h["subtype"] == "simple" and h["direction"] == 1
    assert h["d"] == pytest.approx(3.0, abs=0.1)
    assert h["face"] == pytest.approx(0.0) and h["depth"] == pytest.approx(3.0, abs=0.01)


def test_through_hole_is_not_reported_as_blind():
    step = 0.5
    pos = [step * (i + 0.5) for i in range(12)]
    regs = [_plate_with_hole(True) for _ in range(12)]
    # the hole is a void in the silhouette, so the silhouette cut handles it
    silhouette = box(0, 0, 40, 40).difference(Point(20, 20).buffer(1.5, 32))
    assert cv._blind_holes_from_slices(pos, regs, step, silhouette, np.array([0.0, 6.0]),
                                       lambda c, r, h: 0.0) == []


def test_hole_closed_on_both_ends_is_ignored():
    step = 0.5
    pos = [step * (i + 0.5) for i in range(12)]
    regs = [_plate_with_hole(3 <= i < 8) for i in range(12)]  # internal cavity
    assert cv._blind_holes_from_slices(pos, regs, step, box(0, 0, 40, 40), np.array([0.0, 6.0]),
                                       lambda c, r, h: 0.0) == []


def test_hole_features_group_identical_holes():
    holes = [dict(x=1, y=2, d=3.0, face=5.0, direction=-1, depth=4.0, subtype="simple", idx=(0, 3)),
             dict(x=9, y=2, d=3.0, face=5.0, direction=-1, depth=4.0, subtype="simple", idx=(0, 3))]
    feats = cv.hole_features(holes, "XZ")
    assert len(feats) == 1
    assert feats[0]["points"] == [[1, 2], [9, 2]] and feats[0]["name"] == "Blind hole"
    assert sp.validate({"system": "model", "features": [
        {"type": "extrusion", "plane": "XY", "operation": "new", "distance": 5,
         "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 20, "height": 20}]}] + feats})[0] == []


def test_regularize_aligns_noisy_tangent_points_with_circle_centers():
    # a bar whose left end is a semicircle of radius 5 centered at (5, 5), measured with noise
    p0 = (4.993, -0.004)
    segs = [("L", 30.0, 0.0), ("L", 30.0, 10.0), ("L", 5.044, 10.0), ("A", 0.002, 5.001, 4.993, -0.004)]
    start, out = cv._regularize(p0, segs)
    assert tuple(start) == (5.0, 0.0)
    assert out[2][1:3] == (5.0, 10.0)                  # 5.044 collapsed onto the circle center's x
    assert out[3][3:5] == (5.0, 0.0)
    arc = out[3]
    # the recomputed mid point lies on the clean circle (center (5, 5), radius 5)
    assert math.hypot(arc[1] - 5.0, arc[2] - 5.0) == pytest.approx(5.0, abs=1e-6)
    assert arc[1] < 1.0  # on the far (left) side of the semicircle


def test_tangent_junctions_detects_line_arc_and_arc_arc_continuity():
    from stl_to_ipt import inventor as inv
    # stadium: two lines joined by two semicircles, every junction is tangent
    c = {"shape": "contour", "start": [5, 0],
         "segments": [["L", 20, 0], ["A", 25, 5, 20, 10], ["L", 5, 10], ["A", 0, 5, 5, 0]]}
    assert inv.tangent_junctions(c) == [0, 1, 2, 3]
    # a corner is not tangent
    sq = {"shape": "contour", "start": [0, 0],
          "segments": [["L", 10, 0], ["L", 10, 10], ["L", 0, 10], ["L", 0, 0]]}
    assert inv.tangent_junctions(sq) == []
    # a line meeting an arc at an angle is not tangent
    bent = {"shape": "contour", "start": [0, 0],
            "segments": [["L", 10, 0], ["A", 12, 4, 10, 8], ["L", 0, 8], ["L", 0, 0]]}
    assert 1 not in inv.tangent_junctions(bent)  # the arc meets the line at (10, 8) at an angle
