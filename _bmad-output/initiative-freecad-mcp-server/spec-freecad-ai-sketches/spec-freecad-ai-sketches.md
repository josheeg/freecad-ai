---
id: SPEC-freecad-ai-sketches
companions:
  - sketch-surface.md
  - sketch-traps.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the
> complete contract for what to build, test, and validate. Companion files carry
> the load-bearing detail that would bloat the kernel.

# freecad-ai sketches: 2D profiles to solids

## Why

A pain to solve, and an opportunity to capture at once. FreeCAD's Part
primitives cover boxes, cylinders and spheres, but most real parts begin as a
2D profile that is drawn and then given depth — a bracket plate, a gusset, a
flange. The current 24 tools cannot express that: there is no way to create a
sketch, add a line or an arc, or push a profile into a solid. An assistant
reaching for one has to approximate with primitives and boolean cuts, which
produces the wrong part and reports success doing it.

The opportunity is that this is genuinely scriptable headlessly. Probing
FreeCAD 1.1.3 confirms `Sketcher::SketchObject` creates, accepts geometry,
solves constraints, and extrudes to a correct solid. The gap is the missing
boundary, not a missing capability.

Scope is deliberately a **2D profile to a solid**, chosen over full PartDesign
parametric history. A profile plus an extrusion is what most modelling actually
needs; the feature tree is a much larger build with a much larger test surface.

## Capabilities

- **CAP-S1**
  - **intent:** A caller can create a named sketch in a document and add line, arc, and circle geometry to it at chosen coordinates.
  - **success:** A caller specifying a closed four-line rectangle obtains a sketch whose shape is one closed wire with four edges of the requested lengths, readable through the existing `describe_geometry`.

- **CAP-S2**
  - **intent:** A caller can turn a closed profile into a solid of a chosen depth, in a chosen direction.
  - **success:** A 40×20 rectangle extruded 4mm yields one valid solid of volume 3200mm³, matching FreeCAD's own solver, exportable via the existing `export_object`.

- **CAP-S3**
  - **intent:** A caller can be told whether a profile is closed and usable before asking for a solid, and after a failed attempt.
  - **success:** Given a profile with a gap, the server reports the profile is not closed and names the offending edges, rather than extruding it. Given a closed one, it reports closed and the resulting area.

- **CAP-S4**
  - **intent:** A caller can constrain a sketch so dimensions are driven by named constraints rather than baked into coordinates.
  - **success:** A caller names a `Distance` constraint, re-drives it, and the solver moves the geometry to match — read back from the sketch, not echoed from the request. Verified: a 40x20 profile's named `width` re-driven to 75 reports `value: 75.0` and stays a single constraint; a closed rectangle re-driven from 40x20 to 60x35 keeps `closed: true` throughout.
  - **note:** This was partly fiction until the naming work landed. There was no way to remove a constraint, `sketch_status` did not report how many existed, and no dimension could be changed without destroying and re-adding it. `remove_sketch_constraint` and `set_constraint_value` are what make the claim true (AD-29, AD-31).

- **CAP-S5**
  - **intent:** A caller can edit and delete sketch geometry, and delete a sketch, without corrupting the document.
  - **success:** Removing a line from a four-edge sketch leaves three edges that are correctly renumbered, and a deleted sketch leaves no trace in `list_objects`.
  - **note:** Renumbering is why geometry and constraints can be **named**. A name is reindexed in the same call that renumbers the geometry, so it still refers to the same element afterwards; an index held across an edit silently comes to mean something else (AD-30). Verified: removing the middle of four named lines moves `top` from index 3 to 2 and a constraint built from the name still lands on the element it names.

- **CAP-S6**
  - **intent:** A sketch can be placed in space and attached to a face, so profiles are not confined to one plane.
  - **success:** A sketch with a non-default placement extrudes to a solid at the corresponding offset, and a sketch attached to a planar face takes that face's position and orientation — verified: a 40×20 sketch attached to a box's top face extruded to a valid 1600mm³ solid spanning the face's z range.

- **CAP-S7**
  - **intent:** A sketch participates in the rest of the tool surface unchanged, as an ordinary object.
  - **success:** A sketch appears in `list_objects`, its edges are addressable by the same 1-based `Edge{N}` indices `fillet` and `describe_geometry` already use, and a sketch-derived solid can be cut, filleted, measured, and exported with the existing tools and no new arguments.

- **CAP-S8**
  - **intent:** A caller can snap a sketch flat onto a planar face of an existing object, so a profile follows a surface rather than a plane they must compute.
  - **success:** A 30×15 sketch attached to the top face of a 40×20×4 box takes that face's position and extrudes normal to it — verified: volume 900mm³ spanning z 4 to 6. A non-planar face is refused by name rather than producing a degenerate placement. The face is named by a 1-based integer, the convention edges use, so a wrong index is answered the same way on both.

