# Sketch traps

Every trap here was **observed** by probing FreeCAD 1.1.3 headlessly, not
inferred. Each states what was done, what came back, and what it costs.

## 1. A sketch's `Shape.Area` is 0.0 even when it encloses area

Built a closed 40×20 rectangle as four `LineSegment`s. The wire closed
correctly — `isClosed()` true, one wire, four edges. `Shape.Area` returned
**0.0**, because a wire is not a face and carries no area.

Cost: a caller told the profile is 0mm² either distrusts the tool or believes
it. Get the area from `Part.Face(Part.Wire(shape.Edges))`, which returned
800.0 for the same profile.

## 2. An unclosed profile extrudes to a wrong shape, not an error

Added a five-edge profile with an arc whose angular span swept the wrong
quadrant. The wire came out **not closed**. Extruding it with
`Part::Extrusion`, `Solid = True`, did **not** raise — it produced a shape.
Inspecting the resulting edges showed a 50mm line where 20mm had been drawn,
and a 15.71 arc-length circle where the geometry did not correspond to what was
requested.

This is the most dangerous class in the whole project: a plausible, valid-looking
result that is not the part asked for. It is why CAP-S3 makes closedness a
first-class, checkable thing and why `extrude_sketch` must refuse an open
profile rather than attempting it. Generalises AD-10: a result that looks valid
is not evidence that it is right.

## 3. Draft types are all unavailable

`Draft::Wire`, `Draft::Rectangle`, `Draft::Circle`, `Draft::Polygon`,
`Draft::BSpline`, `Draft::Ellipse` and `Draft::Shape2D` each raised
`TypeError` on `addObject`. No Draft type can be created in this build.

Cost: any design reaching for a Draft convenience object fails at runtime, not
at import. Confirms AD-21's reasoning for arrays by measurement rather than
assumption.

## 4. `Part.Edge` has no `.Name`

`shape.Edges[0].Name` raised `AttributeError: 'Part.Edge' object has no
attribute 'Name'` on FreeCAD 1.1.3.

The `Edge1`, `Edge2` names the tool surface already exposes are **synthesized
by the bridge** from index position — see `_freecad_bridge.py`, which builds
`f"Edge{index}"`. That is why edge addressing already works and why nothing
here needs changing; it is recorded because reading a name off an edge looks
like the obvious approach and is not available.

## 5. Sketches and Part2D objects do create, and constraints solve

The positive findings, so they are not re-derived:

- `Sketcher::SketchObject` and `Part::Part2DObject` both create fine.
- `addConstraint(Sketcher.Constraint("Coincident", ...))` works; `solve()`
  returned 0 and `FullyConstrained` reported `False` as expected for a
  rectangle with one coincident constraint.
- `Part::Extrusion` on a closed rectangle profile at depth 4 gave
  **volume 3200.0, one solid, `isValid()` true**.
- `PartDesign::Pad` on the same profile gave **the same 3200.0**, via a
  `PartDesign::Body`. It printed an out-of-scope warning — *"Link(s) to
  object(s) 'Sketch' go out of the allowed scope"* — and still succeeded, so
  its success is not something to infer from a clean exit.

## 6. `add_sketch` must not assume a plane

A sketch is created on the XY plane by default. CAP-S6 requires placement, and
placement goes through the existing `set_placement` rather than a new argument
on `add_sketch`, so a sketch and any other object are positioned the same way
and there is one convention rather than two.

Attachment to a face works, and is worth recording because the property names
are not guessable. A sketch exposes `AttachmentSupport` (not `Support`, which
is the FreeCAD 0.x name), `MapMode`, `AttachmentOffset` and `AttacherEngine`.

Verified: attaching a 40×20 sketch to face 6 of a `Part::Box` with
`MapMode = "FlatFace"` moved the sketch to `z = 4.0` — the box's top face —
and extruding it 2mm produced one valid solid of **volume 1600.0** spanning
`z = 4.0` to `z = 6.0`. The geometry landed on the face and extruded normal to
it, which is what CAP-S6 requires.

The tool takes a **1-based integer** for the face, the same convention edges
use. It originally took a `"Face{N}"` string matched by regex, which meant one
caller mistake got three different answers across the surface — `Edge99` raised
`BadGeometry`, `Face99` raised `NoSuchFace`, and `"face6"` raised
`BadGeometry`. One convention, one error.
