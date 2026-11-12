# Development notes

## Pipeline

1. `converter.slice_and_group` slices the mesh along the chosen axis and groups consecutive slices with the same
   section (boundaries refined by bisection and pinned to real mesh vertices, at most 6 groups).
2. `converter.plan` picks the base run (least length-weighted editing cost); other runs become a `Recess`
   (cut of base minus run) or a `Pad` (union of the whole run section; cuts come first).
3. Silhouette through cuts on the other two axes are computed on the base body only.
4. `converter._prune` drops features that change the IoU by less than 0.4% each (0.6% in total) for steps and
   0.2% for silhouette cuts. The base and hole features are never pruned.
5. If the IoU stays below 0.97, `best_rotation` tries rotating the mesh (+/-45 degrees, 5 degree steps).
6. `inventor.create_ipt` builds the part and names parameters; the part volume is checked after each feature and
   the opposite direction is tried when nothing changed.

Contour fitting segments the outline into lines, arcs and circles (tolerance `--tol`). Arcs and circles are
checked at vertices and at edge midpoints, because the vertices of a rectangle are concyclic. Corners are found
by turn angle (> 22 degrees). Consecutive arcs on one circle are merged and short tessellation facets next to a
tangent arc are absorbed.

## Inventor API pitfalls

- Internal API units are centimeters. Documents are switched to millimeters (`LengthUnits = 11269`).
- Create the application with `win32com.client.dynamic.Dispatch`. `gencache.EnsureDispatch` makes
  `Documents.Add` return generic documents without `ComponentDefinition`. If a stale `gen_py` cache is present
  (`%TEMP%\gen_py`), delete it.
- Inventor always creates arcs counter-clockwise, so a clockwise arc comes back with start and end swapped;
  `inventor._nearest_end` links consecutive segments by proximity.
- The sketch origin point is obtained with `sk.AddByProjectingEntity(comp.WorkPoints.Item(1))`; `sk.OriginPoint`
  is not usable for constraints.
- For a hole feature, "positive" is opposite to the sketch normal (unlike an extrusion). After a hole feature is
  deleted its sketch cannot be reused, so every attempt creates a new sketch.
- Hole API signatures: `AddCSinkByDistanceExtent(placement, d, depth, direction, csink_d, csink_angle_rad,
  flat_bottom)`, `AddCBoreByDistanceExtent(placement, d, depth, direction, cbore_d, cbore_depth, flat_bottom)`,
  `AddDrilledByDistanceExtent(placement, d, depth, direction, flat_bottom)`.
- Contours that touch or self-intersect make `Profiles.AddForSolid()` fail with `E_INVALIDARG`; they are repaired
  with shapely `make_valid` and redrawn as line polygons. Closing must use a tolerance: sampled arcs leave
  about 1e-14 of noise, and an exact equality test sends valid arc contours to the repair path.

- Sketch geometry read through COM is stale until the document is recomputed: call `doc.Update()` (here
  `Context.update`) before measuring after changing a parameter. `sk.ConstraintStatus` returns a constant and is
  useless for detecting over-constraint.
- With tangent constraints present, Inventor silently ACCEPTS redundant dimensions (without them it raises). The
  sketch is then invalid and `AddByDistanceExtent` fails with E_FAIL. `_is_independent` tests every dimension of a
  contour that has tangents by perturbing its value by 0.02 mm and checking that the geometry follows; redundant
  dimensions are deleted. Points where segments flow tangentially are not dimensioned at all, except one anchor per
  direction when nothing else ties the contour to the origin.
- The junction between two sketch entities is the vertex they share. Do not assume it is the "end" of the first
  one: Inventor swaps start and end of clockwise arcs.
- `MirrorFeatures.Add(features, plane, False, 47361)`: 47361 is `kIdenticalCompute`
  (`kAdjustToModelCompute` is 47362, `kOptimizedCompute` 47363).
- `SketchArcs.AddByFillet(entity1, entity2, radius, point_on_1, point_on_2)` exists and adds the tangent
  constraints itself; the contour builder draws the fitted arcs directly and adds `AddTangent`.

- **Equation curves** (`SketchEquationCurves.Add(98305, 98561, x, y, t_min, t_max)`): the expressions and the range
  accept document parameters. Trigonometric arguments are read as degrees. A literal `pi` with more than nine
  digits, or a parameter holding `pi`, makes the profile impossible to extrude; use `3.14159265` everywhere in an
  equation. Curves that must close should share their end points: draw the tip arc with
  `AddByCenterStartEndPoint` using the end points of the flank curves (a gap of 1e-8 mm already breaks the profile)
  and connect the other ends with `AddByTwoPoints` on the curves' own `SketchPoint`s.
