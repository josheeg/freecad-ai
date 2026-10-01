"""Build the release artifacts, checksum them, and refuse an inconsistent tag.

Split out from the workflow so the parts that can be wrong are testable without
a tag or a GitHub token. The workflow's only job is to run this and upload what
it says to upload.

The ordering is deliberate: everything is built and verified *before* anything
is published, and the tag is checked before the build rather than after. A
release that fails halfway leaves a tag pointing at nothing, and the tag is the
one artefact that cannot be quietly replaced.

The one failure this cannot test locally is the upload itself, because this
repository has no remote. Everything up to and including the manifest is
verified here; `gh release create` is not, and is marked accordingly in the
workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


class ReleaseError(RuntimeError):
    """A release would be wrong or incomplete."""


def project_version() -> str:
    """The declared version, read the same way the package reads it."""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def check_tag(tag: str) -> str:
    """Assert the tag names the version being released.

    The single most common release defect, and one nothing else catches: tag
    `v0.2.0`, bump nothing, and the release ships 0.1.0 under a 0.2.0 label.
    The tag is created *before* this runs, so an unfound version here means a
    tag that must be deleted by hand - which is why the message says so rather
    than just failing.
    """
    match = re.fullmatch(r"v?([0-9]+\.[0-9]+\.[0-9]+)", tag.strip())
    if not match:
        raise ReleaseError(
            f"tag {tag!r} is not a version tag; expected something like v0.1.0"
        )
    tagged = match.group(1)
    declared = project_version()
    if tagged != declared:
        raise ReleaseError(
            f"tag {tag!r} names {tagged} but pyproject.toml declares {declared}. "
            f"The tag is already pushed and cannot be replaced quietly: delete "
            f"it, set the version, and tag again."
        )
    return declared


def uv_executable() -> str:
    """The `uv` binary.

    Called as a subprocess rather than as `python -m uv`: uv is a standalone
    installer-managed executable, not a module in this project's environment,
    so `sys.executable -m uv` fails with "No module named uv". Resolved once and
    overridable so a CI runner with an unusual layout is not stuck.
    """
    override = os.environ.get("FREECAD_AI_UV")
    found = override or shutil.which("uv")
    if not found:
        raise ReleaseError(
            "the `uv` executable was not found on PATH, and this script "
            "requires it to build. Install uv, or set FREECAD_AI_UV to its "
            "full path."
        )
    return found


def run(*args: str) -> None:
    """Run a command, failing loudly. Output goes to this process's stderr."""
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(args)} failed with {result.returncode}")


def build_all(*, skip_exe: bool = False) -> list[Path]:
    """Build the wheel, the sdist and the executable. Verify the exe works.

    The executable is driven through a real FreeCAD before it is eligible for
    publication, because a bundle missing the bridge script as data builds
    perfectly and then fails on the first call a user makes.
    """
    uv = uv_executable()
    run(uv, "build")
    if not skip_exe:
        run(
            uv,
            "run",
            "--with",
            "pyinstaller",
            "pyinstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            "dist",
            "--workpath",
            "build/freeze",
            "packaging/freecad-ai.spec",
        )
        run(sys.executable, "scripts/verify_freeze.py")

    wanted = [
        *sorted(DIST.glob("*.whl")),
        *sorted(DIST.glob("*.tar.gz")),
    ]
    if not skip_exe:
        wanted.append(DIST / "freecad-ai.exe")
    missing = [p for p in wanted if not p.is_file()]
    if missing:
        raise ReleaseError(f"expected artifacts were not produced: {missing}")
    if not wanted:
        raise ReleaseError("no artifacts to release")
    return wanted


def check_artifact_contents(artifacts: list[Path]) -> None:
    """Assert each archive actually contains what it must.

    A wheel that lost the bridge script installs fine and then cannot start
    FreeCAD, so this is checked on the built file rather than trusted from the
    source tree. The sdist is checked for the same reason plus the spec, since
    the frozen build cannot be reproduced without it.
    """
    for path in artifacts:
        if path.suffix == ".whl":
            names = zipfile.ZipFile(path).namelist()
            required = [
                "freecad_ai/_freecad_bridge.py",
                "freecad_ai/__main__.py",
            ]
        elif path.name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                names = archive.getnames()
            required = [
                "src/freecad_ai/_freecad_bridge.py",
                "src/freecad_ai/__main__.py",
                "packaging/freecad-ai.spec",
            ]
        else:
            continue
        absent = [r for r in required if not any(n.endswith(r) for n in names)]
        if absent:
            raise ReleaseError(
                f"{path.name} is missing {absent}. It would install and then "
                f"fail to start FreeCAD."
            )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(artifacts: list[Path]) -> Path:
    """Write `SHA256SUMS` beside the artifacts, in the coreutils format.

    `sha256sum -c SHA256SUMS` then works on any machine with the tool, which is
    the point of using that format rather than a JSON file only this project
    understands.
    """
    target = DIST / "SHA256SUMS"
    lines = [f"{sha256(path)}  {path.name}" for path in artifacts]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_release_notes(version: str) -> Path | None:
    """Extract this version's section from CHANGELOG.md.

    So the release page cannot drift from the changelog. Hand-written release
    notes are a second document describing the same changes, and they go stale
    silently - the changelog gets the fix, the release page does not, and nobody
    notices until someone reads an old one.

    Returns None when there is no section for the version, which is a release
    error rather than a reason to fall back to an empty page.
    """
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.is_file():
        raise ReleaseError(
            "CHANGELOG.md is missing; a release cannot describe itself without one"
        )
    text = changelog.read_text(encoding="utf-8")
    lines = text.splitlines()

    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^##\s+\[?{re.escape(version)}\]?", line.strip()):
            start = index
            break
    if start is None:
        raise ReleaseError(
            f"CHANGELOG.md has no section for {version}. Add one before "
            f"releasing, or the release page would have nothing to say."
        )
    # Runs to the next `## ` heading at the same level, or the link block.
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    while body and not body[-1].strip():
        body.pop()
    # Drop the `[x.y.z]: ...` link reference; it is meaningless on a release page.
    body = [ln for ln in body if not re.match(r"^\[[\d.]+\]:", ln)]

    target = DIST / f"release-notes-{version}.md"
    target.write_text("\n".join(body).strip() + "\n", encoding="utf-8")
    return target


def manifest(artifacts: list[Path]) -> str:
    """What would be attached to the release, with sizes and checksums."""
    rows = [
        f"{path.name}  {path.stat().st_size / 1_048_576:.1f} MB  {sha256(path)}"
        for path in artifacts
    ]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="the git tag being released, e.g. v0.1.0; checked against pyproject",
    )
    parser.add_argument(
        "--skip-exe",
        action="store_true",
        help="wheel and sdist only, for a dry run without a 40s build",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and verify everything, print the manifest, upload nothing",
    )
    args = parser.parse_args(argv)

    if args.tag:
        version = check_tag(args.tag)
        print(f"tag {args.tag} matches version {version}")
    else:
        version = project_version()
        print(f"no --tag given; building version {version} unlabelled")

    artifacts = build_all(skip_exe=args.skip_exe)
    check_artifact_contents(artifacts)
    sums = write_checksums(artifacts)
    notes = write_release_notes(version)

    print(f"\nrelease {version} - {len(artifacts)} artifacts")
    print(manifest(artifacts))
    print(f"{sums.name} written")
    if notes is not None:
        print(f"{notes.name} written ({notes.stat().st_size} bytes)")
    if args.dry_run:
        print("\ndry run: nothing uploaded")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as error:
        print(f"release error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
