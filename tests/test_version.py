"""There is exactly one version literal in this repository.

The failure this prevents is ordinary and easy to ship: someone bumps
`pyproject.toml`, forgets the copy in `_version.py`, and the server reports one
version while the wheel installs another. Nothing crashes. A user reports a bug
against 0.2.0 and the maintainer is reading 0.1.0 code.

So rather than trusting a convention, this asserts the *absence* of the
duplication: no source file, test, or document may contain a second copy of
the version string. `pyproject.toml` is the single exception, and it is checked
separately to be a well-formed version rather than any old string.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from freecad_ai import __version__

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

# Version literals only. Deliberately narrow: `1.1.3` for FreeCAD, `9876` for a
# port, `2024-11-05` for an MCP protocol are all legitimate and matching them
# would make this test cry wolf. A PEP 440 version has to look like one -
# digits and dots, at least one of each.
_VERSION_LITERAL = re.compile(r'["\']([0-9]+\.[0-9]+(?:\.[0-9]+)?)["\']')

# The only file permitted to hold the version. `_version.py` is deliberately
# NOT on this list: it used to declare its own literal while describing itself
# as the single source of truth, which is exactly the duplication this file
# exists to catch. It reads installed metadata now, so it has no reason to
# carry a number - and allowing it would let the old bug return unnoticed.
ALLOWED = {PYPROJECT}
EXCLUDED_DIRS = {".git", ".venv", "dist", "build", "__pycache__", ".mypy_cache"}


def _candidates() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if EXCLUDED_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.suffix not in {".py", ".md", ".toml", ".yaml", ".yml"}:
            continue
        out.append(path)
    return out


def test_the_version_is_a_single_literal_in_pyproject() -> None:
    """`pyproject.toml` holds it, and `_version.py` reads what it says."""
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert declared["version"] == __version__, (
        f"pyproject.toml declares {declared['version']!r} but the package "
        f"reports {__version__!r}"
    )
    # PEP 440 shape, checked so a stray value cannot pass as "some string".
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__), (
        f"{__version__!r} is not a plain major.minor.patch version"
    )
    assert "dynamic" not in declared or "version" not in declared["dynamic"], (
        "pyproject.toml must declare the version statically; _version.py reads "
        "it from installed metadata rather than parsing this file at runtime"
    )


def test_no_other_file_restates_the_version() -> None:
    """No second copy of the version anywhere else in the repository.

    Including tests and documentation. A test asserting the literal is the same
    duplication wearing a disguise - it fails on every version bump for a reason
    that has nothing to do with the code, which trains people to update it
    without reading it.
    """
    offenders: dict[str, list[str]] = {}
    for path in _candidates():
        if path in ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue
        found = {m for m in _VERSION_LITERAL.findall(text) if m == __version__}
        if found:
            offenders[str(path.relative_to(ROOT))] = sorted(found)

    assert not offenders, (
        f"the version {__version__} is restated in {offenders}. Only "
        f"pyproject.toml may hold it; everything else must read it from "
        f"`freecad_ai.__version__`."
    )


@pytest.mark.parametrize(
    "name",
    ["CHANGELOG.md", "README.md", "AGENTS.md"],
)
def test_docs_may_mention_the_version_only_as_a_heading(name: str) -> None:
    """Documented exception, and it is narrow on purpose.

    A changelog has to name the version it describes - that is the whole
    document. It is checked separately from the rule above so the exception is
    explicit rather than a hole in the sweep, and restricted to the changelog
    plus the two files that link to it.
    """
    path = ROOT / name
    if not path.is_file():
        pytest.skip(f"{name} does not exist")
    mentions = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if __version__ in line
    }
    if name == "CHANGELOG.md":
        # Any line naming the version is a heading or a link reference.
        bad = [m for m in mentions if not m.startswith(("#", "["))]
        assert not bad, f"CHANGELOG.md mentions the version outside a heading: {bad}"
        return
    # README and AGENTS.md may only reference the changelog.
    bad = [m for m in mentions if "CHANGELOG" not in m]
    assert not bad, f"{name} mentions the version outside a changelog link: {bad}"
