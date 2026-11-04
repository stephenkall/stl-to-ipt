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
- Only one mirror plane is used per part, and the base profile is always drawn complete.
- Tilted parts are rotated in the mesh instead of using an inclined work plane.
- Linear patterns are several shapes inside one sketch, not a rectangular-pattern feature.
