"""Reproduce the parallel-tool-call failure through the real server path."""

import asyncio
import time

from freecad_ai.server import server


async def main() -> None:
    await server.call_tool("new_document", {"name": "conc"})

    async def add(i: int) -> str:
        r = await server.call_tool(
            "add_primitive",
            {
                "document": "conc",
                "kind": "Part::Box",
                "object_name": f"B{i}",
                "dimensions": {"Length": 10.0, "Width": 10.0, "Height": 10.0},
            },
        )
        return str(r.content[0].text).strip()

    async def read() -> str:
        r = await server.call_tool("list_documents", {})
        return str(r.content[0].text).strip()

    print("--- 8 parallel add_primitive via server.call_tool ---")
    start = time.monotonic()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[add(i) for i in range(8)], return_exceptions=True),
            timeout=90,
        )
        ok = [r for r in results if isinstance(r, str) and not r.startswith("{")]
        bad = [r for r in results if r not in ok]
        print(f"  ok {len(ok)}/8  elapsed {time.monotonic() - start:.1f}s")
        for b in bad[:3]:
            print("   ERR:", type(b).__name__, str(b)[:90])
    except TimeoutError:
        print("  *** HANG ***")

    print("--- 16 parallel list_documents ---")
    start = time.monotonic()
    try:
        rs = await asyncio.wait_for(
            asyncio.gather(*[read() for _ in range(16)], return_exceptions=True),
            timeout=90,
        )
        good = [r for r in rs if not isinstance(r, BaseException)]
        print(f"  ok {len(good)}/16  elapsed {time.monotonic() - start:.1f}s")
        for r in [r for r in rs if isinstance(r, BaseException)][:3]:
            print("   ERR:", type(r).__name__, str(r)[:90])
    except TimeoutError:
        print("  *** HANG ***")

    r = await server.call_tool("list_objects", {"document": "conc"})
    import json

    print("objects created:", len(json.loads(r.content[0].text)))


asyncio.run(main())
