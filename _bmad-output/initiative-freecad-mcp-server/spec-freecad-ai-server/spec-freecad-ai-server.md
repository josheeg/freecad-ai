---
id: SPEC-freecad-ai-server
companions:
  - tool-surface.md
  - failure-modes.md
  - conventions.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# freecad-ai: an MCP server for FreeCAD 1.1

## Why

An opportunity to capture, and a pain to solve at the same time. FreeCAD is
a capable parametric modeller whose entire interface is a GUI and a Python
console, which puts it out of reach of an AI assistant driving it turn by turn.
Three other FreeCAD automation projects exist on the maintainer's machine;
none is a from-scratch server that an agent can point at a headless FreeCAD
and get structured, verifiable geometry back. The pain is that an assistant
which guesses at FreeCAD's API produces silently wrong models — wrong object
types, dropped list results, a kernel that looks alive after dying. The
opportunity is that FreeCAD's Part workbench is fully scriptable, and the
only thing missing is a boundary that makes its results trustworthy.

## Capabilities

- **CAP-1**
  - **intent:** A caller can place parametric Part primitives and learn each type's real property names rather than guessing them.
  - **success:** A caller that asks which types exist receives the types this FreeCAD build actually creates, with the dimension names each takes, and every advertised type can then be instantiated with those properties.

- **CAP-2**
  - **intent:** A caller can position, mirror and repeat geometry to build an assembly from a few parts.
  - **success:** A box placed at an offset, mirrored through a plane, and repeated n times yields an object whose volume and bounding box match the intended geometry.

- **CAP-3**
  - **intent:** A caller can combine and refine shapes — cut, fuse, intersect, round edges, bevel them — without hand-authoring FreeCAD feature types.
  - **success:** A box with a cylinder cut through it, then filleted, reports a volume matching FreeCAD's own solver for that geometry, and the result exports.

- **CAP-4**
  - **intent:** A caller can inspect what it has built, so it can verify its work instead of assuming the operation did what it asked.
  - **success:** For any solid, a caller can obtain volume, area, centre of mass, bounding box, solid/face/edge counts and validity; can name individual edges and faces; can measure the gap between two objects or a point and an object; can test point containment; and can cut a cross-section and read its area.

- **CAP-5**
  - **intent:** A caller can persist its work and get geometry out in a form other tools accept.
  - **success:** A model saved as FCStd and reopened in a different FreeCAD process is intact, and any solid exports to STEP, STL, IGES, OBJ or BREP as a non-empty file.

- **CAP-6**
  - **intent:** A caller that gets something wrong receives a diagnosis and a next action, not a crash.
  - **success:** Every foreseeable failure — missing document, object, property or edge; an operation that does not fit; a missing directory; an unsupported format — arrives as a typed error carrying a recovery hint, and a genuinely unexpected exception still surfaces rather than being swallowed.

- **CAP-7**
  - **intent:** A caller whose FreeCAD dies mid-session is told, rather than continuing to model against an empty kernel.
  - **success:** Killing FreeCAD mid-session causes the next call to reconnect to a fresh process, report that a restart happened and what was lost, and leave the server usable.

- **CAP-8**
  - **intent:** The server runs on a machine with no display and needs no manual FreeCAD session.
  - **success:** On a headless machine with FreeCAD installed, the server starts from a cold process, launches FreeCAD on first tool call, serves its whole tool surface, and leaves no orphaned process behind.

## Constraints

- FreeCAD links `python311.dll` and cannot be imported from this project's interpreter. The server runs on 3.14 and FreeCAD on its own bundled 3.11, and the two never share a process. This rules out embedding, in-process addons, and any shortcut that puts both in one interpreter.
- FreeCAD is not installable from PyPI. It is an external process, never a dependency, and its presence is a runtime precondition rather than an install-time one.
- The bridge is unauthenticated XML-RPC on loopback. The port must never be widened: anyone who can reach it has full control of FreeCAD.
- The FreeCAD-side code runs on Python 3.11 while all other tooling targets 3.14. Tooling that assumes a single target version will corrupt it — the formatter alone will rewrite valid code into a 3.11 syntax error.
- The target is FreeCAD 1.1 specifically. A second major version may be installed on the same machine, and unqualified paths select the wrong one.
- Failures must reach the caller as data. A raised tool error is not delivered to an MCP client, so an error that is only logged is indistinguishable from a tool that silently did nothing.
- Verification is by automated test across the process, protocol and packaging boundaries, not by the unit suite alone. Every defect found so far lived at a seam the unit and integration suites both cross without noticing.

## Non-goals

- A FreeCAD workbench, GUI addon, or any path that runs inside FreeCAD's process. This would force the whole project onto 3.11 and import the workbench loading semantics with it.
- Running or watching a FreeCAD GUI. The bridge waits on `GuiUp` rather than assuming it, and no GUI code path exists.
- Publishing the package to a registry. Local installation is the intended end state until that is asked for.
- Sketch and constraint creation. Not built; most real parts begin as a 2D profile, so this is the largest known gap in modelling coverage.
- Assembly, multi-document, or 2D drawing generation.
- Fidelity to FreeCAD's parametric history for arrays. The array is a fused set of static copies because FreeCAD's array types are not registered in a headless document.

## Success signal

An assistant, given no FreeCAD-specific knowledge, builds a part with a hole
and a fillet, verifies the volume against the numbers the server reports, and
exports a STEP file — in one unattended session, on a machine with no display,
against a headless FreeCAD it never started itself. Demonstrate it by
running the server over stdio, driving it only through its tool names, and
checking the exported file opens.

## Assumptions

- Assumed single-user, single-machine, Windows. Nothing in the design assumes multiple concurrent users or a shared long-lived service.
- Assumed FreeCAD 1.1.3 remains installed. The version is verified by probing the running FreeCAD rather than pinned in configuration, so a different 1.1 patch needs no change.
- Assumed the current 24-tool surface is a first cut rather than a settled product boundary. A later decision to narrow or widen it is legitimate; the spec records what exists, not what must remain.

## Open Questions

All four are settled. Recorded as decisions rather than left open, because an
unanswered question reads as "still unexamined" and these are now examined.

- **`linear_array` stays a fused set of static copies.** FreeCAD's array types
  are not registered in a headless document, and loading Draft's workbench to
  obtain parametric behaviour is not worth the dependency. The trade-off is
  documented in the tool description so a caller is not surprised: editing the
  source does not update the array.
- **Headless is sufficient.** No GUI target. This makes AD-15 absolute rather
  than provisional — a GUI path is now out of scope, not merely untested.
- **Sketch and constraint creation is in scope** and is the next thing to
  specify. It was the largest known gap in modelling coverage, and most real
  parts begin as a 2D profile. It will be specced separately rather than
  folded in here, because it changes the tool layer's shape — likely a new
  sub-namespace — and the tool surface it settles is a new question of its own.
- **Local installation is the end state.** No publishing, so no release
  workflow and no support policy to maintain. Revisiting this is a scope
  change, not a packaging chore.
