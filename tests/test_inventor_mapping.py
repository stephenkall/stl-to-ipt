"""Plane-mapping logic of ``inventor`` against a FAKE Inventor API (no Inventor needed).

For every axis-aligned sketch orientation (including mirrored or swapped u/v) P(a, b) must land on the
requested model coordinates and the sketch normal must be along the plane normal.
"""
import itertools

from stl_to_ipt import inventor as inv
from stl_to_ipt import spec as sp


class Pt:
    def __init__(self, x=0, y=0, z=0):
        self.X, self.Y, self.Z = x, y, z


class FakeTransientGeometry:
    def CreatePoint2d(self, x, y):
        return (x, y)


class FakeSketch:
    """Sketch whose u, v axes point to the given model vectors; origin at ``o`` (cm)."""

    def __init__(self, o, eu, ev):
        self.o, self.eu, self.ev = o, eu, ev

    def SketchToModelSpace(self, p2):
        s, t = p2
        return Pt(*[self.o[i] + s * self.eu[i] + t * self.ev[i] for i in range(3)])


def _unit_axes():
    for v in [(1, 0, 0), (0, 1, 0), (0, 0, 1)]:
        yield v
        yield tuple(-c for c in v)


def test_mapper_matches_model_coordinates_for_all_orientations():
    tg = FakeTransientGeometry()
    checked = 0
    for plane, (n, A, B) in sp.PLANE_AXES.items():
        for eu, ev in itertools.permutations(list(_unit_axes()), 2):
            # only orientations where u and v lie in the plane (perpendicular to the normal n)
            if eu[n] != 0 or ev[n] != 0 or sum(abs(a * b) for a, b in zip(eu, ev)) != 0:
                continue
            for normal_offset in (0.0, 2.5):
                o = [0.0, 0.0, 0.0]
                o[n] = normal_offset
                sk = FakeSketch(o, eu, ev)
                basis = inv.sketch_basis(sk, tg)
                P = inv.make_mapper(tg, basis, A, B)
                for a, b in ((0, 0), (12.5, 3.0), (-4.0, 7.25), (30.0, 40.0)):
                    m = sk.SketchToModelSpace(P(a, b))
                    got = (m.X, m.Y, m.Z)
                    assert abs(got[A] - a * inv.MM) < 1e-9, (plane, eu, ev, a, b, got)
                    assert abs(got[B] - b * inv.MM) < 1e-9, (plane, eu, ev, a, b, got)
                    assert abs(got[n] - normal_offset) < 1e-9
                normal = inv.cross(basis[1], basis[2])
                assert abs(abs(normal[n]) - 1) < 1e-9
                checked += 1
    assert checked > 0


def test_degenerate_plane_is_rejected():
    tg = FakeTransientGeometry()
    sk = FakeSketch([0, 0, 0], (0, 0, 1), (0, 0, 1))
    basis = inv.sketch_basis(sk, tg)
    try:
        inv.make_mapper(tg, basis, 0, 1)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError")


# --- regression: a valid contour with arcs must NOT be redrawn as dozens of line segments -------------
POCKET = {"shape": "contour", "start": [41.199, 10.0],
          "segments": [["L", 122.5, 10.0], ["A", 119.991, 22.5, 122.5, 35.0],
                       ["L", 41.199, 35.0], ["A", 44.991, 22.5, 41.199, 10.0]]}


def test_valid_arc_contour_is_not_repaired():
    assert inv.repair_line_contour(POCKET) is None


def test_self_touching_contour_is_repaired():
    bow_tie = {"shape": "contour", "start": [0, 0],
               "segments": [["L", 10, 10], ["L", 10, 0], ["L", 0, 10]]}
    repaired = inv.repair_line_contour(bow_tie)
    assert repaired and all(len(poly) >= 3 for poly in repaired)


def test_slug_is_ascii_and_safe():
    assert inv._slug("Recess 6.2 to 8.8") == "Recess_6_2_to_8_8"
    assert inv._slug("Sketch - Ação") == "Sketch_Acao"
    assert inv._slug("***") == "P"
