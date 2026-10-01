"""Entry point for `python -m freecad_ai` and for the frozen executable.

The import here is **absolute**, and that is the whole reason this module
exists rather than pointing a launcher straight at `server.py`.

PyInstaller executes its entry script as top-level `__main__` with no package
context, so `server.py`'s `from .bridge import ...` raises `ImportError:
attempted relative import with no known parent package` and the exe dies before
serving anything. An absolute import resolves in both cases: under `-m` the
package is on the path, and under PyInstaller the package is bundled into the
archive. So the same file serves both, and there is no packaging-only shim whose
only job is to differ from the real one.
"""

from __future__ import annotations

from freecad_ai.server import main

if __name__ == "__main__":
    main()
