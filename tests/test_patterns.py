import math

import pytest

from stl_to_ipt import converter as cv
from stl_to_ipt import inventor as inv
from stl_to_ipt import patterns as pt


def _ring(n, r=12.0, cx=50.0, cy=30.0, start=15.0):
    return [(cx + r * math.cos(math.radians(start + k * 360.0 / n)),
             cy + r * math.sin(math.radians(start + k * 360.0 / n))) for k in range(n)]


def test_full_ring_is_detected():
    p = pt.circular_pattern(_ring(8))
    assert p["type"] == "circular" and p["n"] == 8
    assert p["cx"] == pytest.approx(50.0, abs=0.01) and p["cy"] == pytest.approx(30.0, abs=0.01)


def test_unequal_spacing_is_not_a_pattern():
    ring = _ring(8)
    ring[3] = (ring[3][0] + 1.5, ring[3][1])
    assert pt.circular_pattern(ring) is None


def test_partial_arc_is_not_a_full_pattern():
    arc = [(50 + 12 * math.cos(math.radians(a)), 30 + 12 * math.sin(math.radians(a))) for a in (0, 20, 40, 60)]
    assert pt.circular_pattern(arc) is None


def test_points_on_a_line_or_too_few_are_not_patterns():
    assert pt.circular_pattern([(0, 0), (10, 0), (20, 0), (30, 0)]) is None
    assert pt.circular_pattern(_ring(8)[:2]) is None


def test_circle_ring_groups_equal_circles_only():
    ring = [{"shape": "circle", "x": x, "y": y, "d": 3.0} for x, y in _ring(6)]
    other = [{"shape": "circle", "x": 5, "y": 5, "d": 9.0}, {"shape": "rectangle", "x": 0, "y": 0, "width": 4, "height": 4}]
    groups = pt.circle_ring(other + ring)
    assert len(groups) == 1 and sorted(groups[0][0]) == [2, 3, 4, 5, 6, 7]


def test_converter_turns_a_ring_of_circles_into_one_patterned_circle():
    shapes = [{"shape": "circle", "x": x, "y": y, "d": 3.0} for x, y in _ring(6)]
    out = cv._circle_rings(shapes)
    assert len(out) == 1
    assert out[0]["pattern"]["type"] == "circular" and out[0]["pattern"]["n"] == 6


def test_hole_features_annotate_a_ring_of_holes():
    holes = [dict(x=x, y=y, d=3.0, face=5.0, direction=-1, depth=4.0, subtype="simple", idx=(0, 1))
             for x, y in _ring(8)]
    feat = cv.hole_features(holes, "XY")[0]
    assert feat["pattern"]["n"] == 8 and len(feat["points"]) == 8


def test_builder_only_patterns_what_is_worth_it():
    rect_row = {"shape": "rectangle", "x": 0, "y": 0, "width": 2, "height": 1,
                "pattern": {"type": "linear", "nx": 39, "ny": 1, "dx": 4.0, "dy": 0}}
    assert inv._linear_pattern_1d(rect_row) == (39, 4.0, True)
    short = dict(rect_row, pattern={"type": "linear", "nx": 2, "ny": 1, "dx": 4.0, "dy": 0})
    assert inv._linear_pattern_1d(short) is None  # two copies: drawn individually
    column = dict(rect_row, pattern={"type": "linear", "nx": 1, "ny": 5, "dx": 0, "dy": 3.0})
    assert inv._linear_pattern_1d(column) == (5, 3.0, False)
    grid = dict(rect_row, pattern={"type": "linear", "nx": 3, "ny": 3, "dx": 4.0, "dy": 3.0})
    assert inv._linear_pattern_1d(grid) is None
    ring = {"shape": "circle", "x": 60, "y": 40, "d": 6, "pattern": {"type": "circular", "n": 6, "cx": 40, "cy": 40}}
    assert inv._circular_pattern_info(ring) == (6, 40, 40)
    assert inv._circular_pattern_info({"shape": "circle", "x": 1, "y": 1, "d": 2}) is None
