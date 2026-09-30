# Tool surface

The 24 tools the server exposes over MCP stdio. Grouped by what a caller is
trying to do, not by implementation layer.

## Session

| Tool | Purpose |
| --- | --- |
| `connect` | Start FreeCAD if needed and report its version and whether a GUI is up |
| `list_primitive_types` | The Part types this FreeCAD can create, with each one's properties |

## Documents

| Tool | Purpose |
| --- | --- |
| `new_document` | Create a document, or return the existing one. `reuse=False` insists on a new one |
| `open_document` | Open an existing FCStd by absolute path |
| `save_document` | Save to an absolute path |
| `list_documents` | Names of open documents |

`new_document` is idempotent by default and reports `{"name", "created"}`.

## Geometry

| Tool | Purpose |
| --- | --- |
| `add_primitive` | Add a Part primitive. `kind` is a FreeCAD TypeId from `list_primitive_types` |
| `set_placement` | Move and rotate. Components, because a Placement cannot cross the bridge as a value |
| `set_property` | Set one property and recompute |
| `remove_object` | Remove an object |

## Combination and refinement

| Tool | Purpose |
| --- | --- |
| `boolean_op` | `cut`, `fuse` or `common` two objects into a new feature |
| `fillet` | Round edges, by 1-based index from `describe_geometry` |
| `chamfer` | Cut edges flat |
| `mirror` | Reflect through a plane given by a point and a normal |
| `linear_array` | Repeat along a line and fuse the copies |

Every operation here keeps its inputs and returns a new object, so a caller
can keep building on either side.

## Inspection

| Tool | Purpose |
| --- | --- |
| `list_objects` | Objects in a document — `{"objects": [...]}` |
| `describe_geometry` | Edges and faces with FreeCAD names, types, lengths, positions |
| `measure` | Volume, area, centre of mass, bounding box, counts, validity |
| `shape_summary` | The volume/area/bbox subset, kept for existing callers |
| `distance` | Gap between two objects, or between an object and a point |
| `is_inside` | Point-in-solid test |
| `cross_section` | Area and wire count of a planar slice |

## Exchange

| Tool | Purpose |
| --- | --- |
| `export_object` | Write to `.step`, `.stp`, `.iges`, `.igs`, `.brep` (via Part) or `.stl`, `.obj`, `.off`, `.ply` (via Mesh) |

## Result shapes

Every tool returns a dict or a string. Two consequences worth knowing:

- `list_documents` and `list_objects` wrap their list under a named key
  (`documents`, `objects`) rather than returning a bare list.
- Dimensional properties arrive as `{"value": 10.0, "unit": "mm"}`, and a
  value read that way can be passed straight back to `set_property`.

## Worked sequence

```python
new_document("bracket")
add_primitive("bracket", "Part::Box", "Plate", {"Length": 40, "Width": 20, "Height": 4})
add_primitive("bracket", "Part::Cylinder", "Hole", {"Radius": 2, "Height": 10})
set_placement("bracket", "Hole", 10, 9, -3)
boolean_op("bracket", "Plate", "Hole", "cut", "Drilled")
shape_summary("bracket", "Drilled")     # volume 3149.73
describe_geometry("bracket", "Drilled")  # edges by name
fillet("bracket", "Drilled", "Rounded", edges=[1, 3], radius=2.0)
measure("bracket", "Rounded")            # volume 3142.87, solid_count 1
export_object("bracket", "Rounded", "C:/parts/bracket.step")
```

Every volume in that sequence was measured from FreeCAD 1.1.3, not derived.
