"""Tests for the release script's decisions.

The upload cannot be tested - this repository has no remote - so what is
tested here is everything that decides *what* gets uploaded and *whether* the
release is consistent. Those are the parts that go wrong quietly: a tag that
disagrees with the version, an archive missing the bridge script, a checksum
computed over the wrong bytes.

`scripts/release.py --dry-run` exercises the rest end to end, including a real
build; it is a recipe rather than a test because it takes about a minute.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "scripts" / "release.py"


def _module():
    spec = importlib.util.spec_from_file_location("release_script", RELEASE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_script"] = module
    spec.loader.exec_module(module)
    return module


release = _module()


# --------------------------------------------------------------- the tag


def test_a_matching_tag_is_accepted() -> None:
    version = release.project_version()
    assert release.check_tag(f"v{version}") == version
    assert release.check_tag(version) == version, "the v prefix is optional"


@pytest.mark.parametrize("tag", ["v9.9.9", "v0.2.0", "0.0.1"])
def test_a_tag_that_disagrees_with_the_version_is_refused(tag: str) -> None:
    """The most common release defect, and nothing else catches it.

    Tag `v0.2.0` without bumping the version, and the release ships 0.1.0
    under a 0.2.0 label. No build fails, no test fails, and the tag - the one
    artefact that cannot be quietly replaced - now points at the wrong thing.
    """
    if tag.lstrip("v") == release.project_version():
        pytest.skip("this tag is the current version")
    with pytest.raises(release.ReleaseError) as caught:
        release.check_tag(tag)
    message = str(caught.value)
    assert release.project_version() in message
    assert "delete" in message.lower(), (
        "the message must say the tag has to be removed by hand, because it "
        "is already pushed by the time this runs"
    )


@pytest.mark.parametrize("tag", ["release-1", "v1.2", "latest", "", "v1.2.3.4"])
def test_a_malformed_tag_is_refused(tag: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.check_tag(tag)


# ----------------------------------------------------------- the archives


def test_a_wheel_missing_the_bridge_is_refused(tmp_path: Path) -> None:
    """It installs fine and then cannot start FreeCAD.

    The most expensive kind of packaging defect, because installation succeeds
    and the failure surfaces on a user's machine rather than in CI.
    """
    import zipfile

    wheel = tmp_path / "freecad_ai-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("freecad_ai/__init__.py", "")
        archive.writestr("freecad_ai/__main__.py", "")
    with pytest.raises(release.ReleaseError) as caught:
        release.check_artifact_contents([wheel])
    assert "_freecad_bridge.py" in str(caught.value)


def test_an_sdist_missing_the_spec_is_refused(tmp_path: Path) -> None:
    """An sdist without the spec cannot produce the executable at all.

    Found the hard way: the first `--dry-run` run failed here, because
    `packaging/` was not in the sdist at all. A wheel is unaffected - PyInstaller
    needs no spec - so only an sdist user is broken, which is exactly the kind of
    breakage nobody notices until someone tries.
    """
    import tarfile

    sdist = tmp_path / "freecad_ai-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "freecad_ai-0.1.0/src/freecad_ai/_freecad_bridge.py",
            "freecad_ai-0.1.0/src/freecad_ai/__main__.py",
        ):
            info = tarfile.TarInfo(name)
            info.size = 0
            import io

            archive.addfile(info, io.BytesIO(b""))

    with pytest.raises(release.ReleaseError) as caught:
        release.check_artifact_contents([sdist])
    assert "freecad-ai.spec" in str(caught.value)


def test_the_real_sdist_ships_what_an_sdist_user_needs() -> None:
    """Checked against the actual built archive, when one is present.

    Skipped rather than failed when there is no build, so a fresh checkout does
    not fail a unit test for a missing artifact. CI builds before this runs.
    """
    sdists = sorted((ROOT / "dist").glob("*.tar.gz"))
    if not sdists:
        pytest.skip("no sdist built; run `just build` first")
    release.check_artifact_contents(sdists)


# ------------------------------------------------------------- the sums


def test_sha256_is_over_the_real_bytes(tmp_path: Path) -> None:
    payload = b"freecad-ai" * 10_000
    target = tmp_path / "thing.exe"
    target.write_bytes(payload)
    assert release.sha256(target) == hashlib.sha256(payload).hexdigest()


def test_the_sums_file_is_sha256sum_checkable(tmp_path: Path, monkeypatch) -> None:
    """Written in the coreutils format so `sha256sum -c` works on it.

    Deliberately not JSON. The point of publishing a checksum is that someone
    can verify it with the tool their operating system already has, without
    installing anything from this project.
    """
    import os

    monkeypatch.setattr(release, "DIST", tmp_path)
    artifacts = []
    for name, body in (("a.exe", b"aaa"), ("b.whl", b"bbbb")):
        path = tmp_path / name
        path.write_bytes(body)
        artifacts.append(path)

    sums = release.write_checksums(artifacts)
    lines = sums.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line, path in zip(lines, artifacts, strict=True):
        digest, _, name = line.partition("  ")
        assert name == path.name
        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert os.path.exists(sums)


def test_the_manifest_names_every_artifact(tmp_path: Path) -> None:
    artifacts = []
    for name in ("a.exe", "b.whl", "c.tar.gz"):
        path = tmp_path / name
        path.write_bytes(b"x" * 2048)
        artifacts.append(path)
    text = release.manifest(artifacts)
    for path in artifacts:
        assert path.name in text
    assert text.count("\n") == 2, "one line per artifact, no header"
