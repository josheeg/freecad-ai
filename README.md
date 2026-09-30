# freecad-ai

An MCP server that lets an AI assistant drive FreeCAD 1.1.

The server runs on its own Python and reaches FreeCAD over an XML-RPC bridge,
so the two never share an interpreter. This is deliberate: FreeCAD links
`python311.dll` and cannot be imported from any other Python, so the process
boundary is the design rather than a workaround.

## Requirements

- Python 3.14 (`uv` manages the environment)
- FreeCAD 1.1 at `C:\Program Files\FreeCAD 1.1`
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Running

```bash
uv run freecad-ai
```

The server speaks MCP over stdio. Configure it with any MCP client:

```json
{
  "mcpServers": {
    "freecad-ai": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\joshe\\Desktop\\freecad-ai", "run", "freecad-ai"]
    }
  }
}
```

FreeCAD starts on the first tool call, not at server startup, so the MCP server
itself launches instantly. It runs headless — no GUI instance is started.

## Tools

| Tool | Purpose |
| --- | --- |
| `connect` | Start FreeCAD if needed and report its version |
| `new_document` | Create a document, or return the existing one (`reuse=False` to insist) |
| `open_document` / `save_document` | Document lifecycle |
| `list_documents` | List open documents — returns `{"documents": [...]}` |
| `list_primitive_types` | Part types this FreeCAD can create, with their properties |
| `add_primitive` | Add a Part primitive (`Part::Box`, `Part::Cylinder`, …) |
| `list_objects` | Objects in a document — returns `{"objects": [...]}` |
| `describe_geometry` | Edges and faces with their FreeCAD names, types and positions |
| `get_properties` / `set_property` | Read and write object properties |
| `remove_object` | Remove an object |
| `boolean_op` | cut, fuse or common two objects into a new feature |
| `fillet` | Round edges, given 1-based indices from `describe_geometry` |
| `chamfer` | Cut edges flat |
| `mirror` | Reflect through a plane given by a point and a normal |
| `linear_array` | Repeat along a line, fusing the copies |
| `set_placement` | Move and rotate an object |
| `shape_summary` | Volume, area and bounding box |
| `export_object` | Write an object to `.step`, `.stl`, `.iges`, `.obj` or `.brep` |

Dimensional properties are reported as `{"value": 10.0, "unit": "mm"}` rather
than bare numbers, and a value read back with `get_properties` can be passed
straight to `set_property`.

### Errors

A failing tool returns a result carrying the failure rather than crashing:

```json
{
  "error": "no such document: ghost",
  "kind": "DocumentNotFound",
  "hint": "Call `new_document` with that name first."
}
```

`kind` is a stable class name and `hint` says what to do next, so an agent can
recover instead of guessing. Most failures are a missing document, object or
property — not a broken server.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `FREECAD_AI_PORT` | `9875` | Bridge port |
| `FREECAD_AI_HOST` | `127.0.0.1` | Bind address — **leave it on loopback** |
| `FREECAD_AI_FREECAD_BIN` | `C:\Program Files\FreeCAD 1.1\bin` | Where `freecadcmd.exe` lives |

The bridge is unauthenticated. Anyone who can reach the port has full control
of FreeCAD, so do not widen the bind address.

If the port is already taken by another FreeCAD, startup fails with
`PortInUse` rather than connecting to that instance — silently adopting
someone else's session would hand the model their open documents. Note
`freecad-robust-mcp-server` also defaults to 9875.

## Development

