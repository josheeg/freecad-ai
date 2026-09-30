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

### FreeCAD's output streams must stay DEVNULL

FreeCAD writes `Recompute......` progress output continuously. Capturing it
with `subprocess.PIPE` and never reading fills the ~64KB pipe buffer; FreeCAD
then blocks in `write()` and stops answering the bridge. The process stays
alive, so this presents as a hang rather than a crash, and only a heavy
enough workload reaches the limit.

Enforced by `test_freecad_output_streams_are_never_piped`.

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

## Tooling

`mypy` excludes `_freecad_bridge.py`: it runs under FreeCAD's bundled 3.11,
so strict 3.14 checking does not describe it. `ruff` still lints it. The
reason is recorded in `pyproject.toml` alongside the setting.
