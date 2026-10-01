# Gotchas: the FreeCAD bridge and the MCP server layer

Read this when adding or changing a bridge function, a tool, or the server
layer. `AGENTS.md` points here rather than carrying these inline, because they
are expensive to load on every session and cheap to look up when they apply.

Every entry below is a behaviour that was measured or diagnosed on this
project, not a general caution.

## The MCP server layer

### A tool must never raise

MCPServer 2.2 handles `except MCPError: raise` *before* its generic
`except Exception` branch, and `ToolError` subclasses `MCPError`. So the
`is_error=True` result path is unreachable for a `ToolError` — raising one
drops the request instead of answering it.

Return `{"error", "kind", "hint"}` instead. See `_tool` in
`src/freecad_ai/server.py`.

Enforced by `test_non_bridge_exceptions_are_not_swallowed` and
`test_bridge_error_becomes_a_result_not_an_exception`.

### A tool returning a list is silently truncated

`_convert_to_content` treats a list as a *sequence of content blocks* and
chains them. Three objects become three text blocks, and `content[0]` — what
clients read — holds only the first. No error is raised.

Every tool must return a dict or a string. `_tool` wraps stray lists as
`{"items": ...}` as a backstop.

Enforced by `test_no_tool_returns_a_bare_list` and
`test_list_results_survive_server_conversion`.

### A return annotation becomes an output schema

MCPServer derives an output schema from the tool's return annotation and
validates every result against it. A tool annotated `-> list[dict]` rejects
the dict error payload as a type mismatch and resurfaces as
`UnexpectedToolError` — the exact symptom the error handling was meant to
remove.

`_tool` therefore publishes `__signature__` with a widened return. Do not
remove it.

Enforced by `test_tool_return_type_is_widened`.

### Parallel tool calls arrive on different threads

MCPServer runs *synchronous* tool functions on anyio's worker thread pool, so
concurrent calls reach the shared `ServerProxy` from several threads. Its
single `HTTPConnection` cannot serve overlapping requests and raises
`CannotSendRequest` or `ResponseNotReady` rather than queueing — every
parallel call fails.

`Bridge` holds a `threading.Lock` per call. FreeCAD's `SimpleXMLRPCServer` is
single-threaded too, so this matches the server rather than fighting it.

Enforced by `test_bridge_serialises_calls`,
`test_concurrent_calls_do_not_interleave` and
`test_concurrent_calls_from_many_threads_all_succeed`.

## Process lifecycle

### FreeCAD's output must not go on a pipe

FreeCAD writes `Recompute......` progress output continuously. Capturing it
with `subprocess.PIPE` and never reading fills the ~64KB pipe buffer; FreeCAD
then blocks in `write()` and stops answering the bridge. The process stays
alive, so this presents as a hang rather than a crash, and only a heavy
enough workload reaches the limit.

It goes to a log file, not `DEVNULL`. `DEVNULL` avoided the deadlock but made
startup failures undiagnosable — a bridge that could not start reported only
"did not become ready within 30s". The log path is attached to the process as
`freecad_ai_log`, read with `bridge_log_tail`, and included in the error when
FreeCAD exits non-zero.

### A busy port must be an error, not a silent adoption

If another FreeCAD holds the port, the one launched here cannot bind and the
readiness ping answers from the incumbent. Adopting it hands the model that
session's open documents. `start_headless` compares the bridge's
`instance_id` pid against the process it spawned and raises `PortInUse`.

`freecad-robust-mcp-server` also uses 9875, so this collides in practice.

Enforced by `test_refuses_to_adopt_a_foreign_freecad`.

### Shutdown depends on the server exiting normally

The MCP server stops FreeCAD from an `atexit` handler. On Windows
`terminate()` is `TerminateProcess`, which skips `atexit` — the FreeCAD
process is orphaned and keeps holding its port, and the next server on that
port fails with `PortInUse`.

Close the server's stdin rather than terminating it. See `close()` in
`tests/test_boundaries.py`.

## The bridge protocol

### Fault codes start at 100

`xmlrpc.client` serialises any uncaught server exception as `faultCode 1`, so
code 1 must not name a specific condition or an ordinary `TypeError` gets
reported as one. The `FAULT_*` constants in `_freecad_bridge.py` are part of
the contract with `_FAULT_MAP` in `bridge.py`; keep the two in sync.

Enforced by `test_fault_codes_map_to_typed_errors`.

### XML-RPC cannot marshal None, and marshals junk to `{}`

A non-marshallable value does not raise — it becomes an empty struct, so the
failure looks like a well-formed response carrying no data. The dispatch
function must *call* the target and forward `*args`; returning the function
object itself is silently wrong.

Properties whose value is None are omitted from responses rather than sent.
A missing key means "no value".

### Dimensions are Base.Quantity, not float

An `isinstance` filter for `(int, float, str, bool)` drops every dimension
without complaint. Unwrap with `.Value`, and get a clean unit symbol from
`getUserPreferred()[2]` — `str(Quantity.Unit)` yields
`Unit: mm (1,0,0,0,0,0,0,0) [Length]`.

