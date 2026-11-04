import math

import pytest
from shapely.geometry import Polygon

from stl_to_ipt import gears
from stl_to_ipt import inventor as inv
from stl_to_ipt import spec as sp

GEAR = {"type": "gear", "name": "Spur gear", "plane": "XY", "offset": 0.0, "direction": 1, "distance": 20.0,
        "module": 3.0, "teeth": 10, "pressure_angle": 20.0, "addendum": 1.0, "dedendum": 1.2, "phase": 0.0,
        "center": [18.0, 17.37]}


def test_standard_radii():
    r = gears.radii(3.0, 10, 20.0)
    assert r["pitch"] == pytest.approx(15.0)
    assert r["tip"] == pytest.approx(18.0) and r["root"] == pytest.approx(11.25)
    assert r["base"] == pytest.approx(15.0 * math.cos(math.radians(20.0)))


def test_involute_function():
    assert gears.inv(math.radians(20.0)) == pytest.approx(0.014904, abs=1e-6)


def test_tooth_is_symmetric_about_its_center_line():
    pts = gears.tooth_points(3.0, 10, 20.0, 12)
    flipped = sorted((round(x, 6), round(-y, 6)) for x, y in pts)
    assert sorted((round(x, 6), round(y, 6)) for x, y in pts) == flipped


def test_tooth_thickness_at_the_pitch_circle_is_half_the_pitch():
    # at the pitch radius the half angle is pi / (2 z)
    assert gears.half_angle_at(15.0, 3.0, 10, 20.0) == pytest.approx(math.pi / 20, abs=1e-9)


def test_gear_polygon_reaches_tip_and_root_circles():
    poly = gears.gear_polygon(3.0, 10, 20.0, 0.0, (0, 0), 12, 1.2)
    xs = [x for x, _ in poly.exterior.coords]
    assert max(xs) == pytest.approx(18.0, abs=1e-6)
    assert poly.is_valid and poly.area > math.pi * 11.4 ** 2


def test_fit_recovers_the_parameters_of_a_synthetic_gear():
    outline = gears.gear_polygon(2.0, 14, 20.0, 5.0, (30.0, 25.0), 24, 1.25)
    fit = gears.fit_full_gear(outline)
    assert fit is not None
    assert fit["module"] == 2.0 and fit["teeth"] == 14 and fit["pressure_angle"] == 20.0
    assert fit["phase"] == pytest.approx(5.0, abs=0.3)
    assert fit["center"] == [pytest.approx(30.0, abs=0.05), pytest.approx(25.0, abs=0.05)]


def test_a_plain_disk_is_not_a_gear():
    disk = Polygon([(10 * math.cos(a / 40.0 * math.pi * 2), 10 * math.sin(a / 40.0 * math.pi * 2)) for a in range(40)])
    assert gears.fit_full_gear(disk) is None


def test_gear_feature_validation_and_expansion():
    spec = {"system": "model", "features": [GEAR]}
    assert sp.validate(spec)[0] == []
    bad = dict(GEAR, teeth=3, pressure_angle=60)
    errors = sp.validate({"system": "model", "features": [bad]})[0]
    assert any("at least 6 teeth" in e for e in errors) and any("pressure_angle" in e for e in errors)
    expanded = sp.expand_mirrors(spec)["features"][0]
    assert expanded["type"] == "extrusion" and expanded["sketch"][0]["shape"] == "polygon"
    area = sp.sketch_geometry(expanded["sketch"]).area
    assert 640 < area < 700  # the 10-tooth m3 gear outline (the real mesh outline is 682 mm2)


GEAR_PARAMETER_KEYS = ("module", "teeth", "pressure_angle", "phase", "cx", "cy", "pi", "base_r", "tip_r", "root_inner_r",
                       "psi", "psi_a", "roll_tip", "roll_root", "theta0")


def test_equations_use_a_short_pi_literal_and_no_pi_parameter():
    names = {k: "g_" + k for k in GEAR_PARAMETER_KEYS}
    eq = inv.gear_curve_expressions(names, (1, 0, 0, 1))
    assert set(eq) == {"lower", "upper", "root"}
    for x, y, lo, hi in eq.values():
        assert "g_pi" not in x and "g_pi" not in y  # a pi parameter inside an equation breaks the extrusion
    assert inv.PI_LITERAL == "3.14159265" and len(inv.PI_LITERAL.replace(".", "")) <= 9
    assert eq["lower"][2] == "g_roll_root" and eq["lower"][3] == "g_roll_tip"
    assert eq["root"][2] == "-g_theta0"


def test_equations_follow_a_mirrored_sketch():
    names = {k: "g_" + k for k in GEAR_PARAMETER_KEYS}
    ident = inv.gear_curve_expressions(names, (1, 0, 0, 1))["root"][0]
    mirrored = inv.gear_curve_expressions(names, (-1, 0, 0, 1))["root"][0]
    assert ident.startswith("(1) * (") and mirrored.startswith("(-1) * (")


def _sector_outline(z=31, module=3.0, alpha=20.0, phase=2.9, center=(50.0, -24.0)):
    """A gear sector on a plate: the full gear clipped to a box, joined with a plate along the bottom."""
    from shapely.geometry import box
    full = gears.gear_polygon(module, z, alpha, phase, center, 12, 1.25, 1.0)
    return full.intersection(box(0, 0, 100, 30)).union(box(0, 0, 100, 5)).buffer(0)


def test_sector_gear_is_recognized_with_its_tooth_count():
    fit = gears.fit_sector_gear(_sector_outline())
    assert fit is not None
    assert fit["teeth"] == 31 and fit["module"] == 3.0 and fit["pressure_angle"] == 20.0
    assert fit["center"] == [pytest.approx(50.0, abs=0.1), pytest.approx(-24.0, abs=0.1)]


def test_sector_gear_composes_the_outline_from_gear_and_plate():
    outline = _sector_outline()
    fit = gears.fit_sector_gear(outline)
    union, plate, exact = gears.compose_sector(outline, dict(fit, center=tuple(fit["center"])), outline.bounds)
    assert not exact and len(plate) == 1  # the plate is a plain rectangle
    assert plate[0].bounds == pytest.approx((0.0, 0.0, 100.0, 5.0), abs=0.2)
    assert gears._iou(union, outline) > 0.97


def test_round_boss_is_not_taken_for_a_gear_sector():
    from shapely.geometry import Point
    assert gears.fit_sector_gear(Point(22.5, 22.5).buffer(22.5, 64)) is None


def test_flank_may_start_at_the_root_circle():
    # 25 degrees on 31 teeth with the standard dedendum puts the root circle outside the base circle
    rr = gears.radii(3.0, 31, 25.0, 1.25)
    assert rr["root"] > rr["base"]
    ups = {}

    class FakeParameters:
        def AddByExpression(self, name, expression, unit):
            ups[name] = expression

    class FakeComp:
        class Parameters:
            UserParameters = FakeParameters()

    class Ctx:
        @staticmethod
        def unique(name):
            return name

    f = dict(GEAR, teeth=31, pressure_angle=25.0, dedendum=1.25)
    inv.gear_parameters(FakeComp, Ctx, "g", f)
    assert ups["g_roll_root"].startswith("sqrt(") and "theta0" not in ups["g_roll_root"]
    f_small = dict(GEAR, teeth=31, pressure_angle=20.0, dedendum=1.6)
    ups.clear()
    inv.gear_parameters(FakeComp, Ctx, "g", f_small)
    assert ups["g_roll_root"] == "0"
