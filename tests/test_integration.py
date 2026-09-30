"""End-to-end tests against a real headless FreeCAD 1.1.

Marked ``integration`` because each test starts a real ``freecadcmd`` process.
Deselect while iterating:  ``uv run pytest -m "not integration"``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from freecad_ai.bridge import (
    BadGeometry,
    BadOperation,
    Bridge,
    BridgeError,
    DocumentExists,
    EmptyResult,
    ExportFailed,
    NoSuchDimension,
    ObjectNotFound,
    PortInUse,
    start_headless,
    stop,
)

PORT = 9876
HOST = "127.0.0.1"


def _freecad_pids() -> set[int]:
    """PIDs of every FreeCAD process on this machine.

    Counts all of them, not just the ones this suite launched, so a leak is
    detectable without the test having to own the process it is watching for.
    Uses tasklist rather than psutil, which is not a dependency.
    """
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq freecadcmd.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


@pytest.fixture(scope="module")
def bridge() -> Iterator[Bridge]:
    process, client = start_headless(HOST, PORT, timeout=60.0)
    try:
        yield client
    finally:
        stop(process)


@pytest.fixture
def document(bridge: Bridge) -> str:
    name = "test_doc"
    # The module-scoped bridge is reused across tests, so the document may
    # already exist from an earlier one. new_document is idempotent.
    return str(bridge.new_document(name)["name"])


@pytest.mark.integration
def test_bridge_answers_ping(bridge: Bridge) -> None:
    assert bridge.ping() == "pong"


@pytest.mark.integration
def test_reports_freecad_1_1_headless(bridge: Bridge) -> None:
    version = bridge.version()
    assert version["version"][0:2] == ["1", "1"]
    assert version["gui_up"] is False


@pytest.mark.integration
def test_new_document_appears_in_listing(bridge: Bridge, document: str) -> None:
    assert document in bridge.list_documents()


@pytest.mark.integration
def test_add_box_and_read_its_volume(bridge: Bridge, document: str) -> None:
    bridge.add_primitive(
        document, "Part::Box", "Box", {"Length": 10.0, "Width": 20.0, "Height": 30.0}
    )
    objects = bridge.list_objects(document)
    assert any(obj["name"] == "Box" for obj in objects)
    summary = bridge.shape_summary(document, "Box")
    assert summary["volume"] == pytest.approx(6000.0, rel=1e-6)


@pytest.mark.integration
def test_set_property_updates_geometry(bridge: Bridge, document: str) -> None:
    bridge.add_primitive(
        document, "Part::Box", "Sized", {"Length": 1.0, "Width": 1.0, "Height": 1.0}
    )
    assert bridge.shape_summary(document, "Sized")["volume"] == pytest.approx(1.0)
    bridge.set_property(document, "Sized", "Length", 4.0)
    assert bridge.shape_summary(document, "Sized")["volume"] == pytest.approx(4.0)


@pytest.mark.integration
def test_get_properties_omits_null_values(bridge: Bridge, document: str) -> None:
    bridge.add_primitive(document, "Part::Box", "Props", {"Length": 2.0})
    properties = bridge.get_properties(document, "Props")
    # FreeCAD reports dimensions as Quantity, not float; they arrive as
    # {"value": 2.0, "unit": "mm"} rather than being dropped.
    assert properties["Length"] == {"value": 2.0, "unit": "mm"}
    # XML-RPC cannot marshal None, so null-valued properties are dropped
    # entirely rather than sent as null.
    assert all(value is not None for value in properties.values())


@pytest.mark.integration
def test_property_value_round_trips(bridge: Bridge, document: str) -> None:
    """A value read back through get_properties must be writable again."""
    bridge.add_primitive(document, "Part::Box", "RoundTrip", {"Length": 3.0})
    current = bridge.get_properties(document, "RoundTrip")["Length"]
    bridge.set_property(document, "RoundTrip", "Length", current)
    assert bridge.shape_summary(document, "RoundTrip")["volume"] == pytest.approx(300.0)


@pytest.mark.integration
def test_remove_object(bridge: Bridge, document: str) -> None:
    bridge.add_primitive(document, "Part::Box", "Doomed", {"Length": 1.0})
    bridge.remove_object(document, "Doomed")
    assert not any(obj["name"] == "Doomed" for obj in bridge.list_objects(document))


@pytest.fixture
def drilled(bridge: Bridge) -> str:
    """A 10x10x10 box with a 2mm-radius hole through the Z axis.

    Expected volumes, from the FreeCAD 1.1 solver: box 1000, cylinder through
    the full height pi*2^2*10 = 125.664.

    Each test gets its own document. A shared one would accumulate primitives
    and boolean features, and every `recompute` would walk the whole growing
    tree.
    """
    document = bridge.new_document(f"drilled_{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document, "Part::Box", "Plate", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.add_primitive(
        document,
        "Part::Cylinder",
        "Bit",
        {"Radius": 2.0, "Height": 20.0, "Angle": 360.0},
    )
    # Centre the 20mm bit on the 10mm plate so it passes right through.
    bridge.set_placement(document, "Bit", 4.0, 4.0, -5.0)
    return document


@pytest.mark.integration
def test_boolean_cut_removes_material(bridge: Bridge, drilled: str) -> None:
    bridge.boolean_op(drilled, "Plate", "Bit", "cut", "Hole")
    summary = bridge.shape_summary(drilled, "Hole")
    assert summary["volume"] == pytest.approx(1000.0 - 125.664, abs=0.01)


@pytest.mark.integration
def test_boolean_fuse_adds_material(bridge: Bridge, drilled: str) -> None:
    bridge.boolean_op(drilled, "Plate", "Bit", "common", "Overlap")
    common = bridge.shape_summary(drilled, "Overlap")["volume"]
    assert common == pytest.approx(125.664, abs=0.01)

    bridge.boolean_op(drilled, "Plate", "Bit", "fuse", "Welded")
    assert bridge.shape_summary(drilled, "Welded")["volume"] == pytest.approx(
        1125.664, abs=0.01
    )


@pytest.mark.integration
def test_boolean_keeps_its_inputs(bridge: Bridge, drilled: str) -> None:
    bridge.boolean_op(drilled, "Plate", "Bit", "cut", "Kept")
    names = {obj["name"] for obj in bridge.list_objects(drilled)}
    assert {"Plate", "Bit", "Kept"} <= names
    assert bridge.shape_summary(drilled, "Plate")["volume"] == pytest.approx(1000.0)


@pytest.mark.integration
def test_boolean_rejects_unknown_operation(bridge: Bridge, drilled: str) -> None:
    with pytest.raises(BadOperation, match="unsupported boolean operation"):
        bridge.boolean_op(drilled, "Plate", "Bit", "xor", "Bad")


@pytest.mark.integration
def test_boolean_rejects_identical_inputs(bridge: Bridge, drilled: str) -> None:
    with pytest.raises(BadOperation, match="must be different"):
        bridge.boolean_op(drilled, "Plate", "Plate", "cut", "Bad")


@pytest.mark.integration
def test_boolean_result_is_exportable(
    bridge: Bridge, drilled: str, tmp_path: Path
) -> None:
    bridge.boolean_op(drilled, "Plate", "Bit", "cut", "Exportable")
    target = tmp_path / "drilled.step"
    bridge.export_object(drilled, "Exportable", str(target))
    assert target.stat().st_size > 0


@pytest.mark.integration
def test_list_results_survive_server_conversion() -> None:
    """A multi-element result must reach the model intact.

    Calling the Bridge directly is not enough: the server layer re-encodes
    results, and a bare list is treated as a sequence of content blocks, so
    only the first element survives. This exercises the real path.
    """
    from freecad_ai.server import server

    async def run() -> None:
        await server.call_tool("new_document", {"name": "wraptest"})
        for i in range(3):
            await server.call_tool(
                "add_primitive",
                {
                    "document": "wraptest",
                    "kind": "Part::Box",
                    "object_name": f"W{i}",
                    "dimensions": {"Length": 10.0},
                },
            )
        result = await server.call_tool("list_objects", {"document": "wraptest"})
        payload = json.loads(result.content[0].text)
        assert isinstance(payload, dict), f"got {type(payload).__name__}, want dict"
        assert len(payload["objects"]) == 3, payload
        assert {o["name"] for o in payload["objects"]} == {"W0", "W1", "W2"}

        docs = json.loads(
            (await server.call_tool("list_documents", {})).content[0].text
        )
        assert "wraptest" in docs["documents"]

    asyncio.run(run())


@pytest.mark.integration
def test_concurrent_calls_from_many_threads_all_succeed(bridge: Bridge) -> None:
    """Parallel calls must not fail.

    MCPServer runs synchronous tool functions on anyio's worker thread pool,
    so concurrent tool calls reach the shared ServerProxy from several
    threads. Its single HTTPConnection raises CannotSendRequest or
    ResponseNotReady instead of queueing, and every parallel call fails.
    A purely sequential suite never sees this.
    """
    document = bridge.new_document("conc")["name"]

    def add(i: int) -> str:
        return bridge.add_primitive(document, "Part::Box", f"B{i}", {"Length": 10.0})

    with ThreadPoolExecutor(max_workers=8) as pool:
        names = [f.result() for f in [pool.submit(add, i) for i in range(8)]]

    assert len(names) == 8
    assert len(set(names)) == 8, "concurrent adds collided on object names"

    def read() -> str:
        return str(bridge.list_documents())

    with ThreadPoolExecutor(max_workers=8) as pool:
        docs = [f.result() for f in [pool.submit(read) for _ in range(8)]]
    assert all(document in d for d in docs)


@pytest.mark.integration
def test_open_document_round_trips_through_disk(bridge: Bridge, tmp_path: Path) -> None:
    """The one bridge function that had no behavioural coverage at all."""
    source = bridge.new_document("to_save")["name"]
    bridge.add_primitive(
        source, "Part::Box", "Saved", {"Length": 5.0, "Width": 5.0, "Height": 5.0}
    )
    target = tmp_path / "stored.FCStd"
    bridge.save_document(source, str(target))
    assert target.is_file()

    # A fresh FreeCAD process cannot see the in-memory document, so opening
    # the saved file is the only way to prove it reached disk.
    process, other = start_headless(HOST, PORT + 2, timeout=60.0)
    try:
        assert source not in other.list_documents()
        opened = other.open_document(str(target))
        objects = other.list_objects(opened)
        assert any(obj["name"] == "Saved" for obj in objects)
        volume = other.shape_summary(opened, "Saved")["volume"]
        assert volume == pytest.approx(125.0, abs=0.01)
    finally:
        stop(process)


@pytest.mark.integration
def test_open_missing_file_raises(bridge: Bridge, tmp_path: Path) -> None:
    with pytest.raises(BridgeError):
        bridge.open_document(str(tmp_path / "nope.FCStd"))


@pytest.mark.integration
def test_list_primitive_types_reports_real_properties(bridge: Bridge) -> None:
    types = bridge.list_primitive_types()
    by_type = {entry["type"]: entry["properties"] for entry in types}
    assert "Part::Box" in by_type
    assert {"Length", "Width", "Height"} <= set(by_type["Part::Box"])
    assert "Part::Cylinder" in by_type
    assert "Radius" in by_type["Part::Cylinder"]
    # Probed, not hardcoded: a type this build lacks must be absent, not
    # reported and then rejected.
    for entry in types:
        assert entry["properties"], f"{entry['type']} reported no properties"
    assert "Part::Tube" not in by_type, "1.1.3 has no Part::Tube"


@pytest.mark.integration
def test_primitive_catalogue_matches_what_can_be_added(bridge: Bridge) -> None:
    """Every advertised type must actually be creatable."""
    for index, entry in enumerate(bridge.list_primitive_types()):
        document = bridge.new_document(f"cat{index}")["name"]
        name = f"T{index}"
        assert bridge.add_primitive(document, entry["type"], name, {}) == name


@pytest.mark.integration
def test_refuses_to_adopt_a_foreign_freecad() -> None:
    """A busy port must fail, not silently hand over someone else's session.

    If another FreeCAD already holds the port, the one launched here cannot
    bind and a readiness ping answers from the incumbent — so the client would
    adopt it along with its open documents. Verified by pid.

    Also asserts the rejected FreeCAD is not left running. It could not bind,
    so it is idle and holding nothing, but the caller receives an exception and
    never gets a handle on it. Leaking it here orphans a process per run, and
    because it stays bound to the port the next run fails the same way — the
    leak compounds instead of clearing.
    """
    incumbent, other = start_headless(HOST, PORT + 3, timeout=60.0)
    before = _freecad_pids()
    try:
        other.new_document("SOMEONE_ELSES_WORK")
        with pytest.raises(PortInUse, match="not the pid"):
            start_headless(HOST, PORT + 3, timeout=20.0)
    finally:
        stop(incumbent)

    # The rejected process is gone. Allow a moment for the port to be released
    # so a slow teardown is not reported as a leak.
    deadline = time.monotonic() + 10.0
    leaked: set[int] = set()
    while time.monotonic() < deadline:
        leaked = _freecad_pids() - before
        if not leaked:
            break
        time.sleep(0.25)
    assert not leaked, (
        f"FreeCAD processes left running after a rejected start: {sorted(leaked)}. "
        f"start_headless must stop the process it launched before raising."
    )


@pytest.mark.integration
def test_connected_bridge_is_the_process_we_started() -> None:
    process, bridge = start_headless(HOST, PORT + 4, timeout=60.0)
    try:
        assert bridge.instance_pid() == process.pid
    finally:
        stop(process)


@pytest.fixture
def block20(bridge: Bridge) -> str:
    """A 20x20x20 box. Edge1 is the vertical edge (0,0,0)-(0,0,20)."""
    document = bridge.new_document(f"blk{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document,
        "Part::Box",
        "B",
        {"Length": 20.0, "Width": 20.0, "Height": 20.0},
    )
    return document


@pytest.mark.integration
def test_describe_geometry_lists_edges_and_faces(bridge: Bridge, block20: str) -> None:
    geometry = bridge.describe_geometry(block20, "B")
    assert geometry["edge_count"] == 12
    assert geometry["face_count"] == 6
    assert len(geometry["edges"]) == 12
    # Names must be exactly what fillet and chamfer accept.
    assert geometry["edges"][0]["name"] == "Edge1"
    assert geometry["edges"][0]["index"] == 1
    assert geometry["edges"][0]["type"] == "Line"
    assert geometry["edges"][0]["length"] == pytest.approx(20.0)
    assert geometry["edges"][0]["start"] == [0.0, 0.0, 20.0]
    assert geometry["faces"][0]["name"] == "Face1"
    assert geometry["faces"][0]["area"] == pytest.approx(400.0)


@pytest.mark.integration
def test_fillet_removes_material(bridge: Bridge, block20: str) -> None:
    """Volume measured against FreeCAD 1.1.3, not derived by hand."""
    assert bridge.shape_summary(block20, "B")["volume"] == pytest.approx(8000.0)
    bridge.fillet(block20, "B", "Rounded", [1], 3.0)
    assert bridge.shape_summary(block20, "Rounded")["volume"] == pytest.approx(
        7961.372, abs=0.01
    )
    assert bridge.shape_summary(block20, "B")["volume"] == pytest.approx(8000.0)


@pytest.mark.integration
def test_fillet_two_edges(bridge: Bridge, block20: str) -> None:
    bridge.fillet(block20, "B", "Two", [1, 3], 2.0)
    assert bridge.shape_summary(block20, "Two")["volume"] == pytest.approx(
        7965.664, abs=0.01
    )


@pytest.mark.integration
def test_chamfer_removes_material(bridge: Bridge, block20: str) -> None:
    bridge.chamfer(block20, "B", "Cut", [1], 2.0)
    assert bridge.shape_summary(block20, "Cut")["volume"] == pytest.approx(
        7960.0, abs=0.01
    )


@pytest.mark.integration
def test_fillet_rejects_a_missing_edge(bridge: Bridge, block20: str) -> None:
    with pytest.raises(BadGeometry, match="does not exist"):
        bridge.fillet(block20, "B", "Nope", [99], 2.0)


@pytest.mark.integration
def test_fillet_rejects_a_non_positive_radius(bridge: Bridge, block20: str) -> None:
    with pytest.raises(BadGeometry, match="radius must be positive"):
        bridge.fillet(block20, "B", "Nope", [1], 0.0)


@pytest.mark.integration
def test_fillet_rejects_an_unfittable_radius(bridge: Bridge, block20: str) -> None:
    """A radius larger than the box cannot be built; say so, do not crash."""
    with pytest.raises(BadGeometry, match="does not fit"):
        bridge.fillet(block20, "B", "Nope", [1], 500.0)


@pytest.mark.integration
def test_mirror_reflects_across_a_plane(bridge: Bridge) -> None:
    document = bridge.new_document(f"mir{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document, "Part::Box", "B", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    # Reflect through the x=0 plane: the copy lands at negative x.
    bridge.mirror(document, "B", "Flipped", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0])
    box = bridge.shape_summary(document, "Flipped")["bbox"]
    assert box[0] == pytest.approx(-10.0)
    assert box[3] == pytest.approx(0.0)


@pytest.mark.integration
def test_mirror_rejects_a_zero_normal(bridge: Bridge) -> None:
    document = bridge.new_document(f"mir0{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(document, "Part::Box", "B", {"Length": 10.0})
    with pytest.raises(BadGeometry, match="zero vector"):
        bridge.mirror(document, "B", "Nope", [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])


@pytest.mark.integration
def test_linear_array_multiplies_volume(bridge: Bridge) -> None:
    document = bridge.new_document(f"arr{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document, "Part::Box", "B", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.linear_array(document, "B", "Row", [30.0, 0.0, 0.0], 3)
    # 30mm apart, boxes are 10mm, so three copies never touch.
    assert bridge.shape_summary(document, "Row")["volume"] == pytest.approx(
        3000.0, rel=1e-3
    )


@pytest.mark.integration
def test_linear_array_rejects_a_count_of_one(bridge: Bridge) -> None:
    document = bridge.new_document(f"arr1{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(document, "Part::Box", "B", {"Length": 10.0})
    with pytest.raises(BadGeometry, match="count of 1"):
        bridge.linear_array(document, "B", "Nope", [10.0, 0.0, 0.0], 1)


@pytest.mark.integration
def test_fillet_then_export(bridge: Bridge, block20: str, tmp_path: Path) -> None:
    bridge.fillet(block20, "B", "Rounded", [1], 3.0)
    target = tmp_path / "rounded.step"
    bridge.export_object(block20, "Rounded", str(target))
    assert target.stat().st_size > 0


@pytest.fixture
def two_boxes(bridge: Bridge) -> str:
    """Two 10mm boxes 25mm apart on X, so the gap between them is 15mm."""
    document = bridge.new_document(f"gap{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document, "Part::Box", "A", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.add_primitive(
        document, "Part::Box", "B", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.set_placement(document, "B", 25.0, 0.0, 0.0)
    return document


@pytest.mark.integration
def test_measure_reports_mass_properties(bridge: Bridge, block20: str) -> None:
    """Values measured against FreeCAD 1.1.3."""
    facts = bridge.measure(block20, "B")
    assert facts["shape_type"] == "Solid"
    assert facts["is_valid"] is True
    assert facts["is_closed"] is True
    assert facts["volume"] == pytest.approx(8000.0)
    assert facts["area"] == pytest.approx(2400.0)
    assert facts["center_of_mass"] == pytest.approx([10.0, 10.0, 10.0])
    assert facts["bounding_box"] == pytest.approx([0, 0, 0, 20, 20, 20])
    assert facts["solid_count"] == 1
    assert facts["face_count"] == 6
    assert facts["edge_count"] == 12
    assert facts["vertex_count"] == 8


@pytest.mark.integration
def test_distance_between_two_objects(bridge: Bridge, two_boxes: str) -> None:
    result = bridge.distance(two_boxes, "A", second="B")
    assert result["distance"] == pytest.approx(15.0)
    # The closest points lie on the facing faces. Y and Z are not asserted:
    # many points are equidistant and FreeCAD picks one of them.
    assert result["point_on_first"][0] == pytest.approx(10.0)
    assert result["point_on_second"][0] == pytest.approx(25.0)


@pytest.mark.integration
def test_distance_point_to_object(bridge: Bridge, two_boxes: str) -> None:
    result = bridge.distance(two_boxes, "A", point=[0.0, 0.0, 50.0])
    assert result["distance"] == pytest.approx(40.0)
    assert "point_on_object" in result


@pytest.mark.integration
def test_distance_of_overlapping_objects_is_zero(
    bridge: Bridge, two_boxes: str
) -> None:
    bridge.set_placement(two_boxes, "B", 5.0, 0.0, 0.0)
    assert bridge.distance(two_boxes, "A", second="B")["distance"] == pytest.approx(
        0.0, abs=1e-6
    )


@pytest.mark.integration
def test_distance_needs_exactly_one_target(bridge: Bridge, two_boxes: str) -> None:
    with pytest.raises(BadGeometry, match="exactly one"):
        bridge.distance(two_boxes, "A")
    with pytest.raises(BadGeometry, match="exactly one"):
        bridge.distance(two_boxes, "A", second="B", point=[0.0, 0.0, 0.0])


@pytest.mark.integration
def test_is_inside(bridge: Bridge, two_boxes: str) -> None:
    assert bridge.is_inside(two_boxes, "A", [5.0, 5.0, 5.0])["inside"] is True
    assert bridge.is_inside(two_boxes, "A", [50.0, 50.0, 50.0])["inside"] is False


@pytest.mark.integration
def test_cross_section_area(bridge: Bridge, block20: str) -> None:
    section = bridge.cross_section(block20, "B", [0.0, 0.0, 1.0], 10.0)
    assert section["wire_count"] == 1
    assert section["area"] == pytest.approx(400.0)


@pytest.mark.integration
def test_cross_section_that_misses_is_reported(bridge: Bridge, block20: str) -> None:
    with pytest.raises(EmptyResult, match="does not cut"):
        bridge.cross_section(block20, "B", [0.0, 0.0, 1.0], 500.0)


@pytest.mark.integration
def test_cross_section_rejects_a_zero_normal(bridge: Bridge, block20: str) -> None:
    with pytest.raises(BadGeometry, match="zero vector"):
        bridge.cross_section(block20, "B", [0.0, 0.0, 0.0], 10.0)


@pytest.mark.integration
def test_empty_intersection_is_not_silently_accepted(bridge: Bridge) -> None:
    """A `common` of shapes that never touch must fail, not return 0 volume.

    FreeCAD hands back a valid Compound with no solids in it, so an isNull
    check alone lets an empty result through as if it were a real one.
    """
    document = bridge.new_document(f"far{uuid.uuid4().hex[:8]}")["name"]
    bridge.add_primitive(
        document, "Part::Box", "A", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.add_primitive(
        document, "Part::Box", "B", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    bridge.set_placement(document, "B", 100.0, 0.0, 0.0)
    with pytest.raises(EmptyResult, match="do not intersect"):
        bridge.boolean_op(document, "A", "B", "common", "Nothing")

    # Fusing them is legitimate and must still work.
    bridge.boolean_op(document, "A", "B", "fuse", "Both")
    assert bridge.measure(document, "Both")["solid_count"] == 2


@pytest.mark.integration
def test_new_document_is_idempotent(bridge: Bridge) -> None:
    first = bridge.new_document("idem")
    assert first["created"] is True
    second = bridge.new_document("idem")
    assert second["created"] is False
    assert second["name"] == first["name"]


@pytest.mark.integration
def test_new_document_can_refuse_reuse(bridge: Bridge) -> None:
    bridge.new_document("strict")
    with pytest.raises(DocumentExists):
        bridge.new_document("strict", reuse=False)


@pytest.mark.integration
def test_round_trip_through_disk(bridge: Bridge, tmp_path: Path) -> None:
    document = bridge.new_document("roundtrip")["name"]
    bridge.add_primitive(document, "Part::Box", "Saved", {"Length": 5.0})
    target = tmp_path / "roundtrip.FCStd"
    bridge.save_document(document, str(target))
    assert target.is_file()


@pytest.mark.integration
@pytest.mark.parametrize("suffix", [".step", ".stl", ".iges", ".obj"])
def test_export_writes_a_file(bridge: Bridge, tmp_path: Path, suffix: str) -> None:
    document = bridge.new_document("exported")["name"]
    bridge.add_primitive(
        document, "Part::Box", "Part", {"Length": 2.0, "Width": 2.0, "Height": 2.0}
    )
    target = tmp_path / f"part{suffix}"
    written = bridge.export_object(document, "Part", str(target))
    assert Path(written).is_file()
    assert Path(written).stat().st_size > 0


@pytest.mark.integration
def test_export_rejects_unsupported_format(bridge: Bridge, tmp_path: Path) -> None:
    document = bridge.new_document("badext")["name"]
    bridge.add_primitive(document, "Part::Box", "Part", {"Length": 1.0})
    with pytest.raises(ExportFailed, match="unsupported export format"):
        bridge.export_object(document, "Part", str(tmp_path / "part.xyz"))


@pytest.mark.integration
def test_export_reports_missing_directory_as_user_error(
    bridge: Bridge, tmp_path: Path
) -> None:
    """A missing directory is the caller's mistake, not a server bug."""
    document = bridge.new_document("nodir")["name"]
    bridge.add_primitive(document, "Part::Box", "Part", {"Length": 1.0})
    with pytest.raises(ExportFailed, match="directory does not exist"):
        bridge.export_object(document, "Part", str(tmp_path / "absent" / "part.step"))


