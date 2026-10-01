"""Tests for the leak gate in `scripts/freecad_procs.py`.

The gate is what `just post-test` calls and what AGENTS.md describes as
"checked rather than trusted", and nothing exercised it before this file.
That is the defect: a gate nobody runs is a gate nobody knows fails.

The important property is not that it finds leaks - it does that by shelling
out to tasklist and PowerShell - but that it **fails closed**. A version that
returned an empty dict on any error reported a clean machine while blind,
which is the worst possible failure for the one check standing between a
leaked FreeCAD and the next run failing for the wrong reason.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "freecad_procs.py"


def _load() -> ModuleType:
    """Import the script by path.

    It is a standalone script rather than a package module, so it cannot be
    imported normally.
    """
    spec = importlib.util.spec_from_file_location("freecad_procs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def procs() -> ModuleType:
    return _load()


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# -- it cannot see ---------------------------------------------------------


def test_missing_tasklist_is_not_reported_as_no_leaks(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasklist failing means the answer is unknown, not 'none'."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("tasklist")

    monkeypatch.setattr(procs.subprocess, "run", boom)
    with pytest.raises(procs.CannotObserve):
        procs.freecad_pids()


def test_tasklist_nonzero_exit_is_not_reported_as_no_leaks(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        procs.subprocess, "run", lambda *_a, **_k: _completed("boom", "denied", 1)
    )
    with pytest.raises(procs.CannotObserve):
        procs.freecad_pids()


def test_powershell_failure_is_not_an_empty_process_list(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original bug: this returned {}, so every bridge looked absent."""
    monkeypatch.setattr(
        procs.subprocess, "run", lambda *_a, **_k: _completed("", "no such", 1)
    )
    with pytest.raises(procs.CannotObserve):
        procs.command_lines()


def test_unparseable_json_is_not_an_empty_process_list(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        procs.subprocess, "run", lambda *_a, **_k: _completed("not json at all")
    )
    with pytest.raises(procs.CannotObserve):
        procs.command_lines()


def test_main_exits_nonzero_when_it_cannot_look(
    procs: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The end-to-end property: an unobservable gate is a red gate.

    Exit code 2, distinct from 1 which means 'leaks found', so a caller can
    tell "the check failed" from "the check passed".
    """

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(procs.subprocess, "run", boom)
    monkeypatch.setattr(sys, "argv", ["freecad_procs.py"])
    assert procs.main() == 2
    output = capsys.readouterr().out
    assert "not a clean result" in output


# -- it can see ------------------------------------------------------------


def test_no_processes_is_a_clean_zero(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Having looked and found nothing is exit 0, and says so."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        if args[0] == "tasklist":
            return _completed("")
        return _completed("")  # ConvertTo-Json emits nothing with no processes

    monkeypatch.setattr(procs.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["freecad_procs.py"])
    assert procs.main() == 0


def test_a_leak_is_reported_and_exits_nonzero(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        '[{"ProcessId": 456, "CommandLine": "freecadcmd '
        'C:\\\\repo\\\\_freecad_bridge.py 127.0.0.1 9875"}]'
    )

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[0] == "tasklist":
            return _completed('"freecadcmd.exe","456","Console","1","10,000 K"')
        return _completed(payload)

    monkeypatch.setattr(procs.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["freecad_procs.py"])
    assert procs.main() == 1


# -- it never touches a GUI FreeCAD ---------------------------------------


def test_a_gui_freecad_is_left_alone(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety claim, asserted: only our bridge script is ever a candidate.

    AGENTS.md promises a FreeCAD the user is running through the GUI is never
    touched. Nothing checked that before.
    """
    payload = (
        '[{"ProcessId": 111, "CommandLine": "freecadcmd '
        'C:\\\\repo\\\\_freecad_bridge.py 127.0.0.1 9875"},'
        '{"ProcessId": 222, "CommandLine": "freecadcmd C:\\\\FreeCAD\\\\freecad.exe"}]'
    )
    killed: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[0] == "tasklist":
            return _completed(
                '"freecadcmd.exe","111","Console","1","10,000 K"\n'
                '"freecadcmd.exe","222","Console","1","10,000 K"'
            )
        if args[0] == "taskkill":
            killed.append(args)
            return _completed("")
        return _completed(payload)

    monkeypatch.setattr(procs.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["freecad_procs.py", "--stop"])
    procs.main()
    assert killed == [["taskkill", "/PID", "111", "/F"]], killed


def test_a_refused_kill_is_reported_not_swallowed(
    procs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill that fails must not print 'stopped' and exit 0.

    The old version discarded taskkill's returncode and always exited 0, so
    the documented remedy for a compounding leak reported success while the
    orphan kept its port.
    """
    payload = (
        '[{"ProcessId": 456, "CommandLine": '
        '"freecadcmd C:\\\\repo\\\\_freecad_bridge.py"}]'
    )

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[0] == "tasklist":
            return _completed('"freecadcmd.exe","456","Console","1","10,000 K"')
        if args[0] == "taskkill":
            return _completed("", "access denied", 1)
        return _completed(payload)

    monkeypatch.setattr(procs.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["freecad_procs.py", "--stop"])
    assert procs.main() == 1
