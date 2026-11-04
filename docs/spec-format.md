# Spec format

A spec is a JSON document that describes a part as an ordered list of features. The converter writes one per
STL; `stl-to-ipt build` reads them. Specs can also be written or edited by hand.

```json
{
  "system": "model",
  "features": [
    {"type": "extrusion", "name": "Base profile", "plane": "XY", "offset": 0, "direction": 1,
     "operation": "new", "distance": 30,
     "sketch": [{"shape": "rectangle", "x": 0, "y": 0, "width": 100, "height": 40}]},
    {"type": "hole", "name": "Countersunk hole", "subtype": "countersink", "plane": "XY", "offset": 30,
     "direction": -1, "points": [[10, 10], [90, 10]], "d": 3, "depth": 12, "head_d": 7, "angle": 90}
  ]
}
```

## Coordinates

`"system": "model"` uses global X/Y/Z coordinates in millimeters with the minimum corner of the part at the
origin. Sketch coordinates follow the plane: `XY` -> (x, y), `XZ` -> (x, z), `YZ` -> (y, z). The real orientation of
each plane is read from Inventor, so the result does not depend on the template's axis conventions.
(A legacy format with `base`/`holes` and no `system` key is still accepted.)

## Extrusion feature

| Key | Meaning |
|---|---|
| `type` | `"extrusion"` |
| `name` | feature name; also the prefix of the generated parameter names |
| `plane` | `XY`, `XZ` or `YZ` |
| `offset` | distance of the sketch plane from the origin plane along its normal (mm) |
| `direction` | `1` or `-1`, along the global axis of the plane normal |
| `operation` | `new`, `join` or `cut` |
| `distance` | length in mm, or `"through_all"` (cuts only) |
| `sketch` | list of shapes (see below); nested shapes become holes |

## Hole feature

An Inventor Hole feature placed on sketch points. `subtype` is `simple` (a blind hole with a flat bottom),
`countersink` (needs `head_d` and `angle` in degrees) or `counterbore` (needs `head_d` and `head_depth`). `d` is the hole diameter, `depth` the
drilled depth, `offset` the plane of the hole's face, and `points` a list of `[x, y]` centers.

## Gear feature

```json
{"type": "gear", "name": "Spur gear", "plane": "XY", "offset": 0, "direction": 1, "distance": 20,
 "module": 3, "teeth": 10, "pressure_angle": 20, "addendum": 1.0, "dedendum": 1.2, "phase": 0,
 "center": [18, 17.37]}
```

A standard involute spur gear: `module` (mm), `teeth`, `pressure_angle` (degrees), `addendum` and `dedendum` (in
modules), `phase` (angle of tooth 0 in degrees), `center` in plane coordinates and `distance` (face width). The
builder creates a root cylinder, one tooth and a circular pattern, all driven by user parameters. Outlines that are
not a standard involute form (profile shift) stay ordinary contours.

A gear that is only partly used is a full `gear` followed by an ordinary `join` extrusion for the plate around it
and a `cut` extrusion (`Trim gear`) whose rectangles remove the rest of the gear; see `Yaw_Gear` in
`examples/specs`. The converter detects this itself (`docs/development-notes.md`, "Gear sectors").

## Edge fillet

```json
{"type": "fillet", "name": "Edge fillet", "selection": "base_start_outline", "radius": 8}
```

Rounds the outer edges of one cap of the base extrusion: `base_start_outline` is the cap at the sketch plane,
`base_end_outline` the opposite one. Only the outer loop is filleted, never the holes. The feature must come right
after the base extrusion, and holes that lie inside the fillet band belong after it (a separate cut or `hole`
feature), because the rolling ball cannot pass over them. `radius` becomes the parameter `<name>_radius`. The legacy
selections `all`, `vertical`, `top` and `bottom` are still accepted.

## Patterns

* A `rectangle` or `circle` with a `pattern` of type `linear` (one direction, three or more copies) is drawn once and
  patterned in the sketch; the count and the pitch are parameters.
* A `circle` with `{"type": "circular", "n": 6, "cx": 40, "cy": 40}` is drawn once and patterned around
  `(cx, cy)`.
* A `hole` feature with the same `pattern` annotation keeps the full list of `points` (the geometry code uses
  them) while the builder places only the first hole and adds a circular pattern feature around the axis through
  `(cx, cy)`.

## Mirror feature

```json
{"type": "mirror", "name": "Mirror YZ", "plane": "YZ", "offset": 35.0, "features": ["Pad 0.0 to 3.0"]}
```

Mirrors the listed features (they must come earlier in the list and have unique names) about the plane `plane`
(`YZ` mirrors along X, `XZ` along Y, `XY` along Z) placed at `offset`. The converter writes it when a part is
symmetric: duplicated features and mirror-twin shapes or hole centers are removed and replaced by one mirror.
Tools that evaluate geometry call `spec.expand_mirrors(spec)` to get the plain feature list back.

## Shapes

| `shape` | Keys |
|---|---|
| `rectangle` | `x`, `y` (lower-left corner), `width`, `height`, optional `centered` |
| `circle` | `x`, `y`, `d` |
| `polygon` | `points` |
| `slot` | `x1`, `y1`, `x2`, `y2`, `width` |
| `contour` | `start: [x, y]` and `segments`: `["L", x, y]` for a line or `["A", mid_x, mid_y, x, y]` for a three-point arc |

Any `rectangle` or `circle` can carry a `pattern`: `{"type": "linear", "nx": 3, "ny": 1, "dx": 10, "dy": 0}`
or `{"type": "circular", "n": 6, "cx": 0, "cy": 0}`. A one-dimensional linear pattern is dimensioned with a
single pitch parameter in Inventor.

## Notes

The optional `notes` object records how the converter produced the spec (`part`, `extrusion_axis`,
`tolerance_mm`, `iou`, `pruned_features`, ...). It is informational and ignored when building, with two
exceptions used by the fit check: `original_min` (position of the part in the shared design frame) and
`raw_diameters` (diameters before rounding).

## Parameters created in Inventor

Dimensions are named after the feature: `<feature>_x1`, `<feature>_y1` (positions), `<feature>_w1`,
`<feature>_h1` (rectangles), `<feature>_D1` (diameters), `<feature>_H1`, `<feature>_V1` (contour steps),
`<feature>_R1` (arc radii), `<feature>_pitch`, `<feature>_depth` and `<feature>_position` (plane offset).
