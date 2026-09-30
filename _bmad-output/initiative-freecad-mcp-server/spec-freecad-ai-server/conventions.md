# Conventions

Rules an implementing agent must follow that are not visible from reading
the code. Each exists because breaking it produces a wrong result rather than
an error.

## Layout

```
src/freecad_ai/_freecad_bridge.py   runs under FreeCAD's bundled 3.11; the only
                                    file permitted to import FreeCAD, and
                                    never imported by the package
src/freecad_ai/bridge.py           XML-RPC client and process launcher (3.14)
src/freecad_ai/server.py           MCP server, 24 tools over stdio
```

The bridge script is launched by path, not imported. Importing it into the
server process would pull FreeCAD's 3.11-only modules into 3.14. Tests assert
both halves: no package module imports it, and no other module contains
`import FreeCAD`.

It lives *inside* the package so it ships in the wheel. An installed package
has no project root, so any path derived by walking out of `__file__` resolves
into `site-packages`' parent.

## Tool layer

- **Never raise from a tool.** Return `{error, kind, hint}`. MCPServer
  handles `except MCPError: raise` before its generic branch, and `ToolError`
  subclasses `MCPError`, so the `is_error=True` path is unreachable for it and
  the request is dropped.
- **Never return a bare list.** The result conversion treats a list as a
  sequence of content blocks and chains them, so only the first element
  reaches the client. Wrap it.
- **Only `BridgeError` is converted.** Anything else is a bug and must
  surface.
- The decorator publishes `__signature__` with a widened return. MCPServer
  derives an output schema from the return annotation and validates every
  result against it, which would reject the error payload.

## Bridge layer

- **Serialise every call.** A lock per call. MCPServer runs synchronous tools
  on a worker thread pool, and the shared `ServerProxy` cannot serve
  overlapping requests.
- **Never pass `None` across the bridge.** This proxy runs with
  `allow_none=False`. Send an empty value and read its absence on the far
  side.
- **Fault codes start at 100.** `xmlrpc` serialises any uncaught server
  exception as code 1, so 1 must map to a generic error.
- **The dispatch function must call its target and forward `*args`.** An
  unmarshallable return becomes an empty struct rather than raising, so
  returning the function object is silently wrong.
- **Verify the pid after connecting.** A busy port answers from the incumbent.

## FreeCAD-facing code

- **Probe, never assume.** Object types and their properties differ between
  builds — 1.1.3 has no `Part::Boolean` and no `Part::Tube`. Discover at
  runtime.
- **Dimensions are `Base.Quantity`,** not float. `isinstance` filters against
  `(int, float, str, bool)` drop every dimension without complaint.
- **Check for a body, not just a shape.** A `Compound` from a boolean is
  valid and non-null while holding no solids.
- `freecadcmd` imports its script rather than executing it, so `__name__` is
  the filename and a `__main__` guard does nothing. It also passes the script
  path as `sys.argv[1]`, so user arguments start at index 2.

## Tooling

- The bridge script is **excluded from the default ruff run** and checked with
  `--target-version py311`. Ruff takes one target version per run, inferred
  from `requires-python`; under 3.14 `except A, B:` is valid (PEP 758) and
  the formatter prefers that form, rewriting correct code into a 3.11 syntax
  error. `mypy` excludes it for the same underlying reason.
- Tests must cross the process, protocol and packaging boundaries at least
  once each. Every defect found so far lived at such a seam.
- Measured values, not derived ones, belong in tests and documentation.

## Instructions

`AGENTS.md` carries a small always-loaded block. Traps that have acquired a
regression test belong in `docs/mcp-server-gotchas.md` behind a task
trigger, named by the test that guards them — not inline, where they are paid
for in every session.
