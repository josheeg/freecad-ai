<!-- bmad:context -->
<!-- Verified 2026-09-29 against efcfbcc. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## freecad-ai

MCP server that lets an AI assistant drive FreeCAD 1.1. Python, `uv`, packaged with `uv_build`. Planning and specs use the BMad skills; artifacts land in `_bmad-output/`.

## Where things are

- Entry point: `src/freecad_ai/`, exposed as the `freecad-ai` console script via `freecad_ai:main`
- FreeCAD-side bridge is `src/freecad_ai/_freecad_bridge.py` — the only file allowed to `import FreeCAD`, and **never imported by the package**; it is launched by path as a script. It lives inside the package so it ships in the wheel (an installed package has no project root to resolve a sibling directory against), and tests enforce the never-imported rule.
- Target FreeCAD is 1.1 only — `C:\Program Files\FreeCAD 1.1`. Do not write code against 1.0 APIs.

## Running and verifying

- Prefix every Python tool with `uv run` (`uv run pytest`, `uv run ruff check .`, `uv run mypy src`) — none are installed globally, and bare invocations run against the wrong interpreter.
- `freecadcmd.exe` is not on `PATH`; call it by full path, `"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"`.
- Bridge code cannot be covered by unit tests alone — it needs a live `freecadcmd` process. Mark those tests `integration` and deselect them while iterating: `uv run pytest -m "not integration"`.

## Conventions that differ from defaults

- **Never `import FreeCAD` in server code.** FreeCAD links `python311.dll` and only imports cleanly inside its own bundled 3.11; from this project's 3.14 interpreter it fails with `Module use of python311.dll conflicts with this version of Python`. Reach FreeCAD only over the XML-RPC bridge.
- The server runs on the project's own Python (3.14) and never shares an interpreter with FreeCAD's bundled 3.11. That separation is the design — do not "simplify" it by embedding.
- Headless `freecadcmd` is the default and only target; no GUI path is implemented. Do not add one without tests that exercise it.
- The bridge is unauthenticated XML-RPC bound to loopback. Never widen the bind address — anyone who can reach the port controls FreeCAD outright.

## Known pitfalls

- Two FreeCAD versions are installed (`1.0` and `1.1`) with different bundled patch levels. An unqualified path picks the wrong one; always name 1.1 explicitly.
- FreeCAD's workbench loader does not run `Init.py` at startup, only `InitGui.py` module-level code. If bridge auto-start ever needs to live in this repo, that asymmetry governs where it goes.
- Headless `freecadcmd` on 1.1 has **no** `QApplication` *or* `QCoreApplication` instance, and `FreeCADGui`/`PySide6` still import cleanly — branch on `FreeCAD.GuiUp` alone, never on import success or app-instance presence. Starting bridge work before `GuiUp` is `True` races the Qt event loop and crashes FreeCAD with `SIGABRT`.
- FreeCAD is not on PyPI (verified: 404). Never add it as a dependency; it is an external process, not an import.
- `freecadcmd script.py` **imports** the script under the module name taken from its filename, so `__name__` is never `"__main__"`. An `if __name__ == "__main__"` guard silently does nothing: the process starts, defines everything, and exits without binding the port. See `_running_under_freecadcmd()` in `src/freecad_ai/_freecad_bridge.py`.
- Under `freecadcmd`, `sys.argv[1]` is the **script path**, so user arguments start at index 2. Reading from index 1 treats the script's own path as the host and the host as the port.
- FreeCAD dimensions are `Base.Quantity`, not `float`. An `isinstance` filter for `(int, float, str, bool)` drops every dimension silently; unwrap via `.Value` and `.getUserPreferred()`.
- FreeCAD 1.1 has **no `Part::Boolean`**. Booleans are separate feature types — `Part::Cut`, `Part::Fuse`, `Part::Common` — each with `Base` and `Tool` links. `addObject("Part::Boolean", …)` raises `No document object found`.
- A `Placement` is a FreeCAD object, not a primitive: passing a dict to `set_property` raises `type must be 'Matrix' or 'Placement', not dict`. Send components and reassemble — see `set_placement`.
- `_freecad_bridge.py` runs under FreeCAD's bundled 3.11, so `mypy` excludes it (strict 3.14 checking does not describe it). `ruff` still lints it.
- XML-RPC cannot marshal `None`, and marshalling a non-marshallable object (a function, say) returns an empty struct rather than raising. The dispatch function must *call* the target and forward `*args`.
- **Never let a tool raise.** MCPServer 2.2 handles `except MCPError: raise` before its generic handler, and `ToolError` subclasses `MCPError`, so the `is_error=True` result path is unreachable for it. Return `{"error", "kind", "hint"}` instead — see `_tool` in `src/freecad_ai/server.py`.
- MCPServer runs **synchronous** tool functions on anyio's worker thread pool, so parallel tool calls reach the shared `ServerProxy` from several threads. Its single `HTTPConnection` raises `CannotSendRequest` / `ResponseNotReady` rather than queueing. `Bridge` holds a `threading.Lock` for this; do not remove it.
- A tool returning a **list** is silently truncated: MCPServer's `_convert_to_content` treats a list as a sequence of content blocks and chains them, so only the first element reaches the client. Every tool must return a dict or string — `_tool` wraps stray lists as `{"items": ...}` as a backstop.
- A tool's **return annotation becomes an output schema** that every result is validated against. A tool annotated `-> list[dict]` rejects the error payload as a type mismatch and resurfaces as `UnexpectedToolError`. The `_tool` decorator therefore publishes `__signature__` with a widened return; do not remove it.
- FreeCAD's output streams must stay `DEVNULL`. FreeCAD writes `Recompute......` progress output continuously; capturing it with `subprocess.PIPE` and never reading fills the ~64KB buffer, after which FreeCAD blocks in `write()` and stops answering. The process is still alive, so it presents as a hang, not a crash.
- **A port already in use must be an error, not a silent adoption.** If another FreeCAD holds the port, the one launched here cannot bind and a readiness ping answers from the incumbent — adopting it would hand the model that session's open documents. `start_headless` compares the bridge's `instance_id` pid against the process it spawned and raises `PortInUse`. Note `freecad-robust-mcp-server` also uses 9875.
- `xmlrpc.client` serialises any uncaught bridge exception as `faultCode 1`, so the bridge's own codes start at 100. Mapping 1 to a named condition misreports ordinary `TypeError`s.

<!-- /bmad:context -->
