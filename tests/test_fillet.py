import numpy as np
import pytest

from stl_to_ipt import converter as cv
from stl_to_ipt import inventor as inv
from stl_to_ipt import spec as sp

BASE = {"type": "extrusion", "name": "Base profile", "plane": "XY", "operation": "new", "offset": 0.0,
        "direction": 1, "distance": 9.0,
        "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 40, "height": 100},
                   {"shape": "circle", "x": 5, "y": 50, "d": 3}]}  # a hole: it must not be filleted
FILLET = {"type": "fillet", "name": "Edge fillet", "selection": "base_start_outline", "radius": 8.0}


def _spec(*features):
    return {"system": "model", "features": [BASE] + list(features)}


def test_inset_follows_the_rolling_ball():
    assert sp.fillet_inset(8.0, 0.0) == pytest.approx(8.0)
    assert sp.fillet_inset(8.0, 8.0) == pytest.approx(0.0)
    assert sp.fillet_inset(8.0, 4.0) == pytest.approx(8.0 - np.sqrt(64 - 16))
    assert sp.fillet_inset(8.0, 9.0) == 0.0  # beyond the fillet


def test_fillet_removes_the_bottom_edge_only():
    pts = np.array([[0.5, 50.0, 0.2],    # at the outline, just above the start cap: removed
                    [0.5, 50.0, 8.5],    # same place high up: kept
                    [20.0, 50.0, 0.2],   # far from the outline: kept
                    [5.0, 50.0, 0.2]])   # 5 mm from the outline is still inside the 8 mm band: removed
    removed = sp.edge_fillet_removed(FILLET, BASE, pts[:, 0], pts[:, 1], pts[:, 2])
    assert removed.tolist() == [True, False, False, True]


def test_end_cap_fillet_measures_from_the_other_end():
    top = dict(FILLET, selection="base_end_outline")
    pts = np.array([[0.5, 50.0, 8.8], [0.5, 50.0, 0.2]])
    assert sp.edge_fillet_removed(top, BASE, pts[:, 0], pts[:, 1], pts[:, 2]).tolist() == [True, False]


def test_model_evaluation_applies_the_fillet_before_later_features():
    pts = np.array([[0.5, 50.0, 0.2], [20.0, 50.0, 4.0]])
    inside = cv.contains(_spec(FILLET), [40.0, 100.0, 9.0], pts)
    assert inside.tolist() == [False, True]
    assert cv.contains(_spec(), [40.0, 100.0, 9.0], pts).tolist() == [True, True]


def test_fillet_validation():
    assert sp.validate(_spec(FILLET))[0] == []
    late = _spec({"type": "extrusion", "name": "Pad", "plane": "XY", "operation": "join", "distance": 1,
                  "sketch": [{"shape": "circle", "x": 20, "y": 20, "d": 4}]}, FILLET)
    assert any("right after the base extrusion" in e for e in sp.validate(late)[0])
    assert any("larger than the base" in e for e in sp.validate(_spec(dict(FILLET, radius=12.0)))[0])


def test_radius_is_recovered_from_the_insets():
    hs = np.linspace(0.1, 12.0, 30)
    assert cv._fit_fillet_radius(hs, list(sp.fillet_inset(6.0, hs)), 12.0) == pytest.approx(6.0)


def test_chamfers_and_flat_edges_are_not_fillets():
    hs = np.linspace(0.1, 12.0, 30)
    assert cv._fit_fillet_radius(hs, [max(3.0 - h, 0.0) for h in hs], 12.0) is None  # 45 degree chamfer
    assert cv._fit_fillet_radius(hs, [0.0] * len(hs), 12.0) is None


class FakeLoop:
    def __init__(self, outer, edges):
        self.IsOuterEdgeLoop = outer
        self.Edges = edges


class FakeFace:
    def __init__(self, z, area, loops, normal=(0.0, 0.0, -1.0)):
        class Plane:
            Normal = type("N", (), dict(X=normal[0], Y=normal[1], Z=normal[2]))
            RootPoint = type("P", (), dict(X=0.0, Y=0.0, Z=z))
        self.Geometry = Plane
        self.Evaluator = type("E", (), {"Area": area})
        self.EdgeLoops = loops


class FakeEdges(list):
    pass


def test_only_the_outer_loop_of_the_cap_is_selected():
    cap = FakeFace(0.0, 40.0, [FakeLoop(True, ["a", "b", "c", "d"]), FakeLoop(False, ["hole"])])
    small = FakeFace(0.0, 1.0, [FakeLoop(True, ["x"])])          # a smaller planar face on the same plane
    top = FakeFace(0.9, 40.0, [FakeLoop(True, ["t"])], (0.0, 0.0, 1.0))
    comp = type("C", (), {"SurfaceBodies": type("S", (), {"Item": staticmethod(
        lambda i: type("B", (), {"Faces": [small, top, cap]}))})})
    added = []
    app = type("A", (), {"TransientObjects": type("T", (), {"CreateEdgeCollection": staticmethod(
        lambda: type("Col", (), {"Add": lambda self, e: added.append(e)})())})})
    col, count = inv.base_outline_edges(app, comp, BASE, "base_start_outline")
    assert count == 4 and added == ["a", "b", "c", "d"]
    added.clear()
    col, count = inv.base_outline_edges(app, comp, BASE, "base_end_outline")
    assert added == ["t"]
