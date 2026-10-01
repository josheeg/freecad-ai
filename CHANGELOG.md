# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [PEP 440](https://peps.python.org/pep-0440/). The version
below is the one declared in `pyproject.toml`; nothing else in the repository
restates it, and `tests/test_version.py` fails if a second copy appears.

## [0.1.0] — 2026-09-30

First release. The whole surface, built across 40 commits.

### Added — geometry

36 MCP tools in total.

- **Primitives and documents** — `add_primitive`, `new_document`,
  `open_document`, `save_document`, `list_documents`, `list_objects`,
  `remove_object`, `get_properties`, `set_property`, `set_placement`.
- **Geometry queries** — `describe_geometry` (1-based edges and faces with
  types and positions), `shape_summary`, `measure` (mass properties and
  topology, including whether the shape is valid).
- **Operations** — `boolean_op` (cut, fuse, common), `fillet`, `chamfer`,
  `mirror`, `linear_array`.
- **Measurement** — `distance`, `is_inside`, `cross_section`.
- **Export** — `export_object` to `.step`, `.stl`, `.iges`, `.obj`, `.brep`.
- **Sketches, 2D profile to solid** — `add_sketch`, `add_sketch_line`,
  `add_sketch_arc`, `add_sketch_circle`, `remove_sketch_geometry`,
  `add_sketch_constraint`, `remove_sketch_constraint`, `set_constraint_value`,
  `sketch_status`, `extrude_sketch`, `attach_sketch_to_face`,
  `sketch_to_face`.

### Added — named geometry and driven dimensions

Geometry, arcs, circles and constraints take an optional `name`, and a name is
accepted **anywhere an index is**. This exists because an index does not survive
an edit: FreeCAD renumbers geometry on removal, so an index held across an edit
silently comes to mean a different element. Names are stored on the sketch, so
they travel with a saved document.

`set_constraint_value` re-drives a named `Distance` and lets FreeCAD's solver
move the geometry — the difference between a parametric sketch and a finished
one. A `Distance` reachable only by index could previously be changed only by
being destroyed and re-added.

### Added — packaging

- **A standalone Windows x64 executable**, ~24 MB, needing neither Python nor
  `uv`. FreeCAD is deliberately *not* bundled: it links `python311.dll` and this
  server runs on 3.14, so the two cannot share a process.
- `python -m freecad_ai` as an alternative entry point.
- CI verifies the frozen build by **driving FreeCAD through the executable**,
  not by asserting the file exists.

### Fixed — defects that killed processes or produced wrong solids

- **A NaN dimension killed FreeCAD**, taking every open document with it.
  Dimensions are now validated for finiteness at both choke points.
- **Orphaned objects on every failed face creation**, and again in
  `extrude_sketch`. A tool that creates an object now owns it until it returns
  and rolls back on every failure path.
- **The leak gate failed open** — it reported success when it could not observe
  anything. It now fails closed.
- **A plate with holes was reported malformed.** It is a valid shape the
  surface cannot extrude in one step, and `sketch_status` now says so.

### Fixed — argument validation

Validated against the set of values that are **safe**, not the set that is legal
in FreeCAD. Where those differ, the safer set governs. Measured, not assumed:

- `PosId 0` is a real FreeCAD value that **terminates the interpreter** for a
  two-element constraint. `PosId 4` and `99` are neither valid nor fatal: they
  are silently accepted and leave the solver reporting a negative dof.
- Passing a value to a two-element `Sketcher.Constraint` **terminates FreeCAD**
  rather than raising. The arity is now chosen per constraint kind.
- A boolean is never silently accepted as index 1, on any reference-taking tool.

### Changed

- **One addressing convention: every index is 1-based.** Edges, faces, sketch
  geometry and constraint elements alike. Constraint elements were the last
  holdout at 0-based because FreeCAD numbers geometry internally that way.
- `sketch_status` reports `constraint_count`, the geometry name mapping, and
  every constraint with the elements it actually resolved to.

### Notes for users

- Windows x64 only, and FreeCAD **1.1** only. Both 1.0 and 1.1 are commonly
  installed side by side; an unqualified path silently binds the wrong one.
- The executable is **unsigned**, so Windows SmartScreen will warn on first
  run.
- An unclosed sketch profile does not fail at extrude time — it produces a
  *wrong solid*. Call `sketch_status` first; `extrude_sketch` refuses an
  unclosed profile explicitly.

[0.1.0]: https://github.com/josheeg/freecad-ai/releases/tag/v0.1.0