Enforced by `test_property_value_round_trips`.

### A Placement cannot be sent as a dict

`set_property` with a dict raises `type must be 'Matrix' or 'Placement', not
dict`. Send the components and let the bridge rebuild it — see `set_placement`.

### There is no Part::Boolean in FreeCAD 1.1

Each operation is its own parametric feature type: `Part::Cut`, `Part::Fuse`,
`Part::Common`, each with `Base` and `Tool` links.
`addObject("Part::Boolean", …)` raises `No document object found`.

`Part::Tube` is likewise absent on 1.1.3 — `list_primitive_types` probes the
running FreeCAD rather than asserting a hardcoded list, so do not hardcode
one either.

### Fillet and chamfer take Base as the object, not a sub-element tuple

`Part::Fillet.Base` is a plain `App::DocumentObject`. Assigning the usual
`(obj, ["Edge1"])` sub-element tuple raises `Type must be App::DocumentObject
or None, not tuple`. The sub-element names live in `EdgeLinks`, which FreeCAD
fills in itself, and the radii go in `Edges` as
`(index, start_radius, end_radius)` tuples with a **1-based** index.

`Part::Chamfer` takes the same shape, with the chamfer size in both positions.

### Edge indices are only useful if something can list them

`Edges` refers to edges by position, which means nothing to a caller. That is
why `describe_geometry` exists: it reports `Edge1`, `Edge2`, … exactly as
FreeCAD names them, with type, length and endpoints.

### Arrays are not available headlessly

`Part::Array`, `Part::OrthoArray` and `Draft::Array` all raise
`is not a document object type` in `freecadcmd`, and `import Draft` does not
register them. `linear_array` is therefore built from a `Part::MultiFuse` of
translated copies, which needs no workbench. The copies become static
`Part::Feature` objects rather than a parametric array.

## Sketches

### An unclosed profile extrudes to a wrong solid, not an error

`Part::Extrusion` with `Solid=True` over an open wire returns a *shape*. Probed:
a five-edge profile with a mis-spanned arc came out not closed, and the result
had a 50mm edge where 20mm had been drawn. `_profile_wire` checks closedness
explicitly and names the endpoints that fail to meet; `extrude_sketch` and
`sketch_to_face` both refuse rather than attempt it.

Enforced by `test_open_profile_is_refused_rather_than_extruded` and
`test_an_open_profile_makes_no_face`.

### `Sketcher.Constraint` with six arguments terminates FreeCAD

The value form applied to a two-element constraint does not raise — it *kills
the interpreter*, taking every open document with it. The four-argument form
returns normally. `add_sketch_constraint` picks the arity per constraint kind
and rejects an unknown `kind` against a known set before constructing anything.
This is AD-22 in the spine.

Enforced by `test_unknown_constraint_kind_is_refused_without_crashing`.

### A `PosId` of 0 crashes; a `PosId` of 4 is silently accepted

The position argument is validated against `{1, 2, 3}` — start, end, mid — and
the two out-of-range cases fail *differently*, which is why the safe set rather
than the legal set governs (AD-28):

- `PosId 0` is FreeCAD's internal `none`. It is a real PosId, used by SKETCHER
  for whole-element constraints, and passing it to a two-element constraint
  **terminates the interpreter**. It looks safe because it is legal.
- `PosId 4` and `99` are neither valid nor fatal. They are accepted silently and
  leave the solver reporting a **negative dof** — the silent-wrong-result class,
  which is harder to notice than the crash.

Probed rather than assumed; the values that build cleanly are 1, 2 and 3.

Enforced by `test_constraint_positions_are_validated` and
`test_valid_constraint_positions_are_accepted` — the second exists so the guard
cannot degenerate into refusing everything.

### Removing geometry renumbers it, and leaves names behind

`delGeometry` shifts every later element down one: with two elements, deleting
the first moves the survivor from index 1 to index 0. FreeCAD knows nothing
about names this surface stores, so **the mapping must be reindexed in the same
call** or a name comes to point at the wrong element (AD-30).

Constraint names are the opposite case and need no help: `renameConstraint`
attaches the name to the constraint, so deleting an earlier constraint leaves
the later name on the right one. Verified both ways.

Enforced by `test_a_name_survives_the_removal_that_renumbers_it` and
`test_a_constraint_name_follows_the_constraint_not_the_index`.

### XML-RPC cannot carry `None`

The server runs with `allow_none=False`, so a `None` argument raises
`TypeError: cannot marshal None` *on the client*, before anything is sent. An
absent optional name travels as an **empty string** instead, which the bridge
treats as "no name". Whitespace-only is still refused, because unlike `""` it
looks like a name that was meant to be something.

The same trap catches an int-keyed dict in a *result*: `{"geometry": 1}` fails
to marshal, so name mappings are reported as a list of objects.

