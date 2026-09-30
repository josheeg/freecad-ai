"""Report or stop FreeCAD processes this project started.

A FreeCAD left running after a test is a defect, not housekeeping. On Windows
`terminate()` is `TerminateProcess` and skips `atexit`, so the bridge never
unbinds its port. The orphan then holds that port, and the next run fails for
the wrong reason - which is exactly how a real leak hid behind a green suite
for as long as it did. See AD-20.

Matching on the bridge script path is what makes this safe to run: a FreeCAD
you are using through the GUI is a different process and is left alone.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

BRIDGE_MARKER = "_freecad_bridge.py"


def freecad_pids() -> list[int]:
    """PIDs of every freecadcmd.exe on this machine."""
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq freecadcmd.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def command_lines() -> dict[int, str]:
    """pid -> full command line, for every running process."""
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
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
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

    pids = ours()
    if not pids:
        print("no FreeCAD processes from this project")
        return 0

    for pid in pids:
        if args.stop:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(f"stopped pid {pid}")
        else:
            print(f"LEAK: pid {pid} is still running and holding its port")

    if not args.stop:
        print()
        print("Each one keeps its port bound, so the next run may fail for the")
        print("wrong reason. Clear them with: just kill-freecad")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
