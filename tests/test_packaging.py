"""Packaging invariants, without paying for a build.

A frozen bundle is verified by `just verify-freeze`, which builds and drives
FreeCAD through the executable. That is the real check and it takes about a
minute, so it is a recipe rather than a test - a 40-second build on every
`pytest` would dominate a suite that otherwise runs in seconds.

These tests cover the mistakes that make `verify-freeze` fail *late*, in CI, or
on someone else's machine, and which no amount of running the server would
reveal:

  - the bridge script not shipped as data, which yields an exe that serves
    `list_tools` perfectly and then fails on the first FreeCAD call;
  - the entry script using a relative import, which PyInstaller cannot execute
    at all - the exe dies before printing anything;
  - the bundle path lookup being dropped, which works unfrozen and only breaks
    once frozen.

The last of these is mutation-tested like any other gate.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path

import pytest

import freecad_ai.bridge as bridge_module

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yaml"
SPEC = ROOT / "packaging" / "freecad-ai.spec"
MODULE_MAIN = ROOT / "src" / "freecad_ai" / "__main__.py"
SERVER = ROOT / "src" / "freecad_ai" / "server.py"
JUSTFILE = ROOT / "justfile"
VERIFY = ROOT / "scripts" / "verify_freeze.py"

pytestmark = pytest.mark.filterwarnings("error")


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that looks like PyInstaller's `sys._MEIPASS`."""
    root = tmp_path / "_MEI12345"
    root.mkdir()
    shutil.copy2(
        ROOT / "src" / "freecad_ai" / "_freecad_bridge.py",
        root / "_freecad_bridge.py",
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    return root


# ------------------------------------------------------- bundle resolution


def test_bridge_script_is_found_inside_a_bundle(bundle: Path) -> None:
    """Frozen, the script sits in `sys._MEIPASS`, not beside the source file.

    This is the one that makes a frozen build work at all. Unfrozen it passes
    trivially, because the sibling rule already resolves - so without this test
    the lookup could be deleted outright and every unfrozen test would stay
    green.
    """
    assert bridge_module._bundle_root() == bundle
    resolved = bridge_module._bridge_script()
    assert resolved == bundle / "_freecad_bridge.py"
    assert resolved.is_file(), "the bundle lookup produced a path with no file"


def test_bridge_script_falls_back_when_a_bundle_lacks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty bundle directory must not produce a path that does not exist.

    Silently returning the `_MEIPASS` path regardless would make the missing
    file surface much later, as an opaque failure to start FreeCAD, rather than
    at the point where the bundle is malformed.
    """
    empty = tmp_path / "_MEI99999"
    empty.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(empty), raising=False)
    resolved = bridge_module._bridge_script()
    assert resolved != empty / "_freecad_bridge.py"
    assert resolved.is_file()


def test_bridge_script_resolves_unfrozen() -> None:
    """Without `_MEIPASS` the sibling rule is the whole answer."""
    assert not hasattr(sys, "_MEIPASS")
    resolved = bridge_module.BRIDGE_SCRIPT
    assert resolved.name == "_freecad_bridge.py"
    assert resolved.is_file()


def test_the_never_imported_rule_holds_for_the_entry_point() -> None:
    """`__main__.py` is what PyInstaller executes, so it gets the same scrutiny.

    The rule is that nothing in the package imports FreeCAD. A packaging entry
    point is exactly the sort of file that grows a convenience import later,
    and it would be the one file no test was watching.
    """
    tree = ast.parse(MODULE_MAIN.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "FreeCAD" not in imported
    assert imported == {"__future__", "freecad_ai"}


# --------------------------------------------------------------- the spec


def test_spec_ships_the_bridge_script_as_data() -> None:
    """The mistake that produces an exe that works until it does not.

    PyInstaller's import analysis cannot see the bridge script, because FreeCAD
    *reads it from disk as a script* rather than importing it. Omit it from
    `datas` and the exe still starts, still handshakes, still lists all 36
    tools - and fails on the first call that needs FreeCAD. Nothing short of
    driving FreeCAD catches it, and the omission is invisible in review because
    the file is right there in the source tree.
    """
    source = SPEC.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # `datas=[(str(BRIDGE), ".")]` refers to a module-level name, so the
    # filename is not in the call itself. Resolve the name the spec assigns and
    # check the *path it points at* - which is what actually decides what gets
    # bundled, and is the part a rename would silently break.
    assigned = {
        target.id: ast.unparse(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    datas: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "datas":
            assert isinstance(node.value, (ast.List, ast.Tuple))
            datas = list(node.value.elts)
    assert datas, "the spec's Analysis has no datas; the bridge must be one"

    referenced: set[str] = set()
    for element in datas:
        for node in ast.walk(element):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
    path_targets = [
        name for name in referenced if "_freecad_bridge.py" in assigned.get(name, "")
    ]
    assert path_targets, (
        f"Analysis(datas=...) references {sorted(referenced)}, none of which "
        f"resolves to the bridge script; the assigned paths are "
        f"{ {k: v for k, v in assigned.items() if k in referenced} }"
    )


def _spec_paths() -> dict[str, Path]:
    """The spec's module-level path assignments, evaluated.

    Only an assignment whose every free name is *already bound* is executed.
    Filtering on "is an Assign" is not enough and was the first thing tried: the
    spec writes `a = Analysis(...)`, which is an assignment, so that filter ran
    a real build inside a unit test and died on `Analysis is not defined`.

    The rule that works is dependency-ordered: `ROOT` uses only `SPECPATH` and
    `Path`, so it evaluates; `SRC` uses only `ROOT`, so it evaluates; `BRIDGE`
    uses only `SRC`, so it evaluates. `Analysis(...)` calls a name that is
    unbound, so it is skipped. Nothing PyInstaller provides is ever present, so
    no build can start.

    `SPECPATH` is seeded the way PyInstaller sets it - the directory holding the
    spec - so the spec resolves here exactly as it does in a real build rather
    than being hard-coded to this repository's layout.
    """
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    namespace: dict[str, object] = {"Path": Path, "SPECPATH": str(SPEC.parent)}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        free = {
            child.id for child in ast.walk(node.value) if isinstance(child, ast.Name)
        } - {target.id}
        if not free <= set(namespace):
            continue  # calls something PyInstaller provides; not a path
        code = compile(ast.Module([node], []), str(SPEC), "exec")
        exec(code, namespace)
    return {k: v for k, v in namespace.items() if isinstance(v, Path)}


def test_the_spec_points_at_files_that_exist() -> None:
    """The expression is right; is the file?

    The datas test above resolves `datas=[(str(BRIDGE), ".")]` through the name
    it references and confirms that name is assigned a path ending in the bridge
    filename. It never touches the disk, so renaming the script - or the package
    directory - leaves every packaging test green while the build dies at
    `SystemExit` inside PyInstaller.

    The spec checks this itself, which is the right place: it is a loud build
    error. This is here so the failure arrives at `pytest`, naming the file,
    rather than at the end of a 40-second build.
    """
    paths = _spec_paths()
    assert paths, "the spec defines no path constants to check"
    missing = {name: str(p) for name, p in paths.items() if not p.exists()}
    assert not missing, (
        f"the spec points at paths that do not exist: {missing}. Every path "
        f"constant it defines must resolve, or PyInstaller aborts with a bare "
        f"SystemExit naming one of them."
    )


def test_the_spec_entry_and_bridge_are_the_real_files() -> None:
    """Not merely a file with the right name - the ones we actually ship.

    A spec pointing at a stale copy elsewhere would produce a bundle that runs
    a different bridge than the tested one, and that failure is the nasty kind:
    an executable that works, built from the wrong script.
    """
    paths = _spec_paths()
    assert paths["ENTRY"] == ROOT / "src" / "freecad_ai" / "__main__.py"
    assert paths["BRIDGE"] == ROOT / "src" / "freecad_ai" / "_freecad_bridge.py"
    assert (
        paths["BRIDGE"].read_bytes()
        == (ROOT / "src" / "freecad_ai" / "_freecad_bridge.py").read_bytes()
    )


def test_spec_entry_point_uses_absolute_imports() -> None:
    """PyInstaller runs its entry as top-level `__main__`.

    With no package context, `from .bridge import ...` raises ImportError and
    the executable dies before it can serve anything. The fix is an entry
    module that imports absolutely, which is what `__main__.py` is for - so this
    asserts both that the spec points at it and that it is written that way.
    """
    spec_source = SPEC.read_text(encoding="utf-8")
    spec_tree = ast.parse(spec_source)
    assigned = {
        target.id: ast.unparse(node.value)
        for node in spec_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    # The *assignment*, not any mention. A substring check over the whole file
    # passes on the spec's own comment explaining why it is `__main__.py`, so it
    # happily accepted an ENTRY pointing back at server.py - the exact
    # regression this test exists to prevent.
    assert "__main__.py" in assigned.get("ENTRY", ""), (
        f"ENTRY is {assigned.get('ENTRY')!r}; it must be __main__.py, whose "
        f"absolute imports PyInstaller can execute. server.py's relative "
        f"imports raise ImportError before the exe serves anything."
    )

    tree = ast.parse(MODULE_MAIN.read_text(encoding="utf-8"))
    relative = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level != 0
    ]
    assert not relative, (
        f"{MODULE_MAIN.name} uses relative imports at "
        f"{[n.lineno for n in relative]}, which fail under PyInstaller"
    )
    absolute = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    ]
    assert any(node.module == "freecad_ai.server" for node in absolute), (
        f"{MODULE_MAIN.name} must import freecad_ai.server absolutely"
    )


def test_server_relative_imports_are_why_main_exists() -> None:
    """Records *why* there are two entry points, so nobody merges them.

    `server.py` legitimately uses relative imports; that is correct for a
    package module and wrong for a PyInstaller entry script. A future tidy-up
    that deletes `__main__.py` as redundant would break every frozen build, and
    would do so silently in the sense that the source still runs fine.
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    relative = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level != 0
    ]
    assert relative, (
        "server.py no longer uses relative imports, so the two-entry-point "
        "arrangement may no longer be needed - check before deleting it"
    )


