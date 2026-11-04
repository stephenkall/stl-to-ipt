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

## Known limitations

- Parameter counts are high on contour-heavy parts (gears, `Rotation_Bar`), because every X/Y step of a contour
  has a dimension.
- Plain through holes stay as sketch circles instead of Inventor Hole features.
- Tilted parts are rotated in the mesh instead of using an inclined work plane.
- Linear patterns are several shapes inside one sketch, not a rectangular-pattern feature.
