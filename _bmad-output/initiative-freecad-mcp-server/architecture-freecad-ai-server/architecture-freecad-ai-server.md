---
name: 'freecad-ai server'
type: architecture-spine
purpose: build-substrate
altitude: initiative
paradigm: process-boundary ports-and-adapters
scope: The freecad-ai MCP server — its process boundary, tool layer, bridge protocol, and the tooling that keeps them honest
status: final
created: '2026-09-30'
updated: '2026-10-01'
binds:
  - CAP-1
  - CAP-2
  - CAP-3
  - CAP-4
  - CAP-5
  - CAP-6
  - CAP-7
  - CAP-8
sources:
  - ../spec-freecad-ai-server/spec-freecad-ai-server.md
  - ../spec-freecad-ai-server/tool-surface.md
  - ../spec-freecad-ai-server/conventions.md
  - ../spec-freecad-ai-server/failure-modes.md
companions: []
---

# Architecture Spine — freecad-ai server

## Design paradigm

**Process-boundary ports-and-adapters.** The FreeCAD kernel is a port. Everything
that can reason about a result sits on the server side of it; everything that
touches a `TopoShape` sits on the FreeCAD side. The boundary is a process, not
a module — that makes the interpreter split a structural fact, not a style
preference.

*Unit* below means one implementing agent or feature consuming this document.
It does not mean a code module.

```
   agent ──stdio/JSON-RPC──> server.py            CPython 3.14
                                │  tools
                             bridge.py             CPython 3.14
                                │  XML-RPC (loopback, unauthenticated)
                                ╎  ── port ──
                    _freecad_bridge.py             FreeCAD's bundled 3.11
                                │  Part / Mesh
                              FreeCAD               freecadcmd, headless
```

`src/freecad_ai/`
| module | interpreter | role |
| --- | --- | --- |
| `server.py` | 3.14 | MCP surface; tools return values, never raise |
| `bridge.py` | 3.14 | XML-RPC client, process launcher, port identity check |
| `_freecad_bridge.py` | 3.11 | FreeCAD-side RPC handler; sole `import FreeCAD` |
| `_version.py` | 3.14 | `__version__`, extracted to break a circular import |

## What this document governs

It fixes only the trade-offs two independently built units would otherwise
resolve differently, and only where the answer is non-obvious. Everything else
— the tool inventory, argument schemas, naming style, directory layout — lives
in the spec and its companions and is not constrained here.

