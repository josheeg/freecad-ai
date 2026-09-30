# Sketch tool surface

The eight tools this spec adds. All are **additive** — the existing 24 are
unchanged, and a sketch-derived solid is an ordinary object to all of them.

Every sketch tool takes `document` and `sketch` names, not handles, consistent
with AD-8. Geometry is described in the server's own terms and constructed on
the FreeCAD side.

## Proposed tools

| Tool | Purpose |
| --- | --- |
| `add_sketch` | Create an empty sketch, optionally at a placement |
| `add_sketch_line` | Add a line segment from `(x1,y1)` to `(x2,y2)` |
| `add_sketch_arc` | Add an arc: centre, radius, start and end angle |
| `add_sketch_circle` | Add a circle: centre and radius |
| `remove_sketch_geometry` | Remove geometry by 1-based index |
| `add_sketch_constraint` | Add a constraint by type and the geometry it touches |
| `sketch_status` | Closed or not, edge count, area, degrees of freedom |
| `extrude_sketch` | Turn a closed profile into a solid of a given depth |
| `attach_sketch_to_face` | Snap a sketch flat onto a planar face of another object |
| `sketch_to_face` | Turn a closed profile into a real planar face |

`sketch_status` is the important one. CAP-S3 requires the caller to be able to
ask whether a profile is usable *before* extruding, and `extrude_sketch` must
refuse an unclosed profile rather than producing a wrong solid.

## Why geometry is described, not passed

A `Part.LineSegment` cannot cross the XML-RPC boundary: it is a FreeCAD object
with no marshallable form. So the server receives numbers and the bridge builds
the geometry, exactly as `set_placement` receives six floats and builds a
`Placement`. This is AD-8 in practice, not a new idea.

```python
add_sketch("bracket", "Profile")
add_sketch_line("bracket", "Profile", 0, 0, 40, 0)
add_sketch_line("bracket", "Profile", 40, 0, 40, 20)
add_sketch_line("bracket", "Profile", 40, 20, 0, 20)
add_sketch_line("bracket", "Profile", 0, 20, 0, 0)
sketch_status("bracket", "Profile")
# {"closed": true, "edges": 4, "area": 800.0, "dof": 8}
extrude_sketch("bracket", "Profile", "Plate", depth=4.0)
# "Plate"
measure("bracket", "Plate")
# volume 3200.0, solid_count 1, valid
```

Every number above was produced against FreeCAD 1.1.3, not derived. The 800mm²
area comes from `Part.Face` on the wire, never from the sketch's own
`Shape.Area`, which reads 0.0.

## Reuse, not duplication

Most of what a sketch flow needs already exists and needs no new argument:

| need | existing tool |
| --- | --- |
| place the sketch | `set_placement` |
| inspect its edges | `describe_geometry` |
| cut a solid from it | `boolean_op` |
| round an edge | `fillet` |
| bevel an edge | `chamfer` |
| verify the result | `measure`, `shape_summary` |
| measure a gap | `distance`, `is_inside` |
| slice it | `cross_section` |
| write it out | `export_object` |

Only the seven tools above are new. A sketch-derived solid is an ordinary
object to all of them, which is CAP-S7 and the reason this is a bounded piece
of work rather than a new subsystem.

## Arc angles

`add_sketch_arc` takes a centre, radius, and start/end angles **in degrees**,
matching the degree convention AD-19 fixed for placements. Angles are measured
counter-clockwise from the positive X axis in the sketch's plane. This is the
one place a caller can silently get a profile that does not close, so
`sketch_status` reports closedness and `extrude_sketch` refuses an open profile.

## Order matters when extruding

A `Part::Extrusion` captures the sketch's placement when it is created. Position
the sketch **first**, then extrude; moving the sketch afterwards does not move a
solid that has already been made.

## Constraint indices are 0-based

`add_sketch_constraint`'s `first` and `second` are 0-based, as FreeCAD numbers
geometry internally. Every other index in this surface is 1-based. That
inconsistency is FreeCAD's, not this tool's, so the tool description says so
rather than leaving a caller to discover it by getting a fault.
