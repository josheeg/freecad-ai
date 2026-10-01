<!-- bmad:context -->
<!-- Verified 2026-10-01 against 8b41f71. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## freecad-ai

MCP server that lets an AI assistant drive FreeCAD 1.1. Python, `uv`, packaged with `uv_build`. Planning and specs use the BMad skills; artifacts land in `_bmad-output/`.

## Where things are

- Entry point: `src/freecad_ai/`, exposed as the `freecad-ai` console script via `freecad_ai:main`. `src/freecad_ai/__main__.py` is the second entry, for `python -m freecad_ai` and for the frozen build — see "Packaging" below, and do not delete it as redundant.
- FreeCAD-side bridge is `src/freecad_ai/_freecad_bridge.py` — the only file allowed to `import FreeCAD`, and **never imported by the package**; it is launched by path as a script. It lives inside the package so it ships in the wheel, and tests enforce the never-imported rule.
- Target FreeCAD is 1.1 only — `C:\Program Files\FreeCAD 1.1`. Do not write code against 1.0 APIs.
- Adding or changing a tool, a bridge function, or the server layer? Read `docs/mcp-server-gotchas.md` first — the traps there are tested but not self-announcing, and each one fails silently.
- The spine at `_bmad-output/initiative-freecad-mcp-server/architecture-freecad-ai-server/` is `status: final` and binding. Read it before deciding how to implement anything.

## Running and verifying

- `just check` is the gate a commit must pass; `just test` runs everything. Neither is installed as a dependency — without `just`, prefix each tool with `uv run`, or bare invocations run against the wrong interpreter.
- `uv run mypy` with no path argument. `mypy src` checks *less* than configured: `files` covers `scripts/` too.
- Bridge code cannot be covered by unit tests alone — it needs a live `freecadcmd` process. Mark those tests `integration` and deselect them while iterating: `uv run pytest -m "not integration"`.
- `freecadcmd.exe` is not on `PATH`; call it by full path, `"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"`, or set `FREECAD_AI_FREECAD_BIN`.

## Conventions that differ from defaults

- **Never `import FreeCAD` in server code.** FreeCAD links `python311.dll` and only imports cleanly inside its own bundled 3.11; from this project's 3.14 interpreter it fails with `Module use of python311.dll conflicts with this version of Python`. Reach FreeCAD only over the XML-RPC bridge.
- The server runs on the project's own Python (3.14) and never shares an interpreter with FreeCAD's bundled 3.11. That separation is the design — do not "simplify" it by embedding.
- Headless `freecadcmd` is the only target; a GUI path is out of scope, not merely untested.
- The bridge is unauthenticated XML-RPC bound to loopback. Never widen the bind address — anyone who can reach the port controls FreeCAD outright.
- **Never pass an argument that can crash FreeCAD to a native constructor unchecked.** `Sketcher.Constraint` with six arguments *terminates* FreeCAD rather than raising, taking every open document with it. Validate enums against a known set first (AD-22).

## Known pitfalls

- Two FreeCAD versions are installed (`1.0` and `1.1`) side by side. An unqualified path picks the wrong one; name 1.1 explicitly.
- `freecadcmd script.py` **imports** the script under the module name taken from its filename, so `__name__` is never `"__main__"`. A conventional `if __name__ == "__main__"` guard silently does nothing: the process starts, defines everything, and exits without binding the port.
- Under `freecadcmd`, `sys.argv[1]` is the **script path**, so user arguments start at index 2. Reading from index 1 treats the script's own path as the host and the host as the port.
- Headless `freecadcmd` on 1.1 has **no** `QApplication` *or* `QCoreApplication` instance, and `FreeCADGui`/`PySide6` still import cleanly — branch on `FreeCAD.GuiUp` alone, never on import success or app-instance presence. Starting bridge work before `GuiUp` is `True` races the Qt event loop and crashes FreeCAD with `SIGABRT`.
- FreeCAD is not on PyPI (verified: 404). Never add it as a dependency; it is an external process, not an import.
- **A FreeCAD that fails to start must be stopped before the error is raised.** The caller gets an exception and no handle on it, so an un-stopped one is orphaned for good, still bound to its port, and the next attempt fails the same way. `just post-test` reports leaks; `just kill-freecad` clears them.
- An unclosed sketch profile does not fail at extrude time — `Part::Extrusion` returns a *wrong solid*. Call `sketch_status` first; `extrude_sketch` refuses one, but only because it checks explicitly.

<!-- /bmad:context -->

## Releasing

Also outside the managed block, for the same reason as the artifacts below.

`just release-dry-run` builds the wheel, the sdist and the executable, drives
FreeCAD through the executable, checks each archive actually contains the bridge
script, writes `SHA256SUMS`, and extracts the release notes from
`CHANGELOG.md`. Nothing is uploaded — the `.github/workflows/release.yaml`
workflow does that, on a tag.

