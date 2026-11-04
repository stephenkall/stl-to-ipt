import math

import pytest

from stl_to_ipt import rounding as rd


@pytest.mark.parametrize("raw,expected", [
    (5.015, 5.0),          # measurement noise around a round value
    (4.99, 5.0),
    (12.46, 12.5),
    (25.859, 25.85),       # no round value nearby: at most two decimals
    (3.18, 3.2),
    (9.0, 9.0),
    (0.002, 0.0),
    (-0.004, 0.0),
    (91.494, 91.5),
    (7.6, 7.6),
])
def test_nice_snaps_noise_to_round_values(raw, expected):
    assert rd.nice(raw) == pytest.approx(expected, abs=1e-9)


def test_nice_never_returns_more_than_two_decimals():
    for v in (1.23456, 87.6543, 0.0049, 33.3333):
        assert round(rd.nice(v), 2) == rd.nice(v)


def test_negative_zero_is_normalized():
    assert str(rd.nice(-0.001)) == "0.0"


def test_cluster_values_makes_equal_coordinates_exactly_equal():
    out = rd.cluster_values([4.994, 5.0, 5.044, 61.0, 60.99, 30.0])
    assert out[0] == out[1] == out[2] == 5.0
    assert out[3] == out[4] == 61.0
    assert out[5] == 30.0


def test_cluster_values_does_not_chain_distant_values():
    out = rd.cluster_values([1.0, 1.05, 1.10, 1.15])
    assert len(set(out)) >= 2  # a chain of small gaps must not collapse into one value


def test_round_spec_rounds_shapes_features_and_holes():
    spec = {"features": [
        {"type": "extrusion", "name": "a", "plane": "XY", "operation": "new", "distance": 5.015, "offset": 0.004,
         "sketch": [{"shape": "circle", "x": 7.498, "y": 7.503, "d": 8.986},
                    {"shape": "rectangle", "x": 1.002, "y": 2.0, "width": 9.99, "height": 4.01,
                     "pattern": {"type": "linear", "nx": 3, "ny": 1, "dx": 12.004, "dy": 0}}]},
        {"type": "extrusion", "name": "b", "plane": "XY", "operation": "cut", "distance": "through_all",
         "sketch": [{"shape": "circle", "x": 1, "y": 1, "d": 2}]},
        {"type": "hole", "name": "h", "plane": "XY", "points": [[10.004, 19.996]], "d": 3.02, "depth": 11.99,
         "offset": 5.0, "direction": -1},
    ]}
    rd.round_spec(spec)
    f0, f1, f2 = spec["features"]
    assert f0["distance"] == 5.0 and f0["offset"] == 0.0
    assert f0["sketch"][0] == {"shape": "circle", "x": 7.5, "y": 7.5, "d": 9.0}
    assert f0["sketch"][1]["pattern"]["dx"] == 12.0 and f0["sketch"][1]["width"] == 10.0
    assert f1["distance"] == "through_all"
    assert f2["points"] == [[10.0, 20.0]] and f2["d"] == 3.0 and f2["depth"] == 12.0