- **CAP-S9**
  - **intent:** A caller can turn a closed profile into a real planar face, which carries the area a wire cannot.
  - **success:** A 30×15 profile becomes a face reporting one face of area 450mm², and that face is measurable, cuttable and exportable like any other object — verified: cutting a 3mm hole from it yields 421.7257mm² (= 450 − π·9) over two wires. It is a static snapshot, not a parametric link, and it is not extrudable: `extrude_sketch` takes a sketch.

## Constraints

- **Draft is unavailable headlessly.** Every `Draft::*` type raises `TypeError` on creation in this build. No capability may depend on Draft — the same conclusion AD-21 reached for arrays, now confirmed by probe rather than assumption.
- **A sketch's `Shape.Area` is 0.0** even when the profile encloses area, because a wire is not a face. Any area a caller is shown must come from `Part.Face(wire)`, never from the sketch's own shape.
- **`Part.Edge` has no `.Name` attribute.** The `Edge{N}` names the tool surface already exposes are synthesized by the bridge from index position. Sketch geometry must be addressed the same way, never by a name read off an edge.
- **An unclosed profile does not fail loudly.** `Part::Extrusion` with `Solid=True` over an open wire returned a shape rather than an error, with edges that did not correspond to the geometry drawn. A closedness check is mandatory before extruding, not an optimisation.
- **Failure must arrive as data.** Consistent with AD-4, a sketch that cannot be created, constrained, or extruded returns `{error, kind, hint}` and never raises.
- **A sketch crosses the bridge as names and numbers.** Consistent with AD-8: geometry is described in the server's own terms and constructed on the FreeCAD side, because a `Part.Geometry` object cannot be marshalled.

## Non-goals

- **A full PartDesign feature tree.** No Body, Pocket, Fillet feature, or datum
  plane hierarchy. A profile becomes a solid by extrusion; the parametric
  history above that is out of scope. FreeCAD can do it — verified, both
  `Part::Extrusion` and `PartDesign::Pad` produce a correct 3200mm³ solid —
  and this spec declines it deliberately.
- **Spline and ellipse geometry.** Line, arc, and circle only. They cover the
  profiles that extrude cleanly; splines need control-point placement and
  bring their own closure problem.
- **3D sketches.** A sketch is planar. Placement moves it; it does not bend it.
- **Constraint solving as a product.** Constraints are exposed and their effect
  is reported, but there is no tool for diagnosing an over-constrained sketch
  beyond naming the solver's own complaint.
- **Draft interoperability.** No Draft object is created, imported, or converted.
- **Modifying the existing 24 tools.** Every one keeps its current behaviour;
  new tools are additive, per the surface-stability convention.

## Success signal

An assistant, given no FreeCAD knowledge, draws a bracket profile with a
rounded corner as a sketch, extrudes it to a solid, fillets an edge by
`Edge{N}`, checks the volume against what the server reports, and exports STEP
— in one unattended session against a headless FreeCAD. Demonstrate by driving
only the tool names, and confirm the exported file opens and the volume matches
the profile's area times the depth.

## Assumptions

- Assumed the sketch tools are additive to the existing 24, not a replacement
  for any of them. `add_primitive` and `boolean_op` keep working unchanged.
- Assumed callers are willing to state profile coordinates explicitly rather
  than relying on interactive selection, which is unavailable headlessly.
- Assumed `Part::Extrusion` is the primary pad path, with PartDesign as a
  possible second route. Both were verified to produce the same volume; the
  choice between them is an implementation decision, not a capability one.

## Open Questions

All three are settled. Recorded as decisions rather than left open.

- **Extrusion direction is not an argument.** `extrude_sketch` takes a depth
  and nothing else; the sketch's own `Placement` decides which way is out. One
  rule for orientation rather than two that can disagree, and it routes through
  the same `set_placement` every other object uses. A swept profile along an
  edge would be a new capability, not a parameter.
- **Geometry is addressed by index only**, matching `describe_geometry`,
  `boolean_op` and `fillet`. A caller-supplied name would be a second
  convention alongside every index-based tool, and FreeCAD renumbers indices on
  removal anyway. `remove_sketch_geometry`'s description says so.
- **Only `Part::Extrusion` is exposed.** `PartDesign::Pad` produced the same
  volume but printed an out-of-scope warning *on success*, so its correctness
  cannot be inferred from a clean exit. One way to make a solid is worth more
  than two, one of which is noisier.

## Notes from building it

- `Sketcher.Constraint` built with six arguments — the value form applied to a
  two-element constraint — **terminates FreeCAD** rather than raising. The
  four-argument form returns normally. `add_sketch_constraint` now picks the
  arity per kind and rejects an unknown `kind` before constructing anything, so
  a bad argument cannot take the bridge process down with it.
- A `Part::Extrusion` takes the sketch's placement **at the moment it is
  created**. Moving the sketch afterwards does not move a solid already
  extruded, so a profile must be extruded after it is positioned.
- A rectangle swept into a box has 12 edges and 6 faces, not 4 — each side is
  split where the corners meet. Measured, and asserted as measured.
