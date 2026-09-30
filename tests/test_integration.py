"""End-to-end tests against a real headless FreeCAD 1.1.

Marked ``integration`` because each test starts a real ``freecadcmd`` process.
Deselect while iterating:  ``uv run pytest -m "not integration"``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from freecad_ai.bridge import (
    BadOperation,
    Bridge,
    BridgeError,
    DocumentExists,
    ExportFailed,
    NoSuchDimension,
    ObjectNotFound,
    start_headless,
    stop,
)

PORT = 9876
HOST = "127.0.0.1"


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
    """
    document = bridge.new_document("drilled")["name"]
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