Three rules that are not obvious from reading the workflow:

- **The tag must name the version in `pyproject.toml`.** Tag a new version
  without bumping `pyproject.toml` and the old build ships under the new label:
  no build fails and no test fails. `scripts/release.py` checks this first,
  before building anything, because the tag is pushed *before* the workflow
  runs and so cannot be quietly replaced. `just release-check <tag>` exercises
  the check without building.
- **No version number appears in this file or the README.** Both read
  `freecad_ai.__version__`, which reads `pyproject.toml`.
  `tests/test_version.py` fails if a second copy is ever written — including one
  in prose, which this file previously had.
- **Release notes are generated from `CHANGELOG.md`, never written by hand.** A
  second document describing the same changes goes stale silently, and the
  release page is the one people actually read.

The one thing that cannot be verified from this repository is the upload itself,
because there is no remote. A `verify` job re-downloads the published artifacts
and recomputes their checksums to cover exactly that gap.

## Packaging

Also outside the managed block, for the same reason as the artifacts below.

`just freeze` builds `dist/freecad-ai.exe` — one file, ~24 MB, needing neither
Python nor `uv`. `just verify-freeze` builds it and then **drives FreeCAD
through the executable**, because a bundle can start, advertise every tool, and
still fail on the first call that needs FreeCAD.

Three things here are not derivable from reading the code, and each is a trap
that fails quietly:

- **The bridge script must ship as `datas` in `packaging/freecad-ai.spec`.**
  That means `src/freecad_ai/_freecad_bridge.py` specifically. FreeCAD *reads*
  it from disk as a script rather than importing it, so PyInstaller's import
  analysis cannot see it. Omit it and you get the worst failure available: the
  exe starts, lists all 36 tools, and dies on the first FreeCAD call. Nothing
  cheap catches that.
- **`src/freecad_ai/__main__.py` cannot be replaced by `server.py` as the
  PyInstaller entry.** PyInstaller runs its entry as top-level `__main__`, where
  `server.py`'s relative imports raise `ImportError` and the exe dies before
  printing anything. `__main__.py` imports absolutely, which serves both
  `python -m freecad_ai` and the frozen build.
- **`BRIDGE_SCRIPT` resolves through `sys._MEIPASS` when frozen.** Unfrozen,
  "sibling of this module" already works, so this branch can be deleted and every
  ordinary test stays green.

FreeCAD is **never bundled**: it links `python311.dll` and this server runs on
3.14, so the two cannot share a process. That constraint is also what keeps the
executable at 24 MB rather than shipping two Pythons.

Both recipes run in CI (`frozen` job). Do not add `freeze` to `just check` — it
takes ~40s, which would dominate the gate; a test in `tests/test_packaging.py`
pins that it stays out.

## Governing artifacts

Outside the managed block on purpose — a `bmad-project-context` refresh
replaces everything between the markers, which would take this with it.

- **Spec** — `_bmad-output/initiative-freecad-mcp-server/spec-freecad-ai-server/spec-freecad-ai-server.md`,
  with `tool-surface.md`, `failure-modes.md` and `conventions.md` alongside it.
  Eight capabilities, CAP-1…CAP-8. Read this before changing what a tool does.
- **Sketch spec** — `_bmad-output/initiative-freecad-mcp-server/spec-freecad-ai-sketches/spec-freecad-ai-sketches.md`,
  with `sketch-surface.md` and `sketch-traps.md`. Nine capabilities, CAP-S1…CAP-S9,
  for 2D profiles to solids. Built: `add_sketch`, `add_sketch_line`, `add_sketch_arc`,
  `add_sketch_circle`, `remove_sketch_geometry`, `add_sketch_constraint`,
  `sketch_status`, `extrude_sketch`, `attach_sketch_to_face`, `sketch_to_face`.
  Check `sketch_status` before `extrude_sketch` — an unclosed profile extrudes
  to a *wrong solid* rather than failing.
- **Spine** — `_bmad-output/initiative-freecad-mcp-server/architecture-freecad-ai-server/architecture-freecad-ai-server.md`.
  Thirty-one numbered decisions, AD-1…AD-31, each with the failure it prevents.
  **These are binding.** A change that contradicts an AD is either a new AD or
  an explicit decision to retire one — not a quiet divergence. The spine's own
  "What this document governs" section says which rules are load-bearing; the
  Conventions section there is advisory, and promoting one to an AD is a
  deliberate act.

The spec records *what* the system does; the spine records *which decisions
were forced* and are therefore not yours to re-make. Code shows the third
layer. Read in that order when the question is "may I do this?" and start at
the spine.