If [`just`](https://github.com/casey/just) is installed, these wrap the same
commands CI runs:

```bash
just check             # the gate a commit must pass
just test              # everything, then a leaked-process check
just test-integration  # only the tests that drive a real FreeCAD
just format            # reformat, including the bridge at its own target
```

Without it, the raw commands are:

```bash
uv run pytest                      # everything, ~25s (starts a real FreeCAD)
uv run pytest -m "not integration" # only the tests that need no FreeCAD
uv run ruff check .
uv run ruff format .               # CI enforces `ruff format --check`
uv run mypy
```

The FreeCAD-side bridge is linted and formatted separately, because it runs
under FreeCAD's bundled Python 3.11 while everything else targets 3.14:

```bash
uv run ruff check   --target-version py311 src/freecad_ai/_freecad_bridge.py
uv run ruff format --target-version py311 src/freecad_ai/_freecad_bridge.py
```

`mypy` runs in strict mode over `src/` and `scripts/`. Integration tests launch
a real headless FreeCAD 1.1 process, so a working install is required for the
full suite.

If a test run is interrupted, a `freecadcmd` can be left holding a port, which
makes the next run fail for the wrong reason. `just post-test` reports that and
fails; `just kill-freecad` clears it. It matches on the bridge script path, so
a FreeCAD you are using through the GUI is never touched.

## Architecture

```
src/freecad_ai/_freecad_bridge.py  runs under FreeCAD's bundled Python 3.11;
                                  the only file permitted to import FreeCAD,
                                  launched by path and never imported
src/freecad_ai/bridge.py           XML-RPC client and process launcher (3.14)
src/freecad_ai/server.py           MCP server, 34 tools over stdio
scripts/freecad_procs.py           reports or stops leaked FreeCAD processes
```

The FreeCAD-side script lives inside the package so it ships in the wheel — an
installed package has no project root to resolve a sibling directory against.
It is still never imported by the server: tests assert that no package module
references it, which is what keeps FreeCAD's 3.11-only modules out of the 3.14
process.

### Worked example

```python
new_document("bracket")
add_primitive("bracket", "Part::Box", "Plate", {"Length": 40, "Width": 20, "Height": 4})
add_primitive("bracket", "Part::Cylinder", "Hole", {"Radius": 2, "Height": 10})
set_placement("bracket", "Hole", 10, 9, -3)
boolean_op("bracket", "Plate", "Hole", "cut", "Drilled")
shape_summary("bracket", "Drilled")  # volume 3149.73 (3200 plate − 50.27 hole)
export_object("bracket", "Drilled", "C:/parts/bracket.step")
```

Rounding an edge needs to know which edge is which, so ask first:

```python
describe_geometry("bracket", "Drilled")  # {"edges": [{"name": "Edge1", ...}], ...}
fillet("bracket", "Drilled", "Rounded", edges=[1, 3], radius=2.0)
shape_summary("bracket", "Rounded")  # volume 3142.87 (3149.73 − 6.87 of rounding)
```

`linear_array` is the one tool whose result is not parametric: it fuses static
copies, so editing the source afterwards does not update the array. Re-run it
to change the pattern.

### Sketches

Most real parts begin as a 2D profile, and the sketch tools go from a profile
to a solid. **Check `sketch_status` before `extrude_sketch`** — an unclosed
profile does not fail at extrude time, it produces a *wrong solid*, so the
server refuses one explicitly.

```python
add_sketch("bracket", "Profile")
# a 40x20 plate with 5mm rounded corners
add_sketch_line("bracket", "Profile", 5, 0, 35, 0)
add_sketch_arc("bracket", "Profile", 35, 5, 5, -90, 0)
add_sketch_line("bracket", "Profile", 40, 5, 40, 15)
add_sketch_arc("bracket", "Profile", 35, 15, 5, 0, 90)
add_sketch_line("bracket", "Profile", 35, 20, 5, 20)
add_sketch_arc("bracket", "Profile", 5, 15, 5, 90, 180)
add_sketch_line("bracket", "Profile", 0, 15, 0, 5)
add_sketch_arc("bracket", "Profile", 5, 5, 5, 180, 270)

sketch_status("bracket", "Profile")
# {"closed": true, "edge_count": 8, "area": 778.540, "dof": 16, ...}
#            W*H - (4 - pi)*r^2 = 800 - 21.46 = 778.54

extrude_sketch("bracket", "Profile", "Plate", depth=4.0)
measure("bracket", "Plate")
# volume 3114.16  (= 778.54 * 4), solid_count 1
```

A sketch can be placed on an existing face, so a profile follows a surface
rather than a plane the caller has to compute:

```python
add_primitive("bracket", "Part::Box", "Base", {"Length": 40, "Width": 20, "Height": 4})
attach_sketch_to_face("bracket", "Profile", "Base", "Face6")  # top face
extrude_sketch("bracket", "Profile", "Boss", depth=2.0)  # volume 1600, z 4..6
```

A closed profile can also become a real face, which has the area a wire
cannot have — a sketch's own area reads 0.0 — and then feeds the rest of the
surface like any other object:

```python
sketch_to_face("bracket", "Profile", "Face")
describe_geometry("bracket", "Face")  # face_count 1, area 778.540
```

Everything from there is the existing surface: `boolean_op`, `fillet`,
`chamfer`, `measure`, `export_object` all work on a sketch-derived solid with
no new arguments.

## Specification

The design decisions behind this server are written down rather than left in
commit messages, under `_bmad-output/initiative-freecad-mcp-server/`:

- **Spec** — `spec-freecad-ai-server/spec-freecad-ai-server.md`, with
  `tool-surface.md`, `failure-modes.md` and `conventions.md`. What the system
  does, and what it deliberately does not.
- **Spine** — `architecture-freecad-ai-server/architecture-freecad-ai-server.md`.
  Twenty-one numbered decisions, AD-1…AD-21, each naming the failure it
  prevents. These are binding on any change here.

`AGENTS.md` points at both, and a test asserts the counts in all three files
still agree, so they cannot quietly drift apart.

## Security

The bridge is unauthenticated XML-RPC bound to loopback only. Anyone who can
reach the port has full control of FreeCAD. Do not widen the bind address.
