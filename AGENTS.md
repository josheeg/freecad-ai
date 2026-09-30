<!-- bmad:context -->
<!-- Verified 2026-09-29 against efcfbcc. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## freecad-ai

MCP server that lets an AI assistant drive FreeCAD 1.1. Python, `uv`, packaged with `uv_build`. Planning and specs use the BMad skills; artifacts land in `_bmad-output/`.

## Where things are

- Entry point: `src/freecad_ai/`, exposed as the `freecad-ai` console script via `freecad_ai:main`
- FreeCAD-side bridge is `src/freecad_ai/_freecad_bridge.py` — the only file allowed to `import FreeCAD`, and **never imported by the package**; it is launched by path as a script. It lives inside the package so it ships in the wheel, and tests enforce the never-imported rule.
- Target FreeCAD is 1.1 only — `C:\Program Files\FreeCAD 1.1`. Do not write code against 1.0 APIs.
- Adding or changing a tool, a bridge function, or the server layer? Read `docs/mcp-server-gotchas.md` first — the traps there are tested but not self-announcing, and each one fails silently.

## Running and verifying

- Prefix every Python tool with `uv run` (`uv run pytest`, `uv run ruff check .`, `uv run mypy src`) — none are installed globally, and bare invocations run against the wrong interpreter.
- Run `uv run ruff format .` before committing. CI fails on `ruff format --check`; `ruff check` does not enforce it.
- `freecadcmd.exe` is not on `PATH`; call it by full path, `"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"`, or set `FREECAD_AI_FREECAD_BIN`.
- Bridge code cannot be covered by unit tests alone — it needs a live `freecadcmd` process. Mark those tests `integration` and deselect them while iterating: `uv run pytest -m "not integration"`.

## Conventions that differ from defaults

- **Never `import FreeCAD` in server code.** FreeCAD links `python311.dll` and only imports cleanly inside its own bundled 3.11; from this project's 3.14 interpreter it fails with `Module use of python311.dll conflicts with this version of Python`. Reach FreeCAD only over the XML-RPC bridge.
- The server runs on the project's own Python (3.14) and never shares an interpreter with FreeCAD's bundled 3.11. That separation is the design — do not "simplify" it by embedding.
- Headless `freecadcmd` is the default and only target; no GUI path is implemented. Do not add one without tests that exercise it.
- The bridge is unauthenticated XML-RPC bound to loopback. Never widen the bind address — anyone who can reach the port controls FreeCAD outright.

## Known pitfalls

- Two FreeCAD versions are installed (`1.0` and `1.1`) side by side. An unqualified path picks the wrong one; name 1.1 explicitly.
- `freecadcmd script.py` **imports** the script under the module name taken from its filename, so `__name__` is never `"__main__"`. A conventional `if __name__ == "__main__"` guard silently does nothing: the process starts, defines everything, and exits without binding the port.
- Under `freecadcmd`, `sys.argv[1]` is the **script path**, so user arguments start at index 2. Reading from index 1 treats the script's own path as the host and the host as the port.
- Headless `freecadcmd` on 1.1 has **no** `QApplication` *or* `QCoreApplication` instance, and `FreeCADGui`/`PySide6` still import cleanly — branch on `FreeCAD.GuiUp` alone, never on import success or app-instance presence. Starting bridge work before `GuiUp` is `True` races the Qt event loop and crashes FreeCAD with `SIGABRT`.
- FreeCAD's workbench loader does not run `Init.py` at startup, only `InitGui.py` module-level code. If bridge auto-start ever needs to live in this repo, that asymmetry governs where it goes.
- FreeCAD is not on PyPI (verified: 404). Never add it as a dependency; it is an external process, not an import.

<!-- /bmad:context -->

## Governing artifacts

Outside the managed block on purpose — a `bmad-project-context` refresh
replaces everything between the markers, which would take this with it.

- **Spec** — `_bmad-output/initiative-freecad-mcp-server/spec-freecad-ai-server/spec-freecad-ai-server.md`,
  with `tool-surface.md`, `failure-modes.md` and `conventions.md` alongside it.
  Eight capabilities, CAP-1…CAP-8. Read this before changing what a tool does.
- **Sketch spec** — `_bmad-output/initiative-freecad-mcp-server/spec-freecad-ai-sketches/spec-freecad-ai-sketches.md`,
  with `sketch-surface.md` and `sketch-traps.md`. Seven capabilities, CAP-S1…CAP-S7,
  for 2D profiles to solids. Built: `add_sketch`, `add_sketch_line`, `add_sketch_arc`,
  `add_sketch_circle`, `remove_sketch_geometry`, `add_sketch_constraint`,
  `sketch_status`, `extrude_sketch`. Check `sketch_status` before `extrude_sketch` —
  an unclosed profile extrudes to a *wrong solid* rather than failing.
- **Spine** — `_bmad-output/initiative-freecad-mcp-server/architecture-freecad-ai-server/architecture-freecad-ai-server.md`.
  Twenty-two numbered decisions, AD-1…AD-22, each with the failure it prevents.
  **These are binding.** A change that contradicts an AD is either a new AD or
  an explicit decision to retire one — not a quiet divergence. The spine's own
  "What this document governs" section says which rules are load-bearing; the
  Conventions section there is advisory, and promoting one to an AD is a
  deliberate act.

The spec records *what* the system does; the spine records *which decisions
were forced* and are therefore not yours to re-make. Code shows the third
layer. Read in that order when the question is "may I do this?" and start at
the spine.
