import json
from pathlib import Path

import pytest

from stl_to_ipt import spec as sp

EXAMPLES = sorted((Path(__file__).parent.parent / "examples" / "specs").glob("*.json"))


def _box_feature(**kw):
    f = {"type": "extrusion", "plane": "XY", "operation": "new", "distance": 5,
         "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 10, "height": 20}]}
    f.update(kw)
    return f


def test_valid_spec_has_no_errors():
    errors, warnings = sp.validate({"system": "model", "features": [_box_feature()]})
    assert errors == []


@pytest.mark.parametrize("change,message", [
    ({"plane": "AB"}, "invalid plane"),
    ({"operation": "explode"}, "invalid operation"),
    ({"distance": -1}, "distance must be"),
    ({"distance": "through_all"}, "only valid for cuts"),
    ({"direction": 2}, "direction must be"),
    ({"sketch": []}, "empty sketch"),
])
def test_invalid_extrusion_is_reported(change, message):
    errors, _ = sp.validate({"system": "model", "features": [_box_feature(**change)]})
    assert any(message in e for e in errors), errors


def test_first_feature_cannot_be_a_cut():
    errors, _ = sp.validate({"system": "model", "features": [_box_feature(operation="cut")]})
    assert any("cannot be a cut" in e for e in errors)


def test_hole_validation():
    hole = {"type": "hole", "plane": "XY", "points": [[1, 1]], "d": 3, "depth": 5,
            "subtype": "countersink", "head_d": 2, "angle": 90}
    errors, _ = sp.validate({"system": "model", "features": [_box_feature(), hole]})
    assert any("head_d must be larger" in e for e in errors)
    hole["head_d"] = 7
    errors, _ = sp.validate({"system": "model", "features": [_box_feature(), hole]})
    assert errors == []


def test_linear_pattern_expands_and_links_copies():
    shape = {"shape": "circle", "x": 5, "y": 5, "d": 2,
             "pattern": {"type": "linear", "nx": 3, "ny": 1, "dx": 10, "dy": 0}}
    copies = sp.expand_shape(shape)
    assert [c["x"] for c in copies] == [5, 15, 25]
    assert all("_pattern" in c for c in copies)
    assert len({c["_pattern"][0] for c in copies}) == 1


def test_circular_pattern_keeps_radius():
    shape = {"shape": "circle", "x": 10, "y": 0, "d": 1, "pattern": {"type": "circular", "n": 4}}
    copies = sp.expand_shape(shape)
    assert len(copies) == 4
    for c in copies:
        assert abs((c["x"] ** 2 + c["y"] ** 2) ** 0.5 - 10) < 1e-9


def test_unknown_pattern_raises():
    with pytest.raises(ValueError):
        sp.expand_shape({"shape": "circle", "x": 0, "y": 0, "d": 1, "pattern": {"type": "spiral"}})


def test_parameters_are_resolved():
    spec = sp.normalize({"params": {"T": 3}, "features": [_box_feature(distance="T")]})
    assert spec["features"][0]["distance"] == 3.0


def test_legacy_format_is_converted():
    spec = sp.normalize({"base": {"type": "rectangle", "thickness": 4, "width": 30, "height": 20},
                         "holes": [{"x": 5, "y": 5, "d": 3}]})
    assert [f["operation"] for f in spec["features"]] == ["new", "cut"]
    assert spec["features"][1]["distance"] == sp.THROUGH_ALL


def test_sample_arc_passes_through_given_points():
    pts = sp.sample_arc((10, 0), (0, 10), (-10, 0), 8)
    assert pts[0] == (10, 0)
    assert abs(pts[-1][0] + 10) < 1e-9 and abs(pts[-1][1]) < 1e-9
    assert all(abs((x * x + y * y) ** 0.5 - 10) < 1e-9 for x, y in pts)
    assert any(abs(x) < 1e-9 and abs(y - 10) < 1e-6 for x, y in pts)


def test_sketch_geometry_treats_nested_shapes_as_holes():
    g = sp.sketch_geometry([{"shape": "rectangle", "x": 0, "y": 0, "width": 20, "height": 20},
                            {"shape": "circle", "x": 10, "y": 10, "d": 4}])
    assert len(g.interiors) == 1
    assert g.area < 400


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_example_specs_are_valid(path):
    spec = sp.load(path)
    errors, _ = sp.validate(spec)
    assert errors == []
    assert spec["system"] == "model"
    assert spec["notes"]["iou"] >= 0.98


def test_there_are_example_specs():
    assert len(EXAMPLES) == 20


def test_example_specs_use_english_vocabulary_only():
    banned = {"extrusao", "furo", "esboco", "plano", "corte", "uniao", "passante", "retangulo", "circulo"}
    for path in EXAMPLES:
        text = json.dumps(json.loads(path.read_text(encoding="utf-8")))
        assert not any('"%s"' % word in text for word in banned), path.name
