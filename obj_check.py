"""Check whether concurrent addObject calls actually persist every object."""

import asyncio
import json

from freecad_ai.server import server


async def main() -> None:
    r = await server.call_tool("new_document", {"name": "verify"})
    print("new_document:", r.content[0].text.strip().replace("\n", " "))

    async def add(i: int) -> str:
        r = await server.call_tool(
            "add_primitive",
            {
                "document": "verify",
                "kind": "Part::Box",
                "object_name": f"P{i}",
                "dimensions": {"Length": 10.0},
            },
        )
        return str(r.content[0].text).strip()

    names = await asyncio.gather(*[add(i) for i in range(8)])
    print("returned names:", names)

    r = await server.call_tool("list_objects", {"document": "verify"})
    payload = json.loads(r.content[0].text)
    if isinstance(payload, dict):
        print("list_objects ERROR PAYLOAD:", payload)
    else:
        print("listed        :", sorted(o["name"] for o in payload))
    print("count returned:", len(names), " count listed:",
          len(payload) if isinstance(payload, list) else "n/a")

    r = await server.call_tool("list_documents", {})
    print("documents:", r.content[0].text.strip().replace("\n", " "))

    # Repeat, sequentially, for comparison.
    r = await server.call_tool("new_document", {"name": "seq"})
    for i in range(8):
        await server.call_tool(
            "add_primitive",
            {
                "document": "seq",
                "kind": "Part::Box",
                "object_name": f"S{i}",
                "dimensions": {"Length": 10.0},
            },
        )
    r = await server.call_tool("list_objects", {"document": "seq"})
    seq = json.loads(r.content[0].text)
    print("sequential    :", len(seq) if isinstance(seq, list) else seq)


asyncio.run(main())
