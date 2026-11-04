import pytest

from stl_to_ipt import spec as sp
from stl_to_ipt import symmetry as sy

EXT = [70.0, 100.0, 60.0]


def _base():
    return {"type": "extrusion", "name": "Base profile", "plane": "YZ", "operation": "new", "offset": 0.0,
            "direction": 1, "distance": 70.0,
            "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 100, "height": 60}]}


def _pad(name, offset):
    return {"type": "extrusion", "name": name, "plane": "YZ", "operation": "join", "offset": offset,
            "direction": 1, "distance": 3.0,
            "sketch": [{"shape": "circle", "x": 20, "y": 30, "d": 10}]}


def test_reflect_extrusion_along_the_mirror_normal_flips_offset_and_direction():
    r = sp.reflect_feature(_pad("p", 0.0), 0, 35.0)
    assert r["offset"] == 70.0 and r["direction"] == -1 and r["distance"] == 3.0


def test_reflect_shapes_across_a_sketch_axis():
    f = {"type": "extrusion", "name": "c", "plane": "XY", "operation": "cut", "offset": 0, "direction": 1,
         "distance": "through_all",
         "sketch": [{"shape": "rectangle", "x": 9, "y": 5, "width": 10, "height": 4},
                    {"shape": "circle", "x": 12, "y": 30, "d": 4},
                    {"shape": "contour", "start": [0, 0], "segments": [["L", 4, 0], ["A", 5, 1, 4, 2], ["L", 0, 2]]}]}
    r = sp.reflect_feature(f, 0, 35.0)  # mirror normal X is the sketch x axis of an XY plane
    assert r["sketch"][0]["x"] == 51 and r["sketch"][1]["x"] == 58
    assert r["sketch"][2]["start"] == [70, 0] and r["sketch"][2]["segments"][1] == ["A", 65, 1, 66, 2]


def test_reflect_twice_returns_the_original():
    f = _pad("p", 3.0)
    back = sp.reflect_feature(sp.reflect_feature(f, 0, 35.0), 0, 35.0)
    assert back["offset"] == pytest.approx(3.0) and back["direction"] == 1


def test_reflect_rejects_unsupported_shapes():
    f = {"type": "extrusion", "name": "c", "plane": "XY", "operation": "cut", "distance": "through_all",
         "sketch": [{"shape": "circle", "x": 1, "y": 1, "d": 2, "pattern": {"type": "circular", "n": 4}}]}
    assert sp.reflect_feature(f, 0, 35.0) is None


def test_mirrored_pads_are_replaced_by_a_mirror_feature():
    feats = [_base(), _pad("Pad 0 to 3", 0.0), _pad("Pad 67 to 70", 67.0)]
    out, saved = sy.mirror_features(feats, 0, 35.0, EXT)
    assert saved == 1
    assert [f["type"] for f in out] == ["extrusion", "extrusion", "mirror"]
    mirror = out[-1]
    assert mirror["plane"] == "YZ" and mirror["offset"] == 35.0 and mirror["features"] == ["Pad 0 to 3"]


def test_expanding_the_mirror_restores_the_dropped_feature():
    feats = [_base(), _pad("Pad 0 to 3", 0.0), _pad("Pad 67 to 70", 67.0)]
    out, _ = sy.mirror_features(feats, 0, 35.0, EXT)
    expanded = sp.expand_mirrors({"system": "model", "features": out})["features"]
    twin = expanded[-1]
    assert twin["offset"] == 70.0 and twin["direction"] == -1  # covers 67..70 like the dropped pad
    assert sp.validate({"system": "model", "features": out})[0] == []


def test_cut_with_mirror_twins_keeps_one_of_each():
    cut = {"type": "extrusion", "name": "Cut", "plane": "XY", "operation": "cut", "offset": 0, "direction": 1,
           "distance": "through_all",
           "sketch": [{"shape": "rectangle", "x": 9, "y": 20, "width": 10, "height": 8},
                      {"shape": "rectangle", "x": 51, "y": 20, "width": 10, "height": 8}]}
    out, saved = sy.mirror_features([_base(), cut], 0, 35.0, EXT)
    assert saved == 1
    assert len(out[1]["sketch"]) == 1 and out[2]["type"] == "mirror"


def test_hole_points_in_twins_are_halved():
    hole = {"type": "hole", "name": "Hole", "subtype": "simple", "plane": "XY", "offset": 10.0, "direction": -1,
            "points": [[46.2, 98.2], [23.8, 98.2]], "d": 3.0, "depth": 7.0}
    out, saved = sy.mirror_features([_base(), hole], 0, 35.0, EXT)
    assert saved == 1 and out[1]["points"] == [[46.2, 98.2]]
    expanded = sp.expand_mirrors({"system": "model", "features": out})["features"][-1]
    assert expanded["points"] == [[pytest.approx(23.8), 98.2]]


def test_join_shapes_are_never_split():
    # nested shapes of an additive sketch are holes: mirroring "contour minus one hole" would fill the other one
    pad = {"type": "extrusion", "name": "Pad", "plane": "XY", "operation": "join", "offset": 0, "direction": 1,
           "distance": 5, "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 70, "height": 20},
                                     {"shape": "circle", "x": 20, "y": 10, "d": 4},
                                     {"shape": "circle", "x": 50, "y": 10, "d": 4}]}
    out, saved = sy.mirror_features([_base(), pad], 0, 35.0, EXT)
    assert saved == 0 and out[1]["sketch"] == pad["sketch"]


def test_self_symmetric_feature_is_left_alone():
    centered = {"type": "extrusion", "name": "Slot", "plane": "XY", "operation": "cut", "offset": 0, "direction": 1,
                "distance": "through_all",
                "sketch": [{"shape": "rectangle", "x": 25, "y": 20, "width": 20, "height": 8}]}
    out, saved = sy.mirror_features([_base(), centered], 0, 35.0, EXT)
    assert saved == 0 and len(out) == 2


def test_unique_names():
    feats = [{"type": "hole", "name": "Blind hole"}, {"type": "hole", "name": "Blind hole"},
             {"type": "extrusion", "name": "Pad"}]
    assert [f["name"] for f in sy.unique_names(feats)] == ["Blind hole", "Blind hole 2", "Pad"]


def test_mirror_feature_validation_requires_earlier_features():
    spec = {"system": "model", "features": [_base(), {"type": "mirror", "name": "M", "plane": "YZ", "offset": 35,
                                                       "features": ["Missing"]}]}
    assert any("unknown feature" in e for e in sp.validate(spec)[0])