def test_the_spec_does_not_try_to_bundle_freecad() -> None:
    """FreeCAD stays external. Two reasons, and the second is not negotiable.

    It links `python311.dll`, so it cannot load inside this project's 3.14
    process at all - and AD-1's rule that the server never imports FreeCAD is
    what keeps the executable down to about 24 MB. A spec that collected
    FreeCAD would fail obscurely at runtime rather than at build time.
    """
    source = SPEC.read_text(encoding="utf-8").lower()
    assert "freecad 1.1" not in source.replace("freecad-ai", "")
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in {"binaries", "datas"}:
            assert isinstance(node.value, (ast.List, ast.Tuple, ast.Call))
            rendered = ast.unparse(node.value)
            assert "freecadcmd" not in rendered.lower()
            assert "program files" not in rendered.lower()


# ------------------------------------------------------- the verification


def _workflow() -> dict:
    """The parsed CI workflow.

    Parsed rather than grepped. A text search for "freeze" is satisfied by the
    word appearing anywhere - a comment, a disabled step, a job named something
    else - so the check that CI actually builds the exe has to look at the
    structure: is there a job, does it run the spec, does it then run the
    verification.
    """
    import yaml

    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_ci_workflow_builds_and_drives_the_executable() -> None:
    """The justfile says every recipe has a CI counterpart. This enforces it.

    `freeze` and `verify-freeze` were the only recipes with none, so the
    packaging - the newest and least-exercised code in the repo - could rot
    with nothing noticing until someone tried to ship. That is the gap this
    closes, and it is the kind that reopens silently, so it is asserted on the
    job's actual steps.

    The verification is the part that matters and is asserted separately below:
    building the exe proves nothing, since a bundle missing the bridge script
    builds perfectly.
    """
    workflow = _workflow()
    assert "frozen" in workflow["jobs"], (
        f"CI has no `frozen` job; jobs are {sorted(workflow['jobs'])}. "
        f"`just freeze` and `just verify-freeze` have no CI counterpart, which "
        f"is the gap this test closes."
    )
    job = workflow["jobs"]["frozen"]
    assert job.get("runs-on") == "windows-latest", (
        f"the frozen job runs on {job.get('runs-on')!r}; the spec and the "
        f"executable are Windows x64 only"
    )

    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "packaging/freecad-ai.spec" in runs, (
        "the frozen job does not build packaging/freecad-ai.spec"
    )
    assert "verify_freeze.py" in runs, (
        "the frozen job builds the executable but never drives it, so a bundle "
        "missing the bridge script as data would pass CI"
    )
    # Order matters: driving a build that has not happened yet proves nothing.
    assert runs.index("packaging/freecad-ai.spec") < runs.index("verify_freeze.py")


