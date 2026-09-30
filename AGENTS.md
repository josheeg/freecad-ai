<!-- bmad:context -->
<!-- Verified 2026-09-29 against efcfbcc. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## freecad-ai

MCP server that lets an AI assistant drive FreeCAD 1.1. Python, `uv`, packaged with `uv_build`. Planning and specs use the BMad skills; artifacts land in `_bmad-output/`.

## Where things are

- Entry point: `src/freecad_ai/`, exposed as the `freecad-ai` console script via `freecad_ai:main`
- FreeCAD-side bridge lives in `bridge/freecad_bridge.py`, **outside** `src/` on purpose — it is the only file allowed to `import FreeCAD`, and keeping it out of the package makes the 3.11/3.14 boundary structural rather than a convention.
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
- `freecadcmd script.py` **imports** the script under the module name taken from its filename, so `__name__` is never `"__main__"`. An `if __name__ == "__main__"` guard silently does nothing: the process starts, defines everything, and exits without binding the port. See `_running_under_freecadcmd()` in `bridge/freecad_bridge.py`.
- Under `freecadcmd`, `sys.argv[1]` is the **script path**, so user arguments start at index 2. Reading from index 1 treats the script's own path as the host and the host as the port.
- FreeCAD dimensions are `Base.Quantity`, not `float`. An `isinstance` filter for `(int, float, str, bool)` drops every dimension silently; unwrap via `.Value` and `.getUserPreferred()`.
- XML-RPC cannot marshal `None`, and marshalling a non-marshallable object (a function, say) returns an empty struct rather than raising. The dispatch function must *call* the target and forward `*args`.

<!-- /bmad:context -->
