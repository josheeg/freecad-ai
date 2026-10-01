"""Prove a frozen freecad-ai build works, rather than that it exists.

The failure this exists for: a bundle built without the bridge script shipped
as data still starts, still answers `initialize`, still lists all 36 tools, and
then fails on the first call that crosses into FreeCAD. Every cheap check
passes. Only actually driving FreeCAD through the executable catches it, so
that is what this does - naming geometry and re-driving a dimension, because
those cross the boundary twice and the second crossing is where a resolved path
stops being valid.

Exits non-zero on the first failure, and checks for leaked FreeCAD afterwards
because a frozen server that orphans a process is the same defect AD-20 exists
for, and it would hide the next run's failure behind a port conflict.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "freecad-ai.exe"
# A port nothing else on this machine uses, so the check cannot collide with a
# FreeCAD the developer has open.
PORT = "19998"


def _fail(message: str) -> NoReturn:
    print(f"  FAIL  {message}")
    raise SystemExit(1)


async def _drive() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["FREECAD_AI_PORT"] = PORT
    params = StdioServerParameters(command=str(EXE), args=[], env=env)

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(f"  server {init.server_info.name} {init.server_info.version}")

        tools = await session.list_tools()
        count = len(tools.tools)
        print(f"  {count} tools advertised")
        if count < 30:
            _fail(
                f"only {count} tools advertised; a frozen bundle with a "
                f"broken import usually loses most of the surface"
            )

        async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = await session.call_tool(name, args)
            # Narrow rather than assume: the content union includes image and
            # resource blocks, and every tool here returns text. A non-text
            # block would be a real change in the surface, so it is reported
            # rather than coerced.
            first = result.content[0] if result.content else None
            if not isinstance(first, TextContent):
                _fail(f"{name} returned {type(first).__name__}, not text")
            try:
                parsed: Any = json.loads(first.text)
            except ValueError:
                parsed = {"_raw": first.text}
            if not isinstance(parsed, dict):
                parsed = {"_raw": parsed}
            return cast("dict[str, Any]", parsed)

        made = await call("new_document", {"name": "frozen"})
        if "error" in made:
            _fail(f"new_document failed: {made}")

        made = await call("add_sketch", {"document": "frozen", "sketch_name": "P"})
        if "error" in made:
            _fail(f"add_sketch failed: {made}")

        # A name, not an index: proves the bridge script was found and read,
        # since the name is resolved inside the FreeCAD-side process.
        made = await call(
            "add_sketch_line",
            {
                "document": "frozen",
                "sketch_name": "P",
                "x1": 0,
                "y1": 0,
                "x2": 40,
                "y2": 0,
                "name": "base",
            },
        )
        if "error" in made:
            _fail(f"add_sketch_line failed: {made}")

        made = await call(
            "add_sketch_constraint",
            {
                "document": "frozen",
                "sketch_name": "P",
                "kind": "Distance",
                "first": "base",
                "first_pos": 1,
                "second": "base",
                "second_pos": 2,
                "value": 40.0,
                "name": "width",
            },
        )
        if "error" in made:
            _fail(f"add_sketch_constraint failed: {made}")

        # The second crossing: the solver has to move geometry, so this
        # exercises the bridge path twice in one session.
        made = await call(
            "set_constraint_value",
            {
                "document": "frozen",
                "sketch_name": "P",
                "reference": "width",
                "value": 60.0,
            },
        )
        if made.get("value") != 60.0:
            _fail(f"set_constraint_value returned {made}")

        made = await call("sketch_status", {"document": "frozen", "sketch_name": "P"})
        if made.get("geometry") != [{"geometry": 1, "name": "base"}]:
            _fail(f"sketch_status lost the geometry name: {made}")


def main() -> int:
    if not EXE.is_file():
        print(f"  {EXE} does not exist. Run `just freeze` first.")
        return 1
    size = EXE.stat().st_size / 1_048_576
    print(f"  {EXE.name}  {size:.1f} MB")

    asyncio.run(_drive())
    print("  drove FreeCAD through the frozen exe")

    # AD-20: a process left behind stays bound to its port on Windows, so the
    # next run fails for the wrong reason.
    script = ROOT / "scripts" / "freecad_procs.py"
    if script.is_file():
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(result.stdout.strip())
            _fail("the frozen server leaked a FreeCAD process (AD-20)")
    print("  no leaked FreeCAD")
    print("\n  OK: the frozen build works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
