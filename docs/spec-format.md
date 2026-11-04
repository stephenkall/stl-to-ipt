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

An Inventor Hole feature placed on sketch points. `subtype` is `simple`, `countersink` (needs `head_d` and
`angle` in degrees) or `counterbore` (needs `head_d` and `head_depth`). `d` is the hole diameter, `depth` the
drilled depth, `offset` the plane of the hole's face, and `points` a list of `[x, y]` centers.

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
`tolerance_mm`, `iou`, `pruned_features`, ...). It is informational and ignored when building.

## Parameters created in Inventor

Dimensions are named after the feature: `<feature>_x1`, `<feature>_y1` (positions), `<feature>_w1`,
`<feature>_h1` (rectangles), `<feature>_D1` (diameters), `<feature>_H1`, `<feature>_V1` (contour steps),
`<feature>_R1` (arc radii), `<feature>_pitch`, `<feature>_depth` and `<feature>_position` (plane offset).