- Parameter expressions: no `pi` constant (use `acos(-1) / 1 rad`), trigonometry needs an angle (`tan(a)` with an
  angle parameter, or `x * 1 deg`), and `atan(x) / 1 deg` converts back to a plain number.
- **Sketch patterns**: `RectangularPatterns.CreateDefinition(geometries, direction_entity, count, natural, False,
  spacing)` reads a numeric spacing in document units (mm here) and silently creates nothing when the spacing is a
  string. `CircularPatterns.CreateDefinition(geometries, center_point, True, count, spacing_deg, False, True, False)`
  reads degrees (with `Fitted` true, 360 degrees would put the last copy on the first). The pattern direction must be
  derived from the real sketch geometry, because the sketch axes can be mirrored relative to the spec.
- **Pattern features**: `CircularPatternFeatures.Add(features, axis, True, count, angle_radians, True, 47361)`;
  `WorkAxes.AddFixed` takes a geometry `Point` and a `UnitVector` (not a `WorkPoint`).
- Sketch area check: `profile.RegionProperties.Area` is in cm2. The spec side must sample arcs finely (96 steps)
  or large arcs look 1% smaller; sketches that approximate arcs by lines are compared with a looser limit.

## Gear sectors

`Yaw_Gear` is a sector of a 31-tooth module-3 gear sitting on a plate. Replacing its toothed edge by an arc
loses the root fillets, so the converter instead builds the **whole gear** (`gear` feature), joins the part that
is not gear (a plain rectangle) and cuts the gear away outside the silhouette (`Trim gear`, up to four rectangles
around the silhouette's bounding box).

`gears.fit_sector_gear` finds it on the mesh section:

1. the tip circle is the circle that most convex-hull vertices lie on (RANSAC over triples, 0.3 mm);
2. the tooth count comes from the angular coherence of the tip vertices, `|mean(exp(i z theta))|`, over the counts
   that give a standard module (at least 1.0) for that tip radius; the tip radius alone is not enough (`z` and
   `z + 1` differ by 1.5 mm at module 3), and the nominal `m (z + 2) / 2` can be off by a little;
3. `teeth_present` rejects round bosses: above the pitch circle a gear is solid at the tooth centers and empty
   halfway between them, a boss is solid everywhere;
4. the candidates are scored by `edge_agreement`, the fraction of edge points (in the ring of the inner teeth) that
   are within 0.4 mm of the other shape's edge. Area ratios do not work here: the teeth are a thin strip, so a
   0.13 mm tip radius error already halves the IoU. Real gears score 0.89 to 1.0, look-alikes 0.43 to 0.53.

The dedendum is chosen by the IoU of the whole composition (the roots are mostly hidden by the teeth). When the
root circle is larger than the base circle (31 teeth at 25 degrees) the flank starts at the root circle; the choice
is made when the part is built, so editing module or teeth later must keep the root on the same side of the base
circle. A sector may lose up to 0.03 of IoU against the contour version (flat roots instead of mesh fillets).

## Holes with a changing diameter, on any axis

A hole shows up in the slices along its own axis as a circular inner ring whose diameter changes with depth.
`profile_holes` reads that diameter profile from the face (the wide end) inwards: an optional flat part, an optional
cone and the shank.

- flat part then a step of at least 0.8 mm: counterbore (a smaller step is a chamfer or noise and is ignored);
- cone from the face: countersink, the head diameter being the fit at the face and the angle snapped to the standard
  ones;
- flat part (at least three slices), then a cone: a **circular recess** with a countersunk hole in its floor. The
  floor is where the cone's line reaches the flat diameter; the recess is a cut from the floor through the open side.

It runs on the extrusion axis (`detect_holes`) and on the others (`find_blind_holes`). On the other axes:

- a cone cut by a curved face is slightly oval, so rings with a circularity of at least 0.95 and an aspect ratio of at
  most 1.4 count (`_rings_loose`) in profile tracks, with the diameter of the circle of equal area; constant-diameter
  holes still need exact circles;
- a hole whose both ends are open is a blind hole when exactly one end leads to material further along the axis (a
  cavity) and the other to nothing (the outside): it is drilled from the outside and reaches one slice into the cavity;
- the face of a countersunk hole moves out to the outermost point of the part around it, growing the head along the
  cone, because on a curved face the cone goes on to the crown although only the opening is seen.

Holes across the extrusion axis would otherwise show up as steps of the base. `hole_fill` hands the slicer the
sections as if they had not been drilled: the hole, widened to a cylinder as wide as its head, clipped to a section
just outside the hole (the part is prismatic there), which also restores a crown the hole cut off. If no such section
fits, the hull of the section piece is used. Steps that only imitated a hole are then removed by the pruning;
`Circular recess` cuts are never pruned.

## Tilted (radial) holes

A curved part (`Pedal_Heel`, `Pedal_Main_Body`) has screw holes drilled normal to the curve, at an angle about the
extrusion axis. Along a slice axis they show up as short tracks of round rings whose center drifts steadily
(`_drifting_tracks`: at least three slices, the drift along the third axis is linear, the other coordinate constant).
`find_tilted_holes` takes each distinct drift angle, turns the mesh about the extrusion axis until that direction lines
up with the slice axis, runs the ordinary hole detection (`find_blind_holes`, so countersinks, counterbores and the
cavity rule work as before) and carries the holes back with the inverse transform. Only holes that lie on an observed
drift are kept: in the turned mesh the axis-aligned holes also look like drifting tracks. `hole_fill` hands the
slicer a strip along the axis instead of the straight corridor, so the notches these holes leave in a section do not
turn into recesses or pads of the base profile. A tilted hole is its own feature (`tilt`), since a work plane in
Inventor carries one sketch point.

## Rounded edges

A fillet along a stadium or any curved outline shows up in the silhouettes only along the straight parts, so the
slicer used to imitate it with rings of steps (`Pedal_Base_Pins_Cap` had eight features and 193 parameters).
`_apply_edge_fillet` measures it instead: for distances `h` from a cap it finds the inset `d(h)` that shrinks the
full outline (the section with the largest area) to the area of the mesh section, and fits the radius of the
rolling ball, `d(h) = R - sqrt(R^2 - (R - h)^2)`, to it (RMS under 0.2 mm). A chamfer is linear in `h` and does not
fit. The base then gets the full outline without holes, followed by the fillet, a `Through holes` cut and what is
left of the old features: the steps, pads and silhouette cuts that only imitated the rounding are dropped one by
one while the IoU stays within 0.002. Inventor refuses the fillet when holes sit in its band, hence the order.

## Rounding, fits and mirroring

- `rounding.nice` is the single rounding policy. The converter applies it to the whole spec and `_regularize`
  clusters contour coordinates and arc centers first, so tangent points line up with the centers they belong to.
- `fits` needs `notes.original_min` and `notes.raw_diameters`, which the converter writes. Specs without them (or
  rotated parts) are ignored.
- `symmetry.mirror_features` is geometry based and guarded by the IoU: `_apply_mirror` keeps a mirror only if the
  IoU does not drop by more than 0.001. Independent cutters can be split into mirror twins; shapes of an additive
  sketch cannot (nested shapes are holes of their neighbor).

## Known limitations

- Parameter counts are high on contour-heavy parts (gears, `Rotation_Bar`), because every X/Y step of a contour
  has a dimension.
- Plain through holes stay as sketch circles instead of Inventor Hole features. Blind holes across the extrusion
  axis are detected, but blind holes along it still appear as recesses.
- Tilted holes are found only when the tilt is about the extrusion axis, and they are not mirrored.
- Only one mirror plane is used per part, and the base profile is always drawn complete.
- Tilted parts are rotated in the mesh instead of using an inclined work plane.
- Linear patterns are several shapes inside one sketch, not a rectangular-pattern feature.

## Orientation, micro chamfers and progress (v0.7)

* **Alignment (`auto_orient`)**: the largest flat-face directions are aligned with the global axes. It is only
  used when it shrinks the bounding box to 85% or less (a part tilted in the model frame); the part is converted
  both ways and the better fit is kept. The matrix is stored in `notes.aligned_with_rotation_matrix`.
* **Micro chamfers (`simplify.py`)**: 45-degree edge breaks of up to 0.7 mm between a horizontal and a vertical
  line are removed from contours unless the IoU drops by more than 0.003. Inventor sketches of contours with more
  than 14 edges only dimension the overall width/height and the radii.
* **Robust sections**: `section` repairs invalid GEOS geometry (`TopologyException`) instead of aborting.
* **Progress**: workers report their stage; the batch shows it in the bar and logs a heartbeat every 20 s.
* **Known limitation**: parts with two different axes (bent tubes) are still sliced along one axis.

## Knurled surfaces

A nearly circular section with a fine, regular ripple (radius within +/-10%, at least 12 crossings of the mean
radius) is replaced by the circle of the same area, so a knurled grip is modelled as a smooth cylinder instead of
one sketch per section. Gears are not affected (their tooth depth is larger and they are handled by the gear step).
