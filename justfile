# freecad-ai developer tasks.
#
# Every recipe mirrors a CI step or a test-suite command verbatim. That is the
# point of the file: if a recipe and the workflow ever disagree, the workflow
# is what runs, and it is also what decides whether the commit is good. Keeping
# the strings identical means "green locally" and "green in CI" mean the same
# thing, instead of two things that usually agree.
#
# Run `just` with no arguments for the list, `just check` for the gate that
# gates every commit, `just test` for everything.

# This project is Windows-only: it drives a local FreeCAD install, and the
# integration suite kills and inspects real processes. `just` defaults to `sh`,
# which is not present here, so name PowerShell explicitly. Setting it here
# rather than per-recipe means a new recipe is correct by default.
set shell := ["powershell", "-NoProfile", "-Command"]

# FreeCAD 1.1 only. Both 1.0 and 1.1 are installed on this machine, so an
# unqualified path picks the wrong one.
freecad_bin := env_var_or_default("FREECAD_AI_FREECAD_BIN", "C:\\Program Files\\FreeCAD 1.1\\bin")

# A fixed port for the integration suite, so a stray FreeCAD from an earlier
# run cannot be adopted and no two runs collide. Overridable for parallel work.
test_port := env_var_or_default("FREECAD_AI_PORT", "19875")

# The FreeCAD-side script. Excluded from the default ruff run because that run
# targets 3.14 and the formatter would rewrite `except (A, B):` into the PEP 758
# form, which this file's interpreter cannot parse.
bridge := "src/freecad_ai/_freecad_bridge.py"

# List the available recipes.
default:
    @just --list --unsorted

# Everything CI runs, in CI's order. The gate a commit must pass.
check:
    @just _lint
    @just _types
    @just _test-unit

# Lint and format-check, exactly as the checks job does it.
_lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run ruff check --target-version py311 {{bridge}}
    uv run ruff format --check --target-version py311 {{bridge}}

# Reformat in place, including the bridge at its own target version.
# The bridge must be formatted separately or `just format` reintroduces the
# PEP 758 corruption that AD-13 exists to prevent.
format:
    uv run ruff format .
    uv run ruff format --target-version py311 {{bridge}}

# Typecheck. Strict, and excludes the 3.11 file.
_types:
    uv run mypy

# Tests that need no FreeCAD. Fast enough to run on every save.
_test-unit:
    uv run pytest -m "not integration" -q

# Tests that drive a real headless FreeCAD: process kills, the real stdio
# protocol, and a wheel built and installed.
#
# The port is pinned rather than left to the default so two runs cannot collide
# and a stray FreeCAD from an earlier one cannot be adopted. PowerShell has no
# `VAR=value cmd` prefix, hence the explicit assignment.
test-integration:
    $env:FREECAD_AI_PORT = "{{test_port}}"
    uv run pytest -m integration -q

# The whole suite. About 25s locally, against a real FreeCAD.
test: check
    @just test-integration
    @just post-test

# Report anything the suite leaked, and fail if there is anything. A FreeCAD
# left running after a test is a real defect, not housekeeping: on Windows it
# stays bound to its port, so the next run fails for the wrong reason. This is
# the leak that hid behind a green suite, so it is checked rather than trusted.
# See AD-20.
post-test:
    @uv run python scripts/freecad_procs.py

# Stop every freecadcmd this project started. Matches on the bridge script path
# so it cannot kill a FreeCAD you are using through the GUI.
kill-freecad:
    @uv run python scripts/freecad_procs.py --stop

# Build the wheel. Whether it ships the bridge is not re-checked here:
# test_boundaries.py::test_wheel_contains_a_working_package already builds a
# wheel and asserts it, so a second copy in this file could only drift from the
# test that actually gates the commit. Run `just test` and it is covered.
build:
    uv build

# Dry-run a release: build everything, verify the executable against a real
# FreeCAD, check each archive contains the bridge script, write SHA256SUMS and
# the release notes. Nothing is uploaded - the tag-triggered release workflow
# does that. Use this to see exactly what would be published.
release-dry-run:
    uv run python scripts/release.py --dry-run

# Check a tag against the declared version without building anything. The
# cheapest possible check, and it catches the defect nothing else does: tag
# v0.2.0 without bumping pyproject.toml, and 0.1.0 ships under a 0.2.0 label.
release-check TAG:
    uv run python scripts/release.py --tag "{{TAG}}" --skip-exe --dry-run

# Start the MCP server on stdio, for driving it by hand.
serve:
    uv run freecad-ai

# Build a standalone Windows x64 executable, so a user needs neither Python nor
# uv. FreeCAD is NOT bundled: it stays an external freecadcmd process, because
# it links python311.dll and this server runs on 3.14. AD-1's rule that the
# server never imports FreeCAD is what makes a ~24 MB binary possible.
#
# Not part of `just check`, deliberately. The build takes about 40s, which would
# dominate a gate that otherwise runs in seconds, and it is verified by
# `just verify-freeze` rather than assumed. A broken spec therefore fails its
# own recipe, not every commit.
freeze:
    uv run --with pyinstaller pyinstaller --noconfirm --clean --distpath dist --workpath build/freeze packaging/freecad-ai.spec

# Prove a frozen build actually works, which is not the same as existing.
#
# A bundle missing the bridge script as data starts, serves `list_tools`, and
# then fails on the first call that crosses into FreeCAD - so the check drives
# FreeCAD through the exe rather than asserting the file is there. It also
# asserts nothing was left running, per AD-20.
verify-freeze: freeze
    uv run python scripts/verify_freeze.py

# Remove build and cache output. Not dist/ by default - a built wheel is
# evidence the packaging works, so removing it should be deliberate.
clean:
    uv run python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in map(pathlib.Path, ['.pytest_cache', '.mypy_cache', '.ruff_cache'])]"
    @echo "removed caches; left dist/ alone (use 'just clean-dist' if you meant it)"