Silence here is not approval. It means the decision is not load-bearing, and a
unit is free to make it. Where a decision is genuinely open, it is named under
[Deferred](#deferred) rather than left blank.

The Architecture decisions below carry ids because other units cite them. The
sections that follow are the contract a unit consults while coding; treat an
unnumbered rule as weaker than a numbered one and escalate a conflict rather
than resolving it silently.

## Architecture decisions

Load-bearing for every capability. These bind `all` and a unit that breaks one
has not built a variant — it has built something else.

| id | decision | binds | prevents |
| --- | --- | --- | --- |
| AD-1 | The paradigm is process-boundary ports-and-adapters. FreeCAD is a port, not a library. | all | A unit putting FreeCAD types in the server's own type signatures, or crossing the boundary with anything but an XML-RPC call. |
| AD-2 | Dependency direction is `server → bridge → _freecad_bridge`, strictly downward. `_freecad_bridge` is launched by path and never imported. | all | `_freecad_bridge` appearing in the server's import graph, which would drag 3.11-only modules into 3.14. |
| AD-3 | The interpreter split is load-bearing. Server on 3.14, FreeCAD on its bundled 3.11, never one process. | all | Embedding, in-process addons, and shared type objects passed by reference. |
| AD-7 | Every bridge call is serialised by a single lock. | all | A second unserialised call path added alongside the locked one. |
| AD-8 | Nothing unmarshallable crosses the boundary, in either direction. `None` is sent as an empty value and read as absence on the far side. | all | `None` as an optional argument; unknown objects marshalled to an empty struct. |
| AD-11 | FreeCAD's output goes to a log file. Never a pipe, never discarded. | all | The pipe-fill deadlock, or the DEVNULL silence. Both are regressions of the same decision. |
| AD-12 | Identity is verified after connecting: the bridge reports its pid; the launcher compares that pid against the process it spawned. | all | Adopting whatever FreeCAD already answers on that port. |
| AD-13 | Tooling is versioned per file, not per run. The 3.11 file is excluded from the default ruff and mypy runs and checked separately at `py311`. | all | A contributor running `ruff format .` and shipping a bridge that cannot start. |
| AD-14 | The test suite crosses each boundary at least once — process, protocol, packaging. | all | The suite staying green through defects that exist only *between* components. |
| AD-15 | Headless only, FreeCAD 1.1 only, loopback-only bind. | all | A GUI path appearing without tests that exercise it; a bind address widened off loopback. |

Scoped to a capability. These matter only to the feature that touches them.

| id | decision | binds | prevents |
| --- | --- | --- | --- |
| AD-4 | Every tool returns a value and never raises. Failures cross the boundary as `{error, kind, hint}`. | CAP-6, CAP-7 | One unit choosing exceptions and another choosing returns; a raised `MCPError` subclass reaching the client as a dropped request. |
| AD-5 | No tool returns a bare list. Lists are wrapped under a named key. | CAP-1, CAP-4 | A result list being consumed as a sequence of content blocks, of which only the first survives. |
| AD-6 | Tool return annotations are widened to `Any`. | CAP-6 | A new tool's annotation deriving an output schema that rejects the error payload at runtime. |
| AD-9 | Bridge fault codes are a contract: an allocated range from 100, mapped one-to-one onto client exception types. Every `kind` must carry a recovery hint. | CAP-6 | Two features inventing overlapping codes; code 1 naming a specific condition when `xmlrpc` uses it for every uncaught exception. |
| AD-10 | Correctness is decided by FreeCAD, not by the client. Object types and their properties are probed at runtime; results are checked for bodies, not merely for non-null shapes. | CAP-1, CAP-4 | A hardcoded type list wrong for the installed build; a valid-but-empty result read as success. |
| AD-16 | Fault codes are allocated from one registry. A test asserts the set of `FAULT_*` constants in `_freecad_bridge.py` and the client's code→exception mapping are the same set, that the mapping is injective, and that every `kind` carries a hint. | CAP-6 | A new `FAULT_*` constant added on the bridge side with no client case, so it silently degrades to the generic internal error; or two codes collapsing onto one `kind`. |
| AD-17 | A compound state change happens inside **one** bridge call. A read-modify-write spread across two tool calls is not atomic and must not be relied on. | CAP-2, CAP-3 | A caller interleaving `get_properties` → `set_property` → `get_properties` across two worker threads and reading a half-applied object. |
| AD-18 | Exchange has a single owner: `export_object`. A new format extends its suffix table; a second export tool is a violation. | CAP-5 | Two features each adding an export path, so the same format exports with different options or different failure behaviour. |
| AD-19 | A placement is exactly nine floats: `x, y, z`, then `axis_x, axis_y, axis_z, angle`. Rotation is axis-and-angle in degrees, not Euler angles. | CAP-2 | Two units each choosing a rotation convention — degrees vs radians, axis-angle vs yaw-pitch-roll — that each parses but on which the two units disagree. |
| AD-20 | A function that launches a process owns it until it returns successfully. Every failure path must stop it first, because the caller receives an exception and never gets a handle to clean up. | CAP-7, CAP-8 | An orphaned FreeCAD per failed start, still bound to its port, so the next attempt fails identically — a leak that compounds instead of clearing. |
| AD-21 | `linear_array` produces a fused set of static copies, permanently. Draft's workbench is not loaded to obtain parametric behaviour. The tool description must say so, because a caller who edits the source and expects the array to follow is wrong in a way nothing else will correct. | CAP-2 | One unit loading Draft to make its arrays parametric and another fusing copies, so the same tool name means two different things. |
| AD-22 | An argument that can terminate FreeCAD rather than raise must be validated **before** the call that would crash. A caller-supplied enum is checked against a known set, not passed through to a native constructor and hoped for. | CAP-6, CAP-7 | A bad argument killing the bridge process, taking every open document with it and leaving nothing to report the error. Found the hard way: `Sketcher.Constraint` with six arguments terminates FreeCAD; with four it returns. |

## Boundary contract

What may cross the port, and what may not. This is the whole surface.

| | allowed across | forbidden across |
| --- | --- | --- |
| **values** | str, int, float, bool, list, tuple, dict of those, XML-RPC `DateTime` | `None`, free-form objects, anything requiring an import on the far side |
| **errors** | `(code, message)` pairs from the allocated 100-range — the wire form inside the `{error, kind, hint}` envelope of AD-4; code 1 reserved for the generic case | an exception crossing as an object; a code outside the range |
| **identifiers** | document names, object names, object types, 1-based edge indices — strings, resolved on the FreeCAD side | Python object references, handles, `App.Document` instances |
| **placements** | nine floats, degrees (AD-19) | a `Placement` value |

Everything else is FreeCAD's job to interpret. A tool that wants richer
arguments introduces a **serialisation helper inside `_freecad_bridge.py`**,
never a new cross-boundary type.

## Ownership of shared state

FreeCAD owns the document tree. The server owns nothing that FreeCAD also owns.
Concretely — and these are the decisions two units would each get wrong on their
own:

- **Documents are the unit of identity.** They are addressed by name, not by
  handle, and are created idempotently. No second registry of document names
  exists on the server side.
- **Every modelling operation is additive-then-reflective.** A boolean, fillet,
  chamfer, mirror, or array keeps its inputs and returns a new object. Nothing
  mutates an existing object in place except `set_placement` and
  `set_property`, which mutate by explicit request. Two features that each
  "modify" an object therefore do not silently fight over it.
- **The restart notice is read-and-clear state the server owns.** It describes
  the server's relationship to its process, not anything FreeCAD holds, so it
  lives on the server side and is consumed once.
- **The log file is owned by the launcher**, named by port so two instances do
  not interleave, and read only on non-zero exit.

## Verification

An implementation is not done until each seam it touches is exercised at the
seam, not around it.

| check | a test must actually do this |
| --- | --- |
| **process seam** | kill FreeCAD mid-session, then confirm reconnect, the restart notice, and continued usability |
| **process leak** | after a *rejected* start — a busy port, an unreachable bridge — assert no FreeCAD is left running, by pid (AD-20). A leak is invisible to a test that only checks the raised error, and each run inherits the last one's orphan |
| **protocol seam** | speak real stdio JSON-RPC to the server, and check the wire result, not the in-process return |
| **packaging seam** | install the built wheel and confirm `_freecad_bridge.py` ships and is launchable by path |
| **3.11 file** | `ast.parse(..., feature_version=(3, 11))` over `_freecad_bridge.py` |
| **import rule** | assert no package module imports `_freecad_bridge`, and no other module contains `import FreeCAD` |
| **fault codes** | `test_fault_map_matches_the_bridge_source` — the bridge's `FAULT_*` set equals the client's mapping set in both directions, the mapping is injective, every `kind` has a hint (AD-16) |
| **FreeCAD semantics** | assert measured values from a real headless FreeCAD — never values derived on paper |

## Conventions

Rules that bind code style and consistency rather than correctness. Where one of
these turns out to be load-bearing, promote it to an AD.

- Tool names and their arguments are a stable public surface. Adding a tool is
  additive; changing an existing one's meaning is a breaking change to CAP-1
  through CAP-5.
- A wrapped list's key is the plural noun naming the thing returned —
  `objects`, `documents` — so a caller can predict the key without reading the
  tool.
- Dimensional values are `{"value": float, "unit": str}` in both directions, so
  a value read from `get_properties` can be passed straight back to
  `set_property`.
- The bridge script's filename is underscore-prefixed to mark it as not part of
  the importable surface; that is load-bearing, not cosmetic. A future split
  into several 3.11 files inherits the AD-13 exclusions and the AD-1/AD-2
  no-import rule — code organisation, not a boundary change.
- A failure that produces a *plausible but wrong* result is an error, not a
  result. This covers empty intersections, fillets that cannot be fitted, and
  section planes that miss the shape.

## Deferred

Nothing remains. Each row that was here has been settled, and the ones that
were assumptions are now confirmed facts rather than untested positions.

Settled rather than deferred, and now binding:

- **Single user, single machine, single caller.** Confirmed by the maintainer
  rather than assumed by a build. This is what makes AD-15's loopback-only bind
  a settled constraint rather than a provisional one, and it is why
  `start_headless` needs no per-port lock: nothing else races it for the port.
  The one process-ownership rule that does apply is AD-20, which stands
  regardless of caller count.
- **Parametric arrays are not coming.** `linear_array` stays a fused set of
  static copies; Draft's workbench is not loaded to obtain parametric
  behaviour. See AD-21.
- **No GUI target.** AD-15 is absolute, not provisional: a GUI path is out of
  scope, not merely untested.
- **No publishing.** Local installation is the end state, so there is no
  release workflow and no support policy to maintain.

Revisiting any of these is a scope change, not a clarification, and belongs in a
new AD with its reasoning rather than an edit to this section.

## Versions

Verified against the live interpreters, not recalled. Patch pins are the
fastest-decaying content here — re-verify rather than trust them, and keep the
`mcp` note, which is a trap rather than a pin.

| component | version | note |
| --- | --- | --- |
| server interpreter | CPython 3.14.3 | |
| FreeCAD | 1.1.3, bundling CPython 3.11.14 | the split in AD-3 is this exact pair |
| `mcp` | 2.2.0 | `FastMCP` was renamed `MCPServer` in 2.x; the v1 `FastMCP` import raises `ModuleNotFoundError` with a migration-guide URL |
| `ruff` | 0.16.9 | one target version per run — the reason for AD-13 |
| `mypy` | 2.3.1 | strict; excludes the 3.11 file |
| `pytest` | 9.1.1 | `--timeout=120` |
| `pytest-timeout` | 2.4.0 | |
| build backend | `uv_build` | |