def test_the_frozen_job_is_not_the_only_freeze_step() -> None:
    """Guards against `verify-freeze` being satisfied by a stub.

    Cheap, and it exists because the previous assertion could be satisfied by a
    job that runs `verify_freeze.py` against a stale exe left in dist/ - which
    is exactly the state that test_packaging's sibling checks were written for.
    """
    job = _workflow()["jobs"]["frozen"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pyinstaller" in runs, (
        "the frozen job does not invoke pyinstaller; it may be verifying an "
        "executable it never built"
    )


def _release_workflow() -> dict:
    import yaml

    return yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yaml").read_text(encoding="utf-8")
    )


def test_the_release_workflow_is_tag_triggered() -> None:
    """A release must name a version, and a tag is what asserts one.

    There is deliberately no `branches` trigger. A release run off a branch
    would publish whatever happened to be on it, which is the failure mode the
    tag check in scripts/release.py exists to prevent - defeated at the source
    by never offering the workflow a branch to run from.
    """
    workflow = _release_workflow()
    triggers = workflow[True] if True in workflow else workflow.get("on")
    assert "push" in triggers, "the release workflow has no push trigger"
    assert "tags" in triggers["push"], (
        "the release workflow triggers on branches, so it can publish an "
        "unversioned commit; it should be tag-only"
    )
    assert "branches" not in triggers["push"]


