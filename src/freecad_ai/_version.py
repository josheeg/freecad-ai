"""The package version, read rather than restated.

`pyproject.toml` holds the only literal. That is not a preference: it is the
file PEP 621 requires and the one `uv_build` reads, so any second copy is a
copy that can disagree. An earlier version of this module declared its own
`__version__` while calling itself the "single source of truth", which it was
not - and a test asserted that literal too, making three copies in total.

Reading installed metadata rather than parsing `pyproject.toml` at runtime is
deliberate. The wheel ships `.dist-info/METADATA`, so this works from an
installed package; `pyproject.toml` is in the sdist but *not* the wheel, so
parsing it here would work in a checkout and fail for every installed user.

This file deliberately contains no version number, not even in prose -
`tests/test_version.py` sweeps the repository for a second copy of the literal
and would report this one.

The failure mode is a loud one. If the package is not installed, this raises
`PackageNotFoundError` at import - which would break `import freecad_ai`
outright rather than quietly reporting a stale number. That is the correct
trade: a server that cannot say what version it is should not start.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("freecad-ai")
except PackageNotFoundError:  # pragma: no cover - only if not installed
    # Deliberately not a fallback literal. A hardcoded copy is exactly the
    # duplication this module exists to remove, and it would drift silently.
    raise RuntimeError(
        "freecad-ai is not installed, so its version cannot be read. Run "
        "`uv sync`, or install the package. The version lives in "
        "pyproject.toml and is read from the installed metadata; there is no "
        "second copy to fall back to."
    ) from None

__all__ = ["__version__"]
