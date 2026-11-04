import json

import pytest

from stl_to_ipt import fits


def _part(name, feature, origin=(0.0, 0.0, 0.0), raw=None):
    spec = {"system": "model", "features": [feature],
            "notes": {"part": name, "original_min": list(origin),
                      "raw_diameters": raw if raw is not None else fits.raw_diameters({"features": [feature]})}}
    return spec


def _pin(d, d_raw=None):
    f = {"type": "extrusion", "name": "Pin", "plane": "YZ", "operation": "new", "offset": 0.0, "direction": 1,
         "distance": 40.0, "sketch": [{"shape": "circle", "x": 10.0, "y": 10.0, "d": d}]}
    spec = _part("Pin", f, origin=(0, -10, -10), raw=[{"feature": 0, "shape": 0, "d": d_raw or d}])
    return spec


def _block(d, d_raw=None, origin=(10, -10.5, -10.0)):
    f = {"type": "extrusion", "name": "Block", "plane": "YZ", "operation": "new", "offset": 0.0, "direction": 1,
         "distance": 20.0,
         "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 21, "height": 20},
                    {"shape": "circle", "x": 10.5, "y": 10.0, "d": d}]}
    return _part("Block", f, origin=origin, raw=[{"feature": 0, "shape": 1, "d": d_raw or d}])


def _write(tmp_path, *specs):
    for s in specs:
        (tmp_path / (s["notes"]["part"] + ".json")).write_text(json.dumps(s), encoding="utf-8")


def test_raw_diameters_lists_circles_and_holes():
    spec = {"features": [{"type": "extrusion", "sketch": [{"shape": "rectangle"}, {"shape": "circle", "d": 3}]},
                         {"type": "hole", "d": 2.5}]}
    assert fits.raw_diameters(spec) == [{"feature": 0, "shape": 1, "d": 3}, {"feature": 1, "shape": None, "d": 2.5}]


def test_pin_in_hole_is_detected_as_a_fit(tmp_path):
    _write(tmp_path, _pin(9.0, 8.97), _block(9.2, 9.18))
    report = fits.analyze_directory(str(tmp_path))
    assert len(report) == 1
    shaft, hole, raw, rounded, verdict, d_s, d_h = report[0]
    assert (shaft, hole) == ("Pin", "Block")
    assert raw == pytest.approx(0.21, abs=0.005) and rounded == pytest.approx(0.2, abs=0.005) and verdict == "ok"


def test_rounding_that_changes_the_clearance_is_reported_and_repaired(tmp_path):
    # measured: shaft 8.97, hole 9.19 (clearance 0.22); rounded to 9.0 / 9.0 the fit would be closed
    _write(tmp_path, _pin(9.0, 8.97), _block(9.0, 9.19))
    report = fits.analyze_directory(str(tmp_path), apply=True)
    assert report[0][4] == "closed"
    repaired = json.loads((tmp_path / "Block.json").read_text(encoding="utf-8"))
    assert repaired["features"][0]["sketch"][1]["d"] == pytest.approx(9.2)  # 9.0 + 0.22 -> 9.2 (0.05 steps)
    assert fits.analyze_directory(str(tmp_path))[0][4] == "ok"


def test_parts_that_do_not_share_an_axis_are_not_paired(tmp_path):
    _write(tmp_path, _pin(9.0), _block(9.2, origin=(10, 20.0, -10.0)))  # the hole is 30 mm away
    assert fits.analyze_directory(str(tmp_path)) == []


def test_oversized_holes_are_not_fits(tmp_path):
    _write(tmp_path, _pin(9.0), _block(12.0))  # 3 mm of play: not a mating hole
    assert fits.analyze_directory(str(tmp_path)) == []


def test_rotated_parts_are_skipped(tmp_path):
    rotated = _block(9.2)
    rotated["notes"]["rotation_applied_before_slicing"] = {"axis": "X", "degrees": 20}
    _write(tmp_path, _pin(9.0), rotated)
    assert fits.analyze_directory(str(tmp_path)) == []


def test_fixed_hole_diameter_uses_steps_of_five_hundredths():
    pair = {"shaft": {"d": 9.0}, "raw": 0.23}
    assert fits.fixed_hole_diameter(pair) == pytest.approx(9.25)