def test_the_release_workflow_verifies_before_it_publishes() -> None:
    """The build-and-verify step must precede the upload step.

    Order is the whole point. A release that verifies after publishing has
    already attached the artifact; a `verify` job that fails afterwards leaves
    a published release that is known-bad, which is worse than no release
    because it looks official.
    """
    job = _release_workflow()["jobs"]["release"]
    runs = [step.get("run", "") for step in job["steps"]]
    build = next((r for r in runs if "release.py" in r), None)
    publish = next((r for r in runs if "gh release create" in r), None)
    assert build is not None, "the release job never runs scripts/release.py"
    assert publish is not None, "the release job never publishes anything"
    assert runs.index(build) < runs.index(publish), (
        "the release publishes before it verifies"
    )
    # The build step must pass the tag, or the version check never runs.
    assert "--tag" in build, "the release build is not given the tag to check"


def test_a_failed_publish_is_caught_by_re_downloading() -> None:
    """The upload is the one step this repository cannot test.

    There is no remote, so nothing verifies that what was attached is what was
    hashed. The `verify` job closes that by downloading the published artifacts
    and recomputing their checksums - which catches a truncated upload, a
    swapped file, or an asset attached twice.
    """
    workflow = _release_workflow()
    assert "verify" in workflow["jobs"], (
        "nothing re-downloads the published release; the upload itself is "
        "untested because this repository has no remote"
    )
    verify = workflow["jobs"]["verify"]
    assert verify.get("needs") == "release", (
        "the verify job must depend on the release, or it runs before there is "
        "anything to verify"
    )
    body = "\n".join(step.get("run", "") for step in verify["steps"])
    assert "gh release download" in body
    assert "SHA256" in body, "the verify job does not check any checksums"


def test_verification_drives_freecad_rather_than_proving_the_file_exists() -> None:
    """`verify-freeze` must cross into FreeCAD, or it proves nothing.

    A build that merely exists is the state this whole exercise exists to
    distinguish from a working one, so the check is required to name a geometry
    and re-drive a dimension. Asserted on the source because running it costs a
    build.
    """
    source = VERIFY.read_text(encoding="utf-8")
    for tool in ("new_document", "add_sketch", "set_constraint_value"):
        assert tool in source, f"verify_freeze never calls {tool}"
    assert "freecad_procs" in source, (
        "verify_freeze does not check for leaked processes, so a frozen server "
        "that orphans FreeCAD would pass (AD-20)"
    )


def test_justfile_exposes_freeze_and_verify() -> None:
    """Both recipes exist, and neither is in the commit gate.

    Excluding them is deliberate and worth pinning: the build takes about 40
    seconds, which would dominate `just check`, and it is verified by its own
    recipe rather than assumed. Someone adding `freeze` to `check` would make
    every commit pay for it.
    """
    text = JUSTFILE.read_text(encoding="utf-8")
    assert re.search(r"^freeze:", text, re.M)
    assert re.search(r"^verify-freeze:", text, re.M)
    check = text.split("\ncheck:", 1)[1].split("\n#", 1)[0]
    assert "freeze" not in check, (
        "`just check` must not build the executable; that is verify-freeze's job"
    )
