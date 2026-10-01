# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a standalone freecad-ai server (Windows x64).

Run through `just freeze`, not by hand: the build needs `--paths src` and the
`--add-data` below, and forgetting the second produces an exe that starts,
serves `list_tools`, and then fails on the first call that crosses into
FreeCAD - the most expensive possible place to discover it.

What is deliberately *not* bundled: FreeCAD. It stays an external `freecadcmd`
process, found via FREECAD_AI_FREECAD_BIN or the default install path. FreeCAD
links python311.dll, so freezing it into a 3.14 bundle is not merely
unnecessary but impossible - and AD-1's whole point is that the server never
imports it. The split is what makes a 24 MB binary possible at all.

onefile rather than onedir: an MCP client is configured with a single command
path, so a single .exe is what an end user can actually be handed.
"""

from pathlib import Path

# Paths are absolute because `just freeze` writes the spec into build/ and
# PyInstaller resolves a relative --paths against the spec's own directory.
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
BRIDGE = SRC / "freecad_ai" / "_freecad_bridge.py"

if not BRIDGE.is_file():
    raise SystemExit(f"the bridge script is missing at {BRIDGE}")

# freecad-ai/__main__.py, not server.py: PyInstaller runs the entry as
# top-level __main__, where server.py's relative imports raise ImportError and
# the exe dies before serving. __main__ imports the package absolutely, which
# works both here and under `python -m freecad_ai`.
ENTRY = SRC / "freecad_ai" / "__main__.py"

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    # The FreeCAD-side script is *data*, not code: FreeCAD reads it from disk
    # as a script rather than importing it, so PyInstaller would not collect it
    # by import analysis however it is reached. bridge.py resolves it from
    # sys._MEIPASS when frozen.
    datas=[(str(BRIDGE), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here may be excluded. The MCP stack reaches for optional
    # transports and anyio backends by name at import time, and PyInstaller's
    # analyser cannot see those edges.
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="freecad-ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)