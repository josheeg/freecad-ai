"""Report or stop FreeCAD processes this project started.

A FreeCAD left running after a test is a defect, not housekeeping. On Windows
`terminate()` is `TerminateProcess` and skips `atexit`, so the bridge never
unbinds its port. The orphan then holds that port, and the next run fails for
the wrong reason - which is exactly how a real leak hid behind a green suite
for as long as it did. See AD-20.

Matching on the bridge script path is what makes this safe to run: a FreeCAD
you are using through the GUI is a different process and is left alone.

**This gate fails closed.** It reports a clean bill of health only when it
actually looked and found nothing. Every path where it cannot see - PowerShell
missing, `Get-CimInstance` failing, the JSON shape shifting, `tasklist`
changing its columns - exits non-zero and says so, rather than returning an
empty list that reads as "no leaks". An unobservable gate is a red gate; the
version that returned `{}` on error was a green one that reported success
while blind.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

BRIDGE_MARKER = "_freecad_bridge.py"


class CannotObserve(RuntimeError):
    """Raised when the process list could not be determined.

    Distinct from "there are none". Callers must not treat an inability to
    look as an absence of leaks.
    """


def freecad_pids() -> list[int]:
    """PIDs of every freecadcmd.exe on this machine.

    Raises CannotObserve if tasklist itself fails, since an empty result is
    then indistinguishable from a machine with no FreeCAD at all.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq freecadcmd.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CannotObserve(f"cannot run tasklist: {error}") from error
    if result.returncode != 0:
        raise CannotObserve(
            f"tasklist exited {result.returncode}: {result.stderr.strip()}"
        )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def command_lines() -> dict[int, str]:
    """pid -> full command line, for every running process.

    Raises CannotObserve rather than returning {} on any failure: an empty
    dict would make every bridge look absent, and the caller would report a
    clean machine while having asked nothing.
    """
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId, CommandLine | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CannotObserve(f"cannot run powershell: {error}") from error
    if result.returncode != 0:
        raise CannotObserve(
            f"powershell exited {result.returncode}: {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        # ConvertTo-Json emits nothing at all when there are no processes,
        # which is a legitimate answer rather than a failure.
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CannotObserve(
            f"cannot parse the process list as JSON: {error}"
        ) from error
    if isinstance(data, dict):  # a single process serialises as an object
        data = [data]
    out: dict[int, str] = {}
    for entry in data:
        pid = entry.get("ProcessId")
        if isinstance(pid, int):
            out[pid] = entry.get("CommandLine") or ""
    return out


def ours() -> list[int]:
    """FreeCAD pids that are running this project's bridge script."""
    lines = command_lines()
    return [pid for pid in freecad_pids() if BRIDGE_MARKER in lines.get(pid, "")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stop", action="store_true", help="terminate them instead of reporting"
    )
    args = parser.parse_args()

    try:
        pids = ours()
    except CannotObserve as error:
        print(f"cannot tell whether FreeCAD leaked: {error}")
        print("This is not a clean result - the process list could not be read.")
        return 2

    if not pids:
        print("no FreeCAD processes from this project")
        return 0

    failed: list[int] = []
    for pid in pids:
        if args.stop:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                ok = result.returncode == 0
            except OSError, subprocess.SubprocessError:
                ok = False
            if ok:
                print(f"stopped pid {pid}")
            else:
                failed.append(pid)
                print(f"could not stop pid {pid}")
        else:
            print(f"LEAK: pid {pid} is still running and holding its port")

    if failed:
        print()
        print(f"{len(failed)} process(es) are still running and still holding their")
        print("ports, so the next run may fail for the wrong reason.")
        return 1

    if not args.stop:
        print()
        print("Each one keeps its port bound, so the next run may fail for the")
        print("wrong reason. Clear them with: just kill-freecad")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