Enforced by `test_an_omitted_name_means_none_at_all`.

### `sketch_status`'s area depends on constraints, not coordinates

Re-driving a named `Distance` makes the solver **move** the geometry. Without
`Horizontal`/`Vertical`/`Coincident` constraints, four independent lines stretch
into a shape that no longer closes, and area falls to 0.0 — correct, not a bug.
Four `Coincident` plus two `Distance` constraints give a closed but skewed
quadrilateral (area 950.7 at 60x20 rather than 1200), because nothing pins the
corners square. `dof` and `fully_constrained` are what tell the two apart.

### A sketch's `Shape.Area` is 0.0

A wire encloses no area, so a closed profile still reports 0.0. Get area from
`Part.Face(Part.Wire(edges))`, which is what `sketch_status` does and what
`sketch_to_face` makes permanent.

### `Part.Edge` has no `.Name` on 1.1

`Edge1`, `Edge2` … are **synthesized by the bridge** from index position, not
read off the edge. Reading a name raises `AttributeError`.

### Attachment uses `AttachmentSupport`, not `Support`

`Support` is the FreeCAD 0.x name and raises on 1.1. The property pair is
`AttachmentSupport` plus `MapMode`, and only a `Part.Plane` face will take
`FlatFace` — a cylinder's side face is a `Cylinder` surface and is refused by
name.

### An extrusion captures the sketch's placement when it is created

Moving a sketch afterwards does not move a solid already extruded. Position
first, then extrude.

### NaN and infinity terminate FreeCAD

Every comparison-based guard is transparent to NaN: `nan == 0`, `nan < 0` and
`nan <= 0` are all `False`, so `if length <= 0` does not fire and the value
reaches `LengthFwd`. FreeCAD does not reject it either — probed against 1.1.3,
`Part::Extrusion` with `LengthFwd = nan` **killed the interpreter mid-script**,
taking every open document with it. `json` and `xmlrpc` both marshal NaN
happily, so this is reachable from ordinary input.

Refused at the two choke points every dimension passes through: `_dim` in
`bridge.py` before the round trip and `_number` in `_freecad_bridge.py` after
it. `math.isfinite` is the check; comparing is not. AD-25.

Enforced by `test_non_finite_dimensions_are_refused`.

### Creating an object before validating it leaves an orphan

`_feature(name, kind, document)` adds the object to the document immediately.
Anything checked after that runs with a half-built feature already present, so
a refusal leaves it behind under the caller's own name: `list_objects` shows
it, `describe_geometry` on it raises `NO_SHAPE`, and a retry under the same
name collides.

Found twice. `sketch_to_face` was fixed by building and validating the face
first. `extrude_sketch` kept the old order for a while, and both of its
post-recompute checks — no solids, more than one solid — ran after creation;
a self-intersecting bowtie left `['Bow', 'Orphan']` in the document. Validation
that cannot happen before creation is followed by rollback, not hope. AD-26.

Enforced by `test_a_failed_face_leaves_nothing_behind` and
`test_a_failed_extrude_leaves_nothing_behind`. Note the fixture: an *open*
profile is refused by `_profile_wire` before anything is created, and a zero
depth by the length check, so neither can orphan anything. A self-intersecting
profile is the only path that reaches the post-create checks.

### int(True) is 1, so a bare int() is a destructive edit

`int()` in the client converts before the wire, so a bool or a numeric string
becomes a plausible index and the bridge's own guard never sees it.
`remove_sketch_geometry(doc, "S", True)` silently deleted geometry 1. Use
`_index()` — which also refuses floats and strings — and `_dim()` for
coordinates. AD-22's rule applied to types rather than to values.

Enforced by `test_index_arguments_reject_bools_and_strings`.

### Draft is unavailable headlessly

Every `Draft::*` type — `Wire`, `Rectangle`, `Circle`, `Polygon`, `BSpline`,
`Ellipse`, `Shape2D` — raises `TypeError` on `addObject`. No capability may
depend on Draft. This is the same conclusion AD-21 reached for arrays, now
measured rather than assumed.

## Tooling

`_freecad_bridge.py` runs under FreeCAD's bundled 3.11, so it is excluded
from the default ruff and mypy runs and checked separately. The mypy reason
is recorded in `pyproject.toml`.

**The ruff exclusion is not optional.** Ruff infers `target-version` from
`requires-python` (3.14) and takes one value for the whole run. Under 3.14
`except A, B:` is valid — PEP 758 — and `ruff format` *prefers* that
unparenthesised form, rewriting correct code into a 3.11 syntax error:

```
Exception while processing file: _freecad_bridge.py
[('multiple exception types must be parenthesized', ..., 661, 12,
  '    except AttributeError, IndexError:\n', 661, 38)]
```

The failure looks like a FreeCAD problem, not a formatting one, and it is
silent unless the bridge's output is being kept — see the log file above.
`test_bridge_script_parses_as_python_3_11` catches it with
`ast.parse(..., feature_version=(3, 11))`.
