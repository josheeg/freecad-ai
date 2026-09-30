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
| `new_document` / `open_document` / `save_document` | Document lifecycle |
| `list_documents` | List open documents |
| `add_primitive` | Add a Part primitive (`Part::Box`, `Part::Cylinder`, …) |
| `list_objects` | Objects in a document, with name, label and type |
| `get_properties` / `set_property` | Read and write object properties |
| `remove_object` | Remove an object |
| `shape_summary` | Volume, area and bounding box |

Dimensional properties are reported as `{"value": 10.0, "unit": "mm"}` rather
than bare numbers, and a value read back with `get_properties` can be passed
straight to `set_property`.

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
bridge/freecad_bridge.py   runs under FreeCAD's bundled Python 3.11;
                           the only file permitted to import FreeCAD
src/freecad_ai/bridge.py   XML-RPC client and process launcher (3.14)
src/freecad_ai/server.py   MCP server, 11 tools over stdio
```

The FreeCAD-side script lives outside `src/` so the interpreter boundary is
enforced by the package layout rather than by convention.

## Security

The bridge is unauthenticated XML-RPC bound to loopback only. Anyone who can
reach the port has full control of FreeCAD. Do not widen the bind address.