@pytest.mark.integration
def test_unknown_object_raises_object_not_found(bridge: Bridge, document: str) -> None:
    with pytest.raises(ObjectNotFound):
        bridge.shape_summary(document, "no_such_object")


@pytest.mark.integration
def test_bad_dimension_lists_real_properties(bridge: Bridge, document: str) -> None:
    with pytest.raises(NoSuchDimension, match="available"):
        bridge.add_primitive(document, "Part::Box", "Bad", {"Depth": 1.0})


@pytest.mark.integration
def test_unknown_document_raises_bridge_error(bridge: Bridge) -> None:
    with pytest.raises(BridgeError, match="no such document"):
        bridge.list_objects("does_not_exist")


@pytest.mark.integration
def test_unknown_property_raises_bridge_error(bridge: Bridge, document: str) -> None:
    bridge.add_primitive(document, "Part::Box", "Guarded", {"Length": 1.0})
    with pytest.raises(BridgeError, match="no such property"):
        bridge.set_property(document, "Guarded", "NotAProperty", 1)


@pytest.mark.integration
def test_start_headless_reports_process_and_client() -> None:
    process, client = start_headless(HOST, PORT + 1, timeout=60.0)
    try:
        assert isinstance(process, subprocess.Popen)
        assert client.ping() == "pong"
    finally:
        stop(process)
