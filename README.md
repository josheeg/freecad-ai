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
| `add_primitive` | Add a Part primitive (`Part::Box`, `Part::Cylinder`, …) |
| `list_objects` | Objects in a document — returns `{"objects": [...]}` |
| `get_properties` / `set_property` | Read and write object properties |
| `remove_object` | Remove an object |
| `boolean_op` | cut, fuse or common two objects into a new feature |
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

## Development

```bash
uv run pytest                      # everything
uv run pytest -m "not integration" # skip tests that start FreeCAD
uv run ruff check .
uv run mypy
```

`mypy` runs in strict mode. Integration tests launch a real headless FreeCAD
1.1 process, so a working install is required for the full suite.

## Architecture

```
src/freecad_ai/_freecad_bridge.py  runs under FreeCAD's bundled Python 3.11;
                                  the only file permitted to import FreeCAD,
                                  launched by path and never imported
src/freecad_ai/bridge.py           XML-RPC client and process launcher (3.14)
src/freecad_ai/server.py           MCP server, 14 tools over stdio
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
shape_summary("bracket", "Drilled")   # volume 3149.73 (3200 plate − 50.27 hole)
export_object("bracket", "Drilled", "C:/parts/bracket.step")
```

## Security

The bridge is unauthenticated XML-RPC bound to loopback only. Anyone who can
reach the port has full control of FreeCAD. Do not widen the bind address.
