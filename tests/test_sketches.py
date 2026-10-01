"""Sketch tests against a real headless FreeCAD 1.1.

Every number here was measured from FreeCAD, not derived on paper. The two
analytic checks use closed-form values for a rounded rectangle, so they would
catch a regression in either direction rather than merely confirming the tool
still returns whatever it returned before.

Marked ``integration``: each test starts a real ``freecadcmd`` process.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from freecad_ai.bridge import (
    BadGeometry,
    Bridge,
    EmptyResult,
    NoSuchFace,
    NotASketch,
    ProfileNotClosed,
    start_headless,
    stop,
)

PORT = 9882
HOST = "127.0.0.1"

pytestmark = pytest.mark.integration

# A rounded rectangle: four lines joined by four quarter-circle arcs.
# The area of the shape it traces is exactly W*H - (4 - pi)*R^2.
WIDTH, HEIGHT, CORNER, DEPTH = 40.0, 20.0, 5.0, 4.0
ROUNDED_AREA = WIDTH * HEIGHT - (4 - math.pi) * CORNER**2


@pytest.fixture(scope="module")
def bridge() -> Iterator[Bridge]:
    process, client = start_headless(HOST, PORT, timeout=90.0)
    try:
        yield client
    finally:
        stop(process)


@pytest.fixture
def doc(bridge: Bridge) -> str:
    name = f"sk{uuid.uuid4().hex[:8]}"
    bridge.new_document(name)
    return name


def _rounded_rectangle(bridge: Bridge, document: str, sketch: str) -> None:
    """Draw the rounded rectangle described by the module constants.

    Arcs run anticlockwise from +X in degrees, which is the convention
    add_sketch_arc documents. Getting these spans wrong is the usual way a
    profile fails to close, so the sequence is written out rather than looped.
    """
    w, h, r = WIDTH, HEIGHT, CORNER
    bridge.add_sketch_line(document, sketch, r, 0, w - r, 0)
    bridge.add_sketch_arc(document, sketch, w - r, r, r, -90, 0)
    bridge.add_sketch_line(document, sketch, w, r, w, h - r)
    bridge.add_sketch_arc(document, sketch, w - r, h - r, r, 0, 90)
    bridge.add_sketch_line(document, sketch, w - r, h, r, h)
    bridge.add_sketch_arc(document, sketch, r, h - r, r, 90, 180)
    bridge.add_sketch_line(document, sketch, 0, h - r, 0, r)
    bridge.add_sketch_arc(document, sketch, r, r, r, 180, 270)


# -- CAP-S1: create a sketch and add geometry -------------------------------


def test_sketch_geometry_can_be_added(bridge: Bridge, doc: str) -> None:
    """A closed four-line rectangle: one closed wire, four edges."""
    assert bridge.add_sketch(doc, "Rect") == "Rect"
    for x1, y1, x2, y2 in (
        (0, 0, 40, 0),
        (40, 0, 40, 20),
        (40, 20, 0, 20),
        (0, 20, 0, 0),
    ):
        result = bridge.add_sketch_line(doc, "Rect", x1, y1, x2, y2)
        assert result["index"] == result["geometry_count"]

    status = bridge.sketch_status(doc, "Rect")
    assert status["geometry_count"] == 4
    assert status["closed"] is True
    assert status["edge_count"] == 4
    assert status["area"] == pytest.approx(800.0, abs=1e-6)


def test_arc_and_circle_can_be_added(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Mixed")
    bridge.add_sketch_line(doc, "Mixed", 0, 0, 20, 0)
    bridge.add_sketch_arc(doc, "Mixed", 10, 10, 10, 180, 360)
    bridge.add_sketch_circle(doc, "Mixed", 5, 5, 2)
    status = bridge.sketch_status(doc, "Mixed")
    assert status["geometry_count"] == 3
    # Not closed: the circle is a separate wire from the line-and-arc run.
    assert status["closed"] is False


# -- CAP-S2: a closed profile becomes a solid -------------------------------


def test_closed_profile_extrudes_to_a_solid(bridge: Bridge, doc: str) -> None:
    """The spec's worked example: 40x20 extruded 4mm is 3200mm3.

    Volume is the analytic cross-check. A wrong solid is the failure this whole
    feature guards against, so the number that matters is the one FreeCAD
    computes for a shape we can reason about independently.
    """
    bridge.add_sketch(doc, "Rect")
    for x1, y1, x2, y2 in (
        (0, 0, 40, 0),
        (40, 0, 40, 20),
        (40, 20, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Rect", x1, y1, x2, y2)
    bridge.extrude_sketch(doc, "Rect", "Plate", 4.0)

    measured = bridge.measure(doc, "Plate")
    assert measured["volume"] == pytest.approx(3200.0, abs=1e-6)
    assert measured["solid_count"] == 1


def test_rounded_rectangle_area_matches_the_closed_form(
    bridge: Bridge, doc: str
) -> None:
    """A profile with arcs: area and volume against the analytic value.

    The strongest check available, because the expected value comes from
    geometry rather than from a previous run of this tool.
    """
    bridge.add_sketch(doc, "Rounded")
    _rounded_rectangle(bridge, doc, "Rounded")

    status = bridge.sketch_status(doc, "Rounded")
    assert status["closed"] is True
    assert status["edge_count"] == 8
    assert status["area"] == pytest.approx(ROUNDED_AREA, abs=1e-6)

    bridge.extrude_sketch(doc, "Rounded", "Solid", DEPTH)
    measured = bridge.measure(doc, "Solid")
    assert measured["volume"] == pytest.approx(ROUNDED_AREA * DEPTH, abs=1e-4)


# -- CAP-S3: closedness is checkable, and an open profile is refused --------


def test_open_profile_is_refused_rather_than_extruded(bridge: Bridge, doc: str) -> None:
    """The trap that made this a capability: FreeCAD extrudes an open profile
    into a *wrong solid* rather than raising. The server must refuse it.

    Probed before the tools were written: a five-edge profile with a mis-spanned
    arc came out not closed, and Part::Extrusion with Solid=True returned a
    shape with a 50mm edge where 20mm had been drawn.
    """
    bridge.add_sketch(doc, "Open")
    bridge.add_sketch_line(doc, "Open", 0, 0, 40, 0)
    bridge.add_sketch_line(doc, "Open", 40, 0, 40, 20)
    bridge.add_sketch_line(doc, "Open", 40, 20, 0, 20)

    status = bridge.sketch_status(doc, "Open")
    assert status["closed"] is False
    assert status["area"] == 0.0

    with pytest.raises(ProfileNotClosed):
        bridge.extrude_sketch(doc, "Open", "Wrong", 4.0)
    # And nothing was created, so the caller cannot model against it.
    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert "Wrong" not in names


def test_profile_not_closed_names_the_endpoints(bridge: Bridge, doc: str) -> None:
    """The error has to be actionable, so it carries the coordinates."""
    bridge.add_sketch(doc, "Gap")
    bridge.add_sketch_line(doc, "Gap", 0, 0, 40, 0)
    bridge.add_sketch_line(doc, "Gap", 40, 0, 40, 20)
    with pytest.raises(ProfileNotClosed) as caught:
        bridge.extrude_sketch(doc, "Gap", "Nope", 1.0)
    message = str(caught.value)
    assert "not a closed profile" in message
    assert "[[" in message  # the endpoint list
    assert caught.value.hint


def test_empty_sketch_cannot_be_extruded(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Empty")
    with pytest.raises(ProfileNotClosed):
        bridge.extrude_sketch(doc, "Empty", "Nothing", 1.0)


# -- CAP-S4: constraints ----------------------------------------------------


def test_constraints_can_be_added_and_solved(bridge: Bridge, doc: str) -> None:
    """A Coincident constraint between two lines reduces the degrees of freedom.

    Element indices are 1-based, like every other index in the surface. They
    were 0-based because FreeCAD numbers geometry internally from zero, and
    that split was the last one standing; the translation now happens once, in
    the bridge.
    """
    bridge.add_sketch(doc, "C")
    bridge.add_sketch_line(doc, "C", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "C", 10, 0, 10, 10)

    result = bridge.add_sketch_constraint(doc, "C", "Coincident", 1, 2, 2, 1, 0.0)
    assert result["constraint_count"] == 1
    assert result["dof"] >= 0
    assert result["fully_constrained"] is False


def test_over_constrained_sketch_is_reported(bridge: Bridge, doc: str) -> None:
    """A negative dof means the sketch fights itself, and the caller is told.

    FreeCAD adds the constraint anyway and reports a negative dof rather than
    refusing, so surfacing it is the only way the caller finds out.
    """
    bridge.add_sketch(doc, "Over")
    bridge.add_sketch_line(doc, "Over", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "Over", 10, 0, 10, 10)
    result = bridge.add_sketch_constraint(doc, "Over", "Coincident", 1, 2, 2, 1, 0.0)
    bridge.add_sketch_constraint(doc, "Over", "Coincident", 1, 2, 2, 1, 0.0)
    status = bridge.sketch_status(doc, "Over")
    assert status["over_constrained"] is True
    assert status["dof"] < 0
    assert result["constraint_count"] == 1


def test_unknown_constraint_kind_is_refused_without_crashing(
    bridge: Bridge, doc: str
) -> None:
    """Building a Constraint with the wrong arity *terminates FreeCAD*.

    Verified against 1.1.3: the 4-argument form returns a Constraint, the
    6-argument form kills the interpreter outright. An unknown kind must
    therefore be rejected before any Constraint object is built, and the bridge
    must still answer afterwards.
    """
    bridge.add_sketch(doc, "Bad")
    bridge.add_sketch_line(doc, "Bad", 0, 0, 10, 0)
    with pytest.raises(BadGeometry):
        bridge.add_sketch_constraint(doc, "Bad", "Nonsense", 0, 1, 0, 2, 1.0)
    assert bridge.ping() == "pong"


def test_out_of_range_constraint_index_is_refused(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Range")
    bridge.add_sketch_line(doc, "Range", 0, 0, 10, 0)
    with pytest.raises(BadGeometry) as caught:
        bridge.add_sketch_constraint(doc, "Range", "Coincident", 99, 1, 1, 2, 0.0)
    message = str(caught.value)
    assert "99" in message and "does not exist" in message
    assert "1-based" in message, (
        "the refusal must state the convention, since a caller who assumed "
        "FreeCAD's 0-based numbering would otherwise have no way to tell"
    )


# -- CAP-S5: edit and remove -----------------------------------------------


def test_geometry_can_be_removed_and_renumbered(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Edit")
    for x1, y1, x2, y2 in (
        (0, 0, 40, 0),
        (40, 0, 40, 20),
        (40, 20, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Edit", x1, y1, x2, y2)
    assert bridge.sketch_status(doc, "Edit")["closed"] is True

    bridge.remove_sketch_geometry(doc, "Edit", 4)
    status = bridge.sketch_status(doc, "Edit")
    assert status["geometry_count"] == 3
    # Removing the closing line opens the profile, which is the honest result.
    assert status["closed"] is False


def test_removing_a_missing_geometry_is_refused(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "OneLine")
    bridge.add_sketch_line(doc, "OneLine", 0, 0, 10, 0)
    with pytest.raises(BadGeometry):
        bridge.remove_sketch_geometry(doc, "OneLine", 7)


# -- CAP-S6: placement -----------------------------------------------------


def test_sketch_placement_moves_the_solid(bridge: Bridge, doc: str) -> None:
    """Direction is placement, not an argument, so one rule governs orientation.

    Order matters, and this is a real constraint rather than a test artefact: a
    Part::Extrusion takes the sketch's placement at the moment it is created,
    so moving the sketch afterwards does not move a solid already extruded.
    Extrude, then move, then extrude again.
    """
    bridge.add_sketch(doc, "Moved")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Moved", x1, y1, x2, y2)
    bridge.extrude_sketch(doc, "Moved", "AtOrigin", 2.0)
    origin = bridge.shape_summary(doc, "AtOrigin")
    assert origin["bbox"][2] == pytest.approx(0.0, abs=1e-6)

    bridge.set_placement(doc, "Moved", 0, 0, 50)
    bridge.extrude_sketch(doc, "Moved", "Raised", 2.0)

    raised = bridge.shape_summary(doc, "Raised")
    assert raised["volume"] == pytest.approx(origin["volume"], abs=1e-6)
    # bbox is [xmin, ymin, zmin, xmax, ymax, zmax]
    assert raised["bbox"][2] == pytest.approx(50.0, abs=1e-6)
    assert raised["bbox"][5] == pytest.approx(52.0, abs=1e-6)


# -- CAP-S7: a sketch-derived solid is an ordinary object -------------------


def test_extruded_sketch_works_with_the_existing_tools(
    bridge: Bridge, doc: str
) -> None:
    """No new arguments anywhere: the whole existing surface applies unchanged."""
    bridge.add_sketch(doc, "Reuse")
    for x1, y1, x2, y2 in (
        (0, 0, 40, 0),
        (40, 0, 40, 20),
        (40, 20, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Reuse", x1, y1, x2, y2)
    bridge.extrude_sketch(doc, "Reuse", "Plate", 4.0)

    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert "Plate" in names

    geometry = bridge.describe_geometry(doc, "Plate")
    # A rectangle swept into a box is 12 edges over 6 faces, not 4: each side
    # is split where the corners meet. Measured, not assumed.
    assert geometry["edge_count"] == 12
    assert geometry["face_count"] == 6
    assert geometry["edges"][0]["name"] == "Edge1"

    # Cut a hole using the same boolean_op every other part uses.
    bridge.add_primitive(doc, "Part::Cylinder", "Hole", {"Radius": 3.0, "Height": 10.0})
    bridge.set_placement(doc, "Hole", 20, 10, -3)
    bridge.boolean_op(doc, "Plate", "Hole", "cut", "Drilled")
    drilled = bridge.measure(doc, "Drilled")
    assert drilled["volume"] == pytest.approx(3200.0 - math.pi * 9 * 4, abs=1e-3)

    filled = bridge.fillet(doc, "Drilled", "Rounded", [1, 3], 1.0)
    assert filled == "Rounded"
    assert bridge.measure(doc, "Rounded")["volume"] < drilled["volume"]

    section = bridge.cross_section(doc, "Drilled", [0.0, 0.0, 1.0], 2.0)
    assert section["area"] > 0.0


def test_sketch_can_be_exported(bridge: Bridge, doc: str, tmp_path: Path) -> None:
    bridge.add_sketch(doc, "Export")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Export", x1, y1, x2, y2)
    bridge.extrude_sketch(doc, "Export", "Part", 3.0)
    target = tmp_path / "from_sketch.step"
    bridge.export_object(doc, "Part", str(target))
    assert os.path.getsize(target) > 0


# -- round two: face attachment and face creation ---------------------------


def test_sketch_attaches_flat_to_a_planar_face(bridge: Bridge, doc: str) -> None:
    """A sketch on a box's top face takes that face's position and extrudes
    normal to it.

    Verified before the tool was written: a 30x15 profile attached to Face6 of a
    40x20x4 box moved to z=4 and extruded 2mm gave 900mm3 spanning z 4 to 6.
    """
    bridge.add_primitive(
        doc, "Part::Box", "Box", {"Length": 40, "Width": 20, "Height": 4}
    )
    bridge.add_sketch(doc, "OnFace")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "OnFace", x1, y1, x2, y2)

    result = bridge.attach_sketch_to_face(doc, "OnFace", "Box", 6)
    assert result["map_mode"] == "FlatFace"
    assert result["placement"] == [0.0, 0.0, 4.0]

    bridge.extrude_sketch(doc, "OnFace", "Pad", 2.0)
    summary = bridge.shape_summary(doc, "Pad")
    assert summary["volume"] == pytest.approx(900.0, abs=1e-6)
    # bbox is [xmin, ymin, zmin, xmax, ymax, zmax]; the box top is z=4
    assert summary["bbox"][2] == pytest.approx(4.0, abs=1e-6)
    assert summary["bbox"][5] == pytest.approx(6.0, abs=1e-6)


def test_attaching_to_a_curved_face_is_refused(bridge: Bridge, doc: str) -> None:
    """Only a plane can carry a flat sketch.

    A cylinder's side face is a Cylinder surface, and attaching to it would
    produce a degenerate placement rather than an error.
    """
    bridge.add_primitive(doc, "Part::Cylinder", "Cyl", {"Radius": 10, "Height": 20})
    faces = bridge.describe_geometry(doc, "Cyl")["faces"]
    curved = next(f for f in faces if f["type"] == "Cylinder")
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 5, 0)
    with pytest.raises(NoSuchFace):
        bridge.attach_sketch_to_face(doc, "S", "Cyl", curved["index"])


def test_a_sketch_cannot_be_attached_to_itself(bridge: Bridge, doc: str) -> None:
    """Self-reference is refused before it can wedge FreeCAD.

    A sketch attached to its own face is a parametric link that resolves the
    support by recomputing the object being recomputed. That presents as a hung
    process, not a catchable exception, so the guard is the only thing standing
    between a mistyped target and a wedged bridge.
    """
    bridge.add_sketch(doc, "Self")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Self", x1, y1, x2, y2)
    with pytest.raises(BadGeometry) as caught:
        bridge.attach_sketch_to_face(doc, "Self", "Self", 1)
    assert "itself" in str(caught.value)
    # FreeCAD is still answering, which is the point of refusing early.
    assert bridge.ping() == "pong"


def test_attaching_to_a_curved_face_names_it(bridge: Bridge, doc: str) -> None:
    """The refusal must name the face and the reason.

    A caller choosing between several faces needs to know which was rejected
    and why, or they cannot act on it. The capability claims the refusal
    happens "by name"; this is what holds that to account.
    """
    bridge.add_primitive(doc, "Part::Cylinder", "Cyl", {"Radius": 10, "Height": 20})
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 5, 0)
    faces = bridge.describe_geometry(doc, "Cyl")["faces"]
    curved = next(f for f in faces if f["type"] == "Cylinder")
    with pytest.raises(NoSuchFace) as caught:
        bridge.attach_sketch_to_face(doc, "S", "Cyl", curved["index"])
    message = str(caught.value)
    assert curved["name"] in message
    assert "Cyl" in message
    assert "Cylinder" in message


def test_extrusion_direction_is_the_sketch_normal_not_an_argument(
    bridge: Bridge, doc: str
) -> None:
    """AD-23: depth is the only argument, and Normal already follows rotation.

    The alternative was measured rather than assumed. DirMode="Custom" with a
    direction off the sketch plane produced a *valid shape of volume 0.0* -
    the silent-wrong-result class this project exists to avoid. So the tool
    must not grow a direction argument, and the normal must track the sketch's
    own placement.
    """
    bridge.add_sketch(doc, "Flat")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Flat", x1, y1, x2, y2)
    bridge.set_placement(doc, "Flat", 0, 0, 0, 1, 0, 0, 90)  # 90 degrees about X
    bridge.extrude_sketch(doc, "Flat", "Upright", 2.0)

    summary = bridge.shape_summary(doc, "Upright")
    assert summary["volume"] == pytest.approx(200.0, abs=1e-6)
    # Rotated 90 about X, so the normal lies along Y: the solid grows in Y,
    # not Z. A normal that ignored the rotation would span z 0..2 instead.
    assert summary["bbox"][5] == pytest.approx(10.0, abs=1e-6)
    assert summary["bbox"][2] == pytest.approx(0.0, abs=1e-6)


def test_non_finite_dimensions_are_refused(bridge: Bridge, doc: str) -> None:
    """NaN and infinity terminate FreeCAD rather than raising.

    Verified against 1.1.3: Part::Extrusion with LengthFwd = nan killed the
    interpreter mid-script, taking every open document with it. NaN defeats
    every comparison-based guard because `nan == 0`, `nan < 0` and `nan <= 0`
    are all False, so the refusal has to happen before the value crosses the
    wire. AD-25.
    """
    bridge.add_sketch(doc, "S")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "S", x1, y1, x2, y2)

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(BadGeometry):
            bridge.extrude_sketch(doc, "S", "R", bad)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_line(doc, "S", bad, 0, 1, 1)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_circle(doc, "S", 0, 0, bad)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_arc(doc, "S", 0, 0, 5.0, bad, 90)

    # FreeCAD must still be answering: the whole point is that nothing reached
    # it. A refusal that arrived after the crash would raise BridgeUnreachable
    # instead, which these assertions would also catch.
    assert bridge.ping() == "pong"
    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert names == {"S"}, f"a refused call left {sorted(names)}"


def test_a_failed_extrude_leaves_nothing_behind(bridge: Bridge, doc: str) -> None:
    """A refused extrude must not deposit a Part::Extrusion.

    The orphan bug 839a08e fixed in sketch_to_face was still live here: both
    post-recompute checks ran after _feature created the object, so a
    degenerate profile raised the right error and left a half-built feature
    under the caller's own name. Verified live before the fix - a bowtie
    extrude left ['Bow', 'Orphan'] behind.

    A bowtie is the fixture that matters. An *open* profile is refused by
    _profile_wire before anything is created, and a zero depth by the length
    check, so neither can orphan anything and neither proves anything here.
    """
    bridge.add_sketch(doc, "Bow")
    for x1, y1, x2, y2 in (
        (0, 0, 20, 20),
        (20, 20, 20, 0),
        (20, 0, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Bow", x1, y1, x2, y2)
    assert bridge.sketch_status(doc, "Bow")["closed"] is True

    with pytest.raises(BadGeometry):
        bridge.extrude_sketch(doc, "Bow", "Orphan", 2.0)

    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert "Orphan" not in names, f"a failed extrude left {sorted(names)}"
    # And the name is free, so a retry is not silently suffixed.
    bridge.add_sketch(doc, "Good")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Good", x1, y1, x2, y2)
    assert bridge.extrude_sketch(doc, "Good", "Orphan", 2.0) == "Orphan"


def test_index_arguments_reject_bools_and_strings(bridge: Bridge, doc: str) -> None:
    """A bool or string where an index belongs is refused, not coerced.

    int(True) is 1, so a bare int() in the client turns a caller's flag into a
    destructive edit: remove_sketch_geometry(..., True) silently deleted
    geometry 1. _index exists for this and was applied to fillet, chamfer and
    attach_sketch_to_face while these two kept the bare coercion - which also
    made the bridge's own bool guard unreachable, since the value had already
    been converted before the wire.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "S", 10, 0, 10, 10)

    for bad in (True, False, "1", 1.0):
        with pytest.raises(BadGeometry):
            bridge.remove_sketch_geometry(doc, "S", bad)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_constraint(doc, "S", "Coincident", bad, 2, 2, 1, 0.0)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_constraint(doc, "S", "Coincident", 1, bad, 2, 1, 0.0)

    # Nothing was removed or constrained along the way.
    assert bridge.sketch_status(doc, "S")["geometry_count"] == 2
    assert bridge.sketch_status(doc, "S")["dof"] >= 0


def test_a_profile_with_holes_is_told_so(bridge: Bridge, doc: str) -> None:
    """Several wires means a plate with holes, not a malformed outline.

    The message used to say "a profile must be a single connected outline",
    which is false for this case: the region is connected, only the outline is
    not. A caller who had drawn the obvious thing was told they were wrong,
    with a reason that was not, and the fix - extrude the outline then cut the
    holes with boolean_op - was nowhere stated, though the suite demonstrates
    exactly that sequence elsewhere.
    """
    bridge.add_sketch(doc, "Plate")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 20),
        (30, 20, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Plate", x1, y1, x2, y2)
    bridge.add_sketch_circle(doc, "Plate", 15, 10, 4)

    status = bridge.sketch_status(doc, "Plate")
    assert status["closed"] is False
    assert status["wire_count"] == 2
    assert "holes" in status["closed_reason"]
    assert "boolean_op" in status["closed_reason"], (
        "the reason must name the way out, not just the problem"
    )

    with pytest.raises(BadGeometry) as caught:
        bridge.extrude_sketch(doc, "Plate", "P", 2.0)
    message = str(caught.value)
    assert "holes" in message
    assert "boolean_op" in message
    assert "malformed" not in message

    # And an open outline gives a *different* reason, so the two are
    # distinguishable rather than both being "closed is False".
    bridge.add_sketch(doc, "Gap")
    bridge.add_sketch_line(doc, "Gap", 0, 0, 10, 0)
    assert "does not close" in bridge.sketch_status(doc, "Gap")["closed_reason"]


def test_sketch_status_always_returns_the_same_keys(bridge: Bridge, doc: str) -> None:
    """Both return paths give the same shape, so a caller need not branch.

    `wire_count` used to be absent on an empty sketch - the early return ran
    before it was set - so reading it on a fresh sketch raised KeyError, and a
    fresh sketch is exactly what add_sketch returns.
    """
    bridge.add_sketch(doc, "Empty")
    empty = bridge.sketch_status(doc, "Empty")
    assert empty["wire_count"] == 0
    assert empty["closed_reason"]

    bridge.add_sketch(doc, "Full")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Full", x1, y1, x2, y2)
    full = bridge.sketch_status(doc, "Full")
    assert full["wire_count"] == 1
    assert set(empty) == set(full), (
        f"sketch_status returns different keys: {set(empty) ^ set(full)}"
    )


def test_constraint_positions_are_validated(bridge: Bridge, doc: str) -> None:
    """A position outside the safe PosId set is refused before the constructor.

    Probed against 1.1.3, and the two failure modes are different:

      pos 0 (FreeCAD's internal `none`) CRASHES the interpreter for a
        two-element constraint - the same fatality as the six-argument form
        AD-22 was written about.
      pos 4 and 99 are neither valid nor fatal: they are silently accepted
        and leave the solver reporting a negative dof, which is the
        silent-wrong-result class rather than the fatal one.

    Both are refused here, so neither reaches the native constructor. The
    assertion that FreeCAD still answers is the point: a guard that ran after
    the crash would raise BridgeUnreachable instead.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "S", 10, 0, 10, 10)

    for bad in (0, 4, 99, -1):
        with pytest.raises(BadGeometry):
            bridge.add_sketch_constraint(doc, "S", "Coincident", 1, bad, 2, 1, 0.0)
        with pytest.raises(BadGeometry):
            bridge.add_sketch_constraint(doc, "S", "Coincident", 1, 2, 2, bad, 0.0)

    assert bridge.ping() == "pong"


def test_valid_constraint_positions_are_accepted(bridge: Bridge, doc: str) -> None:
    """1 (start), 2 (end) and 3 (mid) all build.

    Without this the guard could be "refuse everything" and still pass.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "S", 10, 0, 10, 10)
    for position in (1, 2, 3):
        result = bridge.add_sketch_constraint(
            doc, "S", "Coincident", 1, position, 2, 1, 0.0
        )
        assert result["constraint_count"] >= 1


def test_a_distance_constraint_needs_a_finite_positive_value(
    bridge: Bridge, doc: str
) -> None:
    """Distance is the one kind whose value is used, so it is the one validated.

    A NaN would reach the native constructor, which is what AD-25 exists to
    prevent, and a zero or negative distance constrains two points to coincide
    or to separate impossibly.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)

    for bad in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(BadGeometry):
            bridge.add_sketch_constraint(doc, "S", "Distance", 1, 1, 1, 2, bad)

    result = bridge.add_sketch_constraint(doc, "S", "Distance", 1, 1, 1, 2, 25.0)
    assert result["constraint_count"] == 1
    assert bridge.ping() == "pong"


def test_extrude_sketch_takes_no_direction_argument(bridge: Bridge, doc: str) -> None:
    """The signature itself is the guarantee: no way to pass a direction.

    A test on behaviour cannot stop a future edit that adds a `direction`
    parameter and computes something wrong with it. This one can.
    """
    import inspect

    from freecad_ai import server as server_module

    signature = inspect.signature(server_module.extrude_sketch)
    assert list(signature.parameters) == [
        "document",
        "sketch_name",
        "result_name",
        "depth",
    ], (
        f"extrude_sketch now takes {list(signature.parameters)}; AD-23 fixes "
        f"direction to the sketch's own normal so that no argument can produce "
        f"a valid shape of zero volume"
    )


def test_extrude_sketch_refuses_a_zero_depth(bridge: Bridge, doc: str) -> None:
    """A zero depth is refused before anything is created, not after.

    The error is BadGeometry rather than EmptyResult because the depth is
    checked before the feature exists - which is the ordering that matters: a
    refusal that leaves a half-built feature behind is the orphan bug fixed in
    839a08e, and this path must not reintroduce it.
    """
    bridge.add_sketch(doc, "Zero")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Zero", x1, y1, x2, y2)
    with pytest.raises(BadGeometry):
        bridge.extrude_sketch(doc, "Zero", "Nothing", 0.0)
    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert "Nothing" not in names, f"a refused extrude left {sorted(names)}"


def test_a_successful_attachment_reports_a_placement_on_the_face(
    bridge: Bridge, doc: str
) -> None:
    """The reported placement must be the face's, not whatever it was before.

    This is the post-condition the tool checks. Asserting it end to end means
    the sketch genuinely lands in the face's plane: for a box's top face the
    origin is the plane origin at z=4, and a solid extruded from it spans
    z 4 to 6. If the attachment silently failed, the placement would still be
    z=0 and the extrusion would span 0 to 2.
    """
    bridge.add_primitive(
        doc, "Part::Box", "Box", {"Length": 40, "Width": 20, "Height": 4}
    )
    bridge.add_sketch(doc, "S")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "S", x1, y1, x2, y2)
    result = bridge.attach_sketch_to_face(doc, "S", "Box", 6)
    assert result["placement"] == [0.0, 0.0, 4.0]
    assert result["on_face_plane"] is True

    bridge.extrude_sketch(doc, "S", "Pad", 2.0)
    summary = bridge.shape_summary(doc, "Pad")
    assert summary["bbox"][2] == pytest.approx(4.0, abs=1e-6)
    assert summary["bbox"][5] == pytest.approx(6.0, abs=1e-6)


def test_face_name_is_normalised_before_use(bridge: Bridge, doc: str) -> None:
    """Face06 resolves to Face6, and it is Face6 that reaches FreeCAD.

    "Face06" passes the index check, so handing the caller's string to
    AttachmentSupport risks an unresolvable subelement - which would leave the
    sketch silently where it was rather than failing.
    """
    bridge.add_primitive(
        doc, "Part::Box", "Box", {"Length": 40, "Width": 20, "Height": 4}
    )
    bridge.add_sketch(doc, "S")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "S", x1, y1, x2, y2)
    result = bridge.attach_sketch_to_face(doc, "S", "Box", 6)
    assert result["face"] == "Face6"
    assert result["map_mode"] == "FlatFace"
    assert result["placement"] == [0.0, 0.0, 4.0]


def test_attaching_to_a_missing_face_is_refused(bridge: Bridge, doc: str) -> None:
    bridge.add_primitive(
        doc, "Part::Box", "Box", {"Length": 10, "Width": 10, "Height": 10}
    )
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 5, 0)
    with pytest.raises(BadGeometry):
        bridge.attach_sketch_to_face(doc, "S", "Box", 99)
    with pytest.raises(BadGeometry):
        bridge.attach_sketch_to_face(doc, "S", "Box", "face6")  # a string, not an int


def test_faces_and_edges_share_one_addressing_convention(
    bridge: Bridge, doc: str
) -> None:
    """One caller mistake must get one answer, whichever kind it is.

    Faces used to take a `Face{N}` string, so `Edge99`, `Face99` and `"face6"`
    were the same mistake answered three ways - BadGeometry, NoSuchFace and
    BadGeometry. Faces now take a 1-based integer like edges, and every
    out-of-range or wrongly-typed index on either raises BadGeometry.
    """
    bridge.add_primitive(
        doc, "Part::Box", "Box", {"Length": 40, "Width": 20, "Height": 4}
    )
    bridge.add_sketch(doc, "S")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "S", x1, y1, x2, y2)
    bridge.extrude_sketch(doc, "S", "Solid", 2.0)

    # An out-of-range edge and an out-of-range face, same error, same shape.
    with pytest.raises(BadGeometry):
        bridge.fillet(doc, "Solid", "R", [999], 1.0)
    with pytest.raises(BadGeometry):
        bridge.attach_sketch_to_face(doc, "S", "Box", 99)
    # A wrongly-typed index on either, likewise.
    with pytest.raises(BadGeometry):
        bridge.fillet(doc, "Solid", "R", ["Edge3"], 1.0)
    with pytest.raises(BadGeometry):
        bridge.attach_sketch_to_face(doc, "S", "Box", "Face3")


def test_sketch_becomes_a_face_with_real_area(bridge: Bridge, doc: str) -> None:
    """A wire encloses no area; a face does.

    The trap is that a sketch's *own* ``Shape.Area`` is 0.0 for this profile.
    ``sketch_status`` works around that by building a Part.Face, which is why
    it already reports 450. This asserts the face object proper, which is what
    the new tool creates.
    """
    bridge.add_sketch(doc, "Rect")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Rect", x1, y1, x2, y2)
    assert bridge.sketch_status(doc, "Rect")["area"] == pytest.approx(450.0, abs=1e-6)

    bridge.sketch_to_face(doc, "Rect", "Face")
    geometry = bridge.describe_geometry(doc, "Face")
    assert geometry["face_count"] == 1
    assert geometry["faces"][0]["area"] == pytest.approx(450.0, abs=1e-6)


def test_a_face_is_cuttable_by_the_existing_tools(bridge: Bridge, doc: str) -> None:
    """The point of creating a face: it feeds the rest of the surface.

    Asserts the *effect* of the cut, not the solid count. An earlier version
    asserted `solid_count == 0`, which is also what a cut that removed nothing
    returns — so it passed for a face with no hole, or for a wire, or for
    garbage with one planar face in it. Asserting the analytic area
    distinguishes all of those from a real hole.

    Measured against FreeCAD 1.1.3: 450.0 - pi*3^2 = 421.7257.
    """
    bridge.add_sketch(doc, "Rect")
    for x1, y1, x2, y2 in (
        (0, 0, 30, 0),
        (30, 0, 30, 15),
        (30, 15, 0, 15),
        (0, 15, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Rect", x1, y1, x2, y2)
    bridge.sketch_to_face(doc, "Rect", "Face")
    assert bridge.measure(doc, "Face")["area"] == pytest.approx(450.0, abs=1e-6)

    bridge.add_primitive(doc, "Part::Cylinder", "Hole", {"Radius": 3, "Height": 5})
    bridge.set_placement(doc, "Hole", 15, 7.5, -1)
    bridge.boolean_op(doc, "Face", "Hole", "cut", "Cut")

    cut = bridge.measure(doc, "Cut")
    expected = 450.0 - math.pi * 9
    assert cut["area"] == pytest.approx(expected, abs=1e-3)
    # Two wires: the outer boundary plus the hole's boundary. A single wire
    # would mean the cut left the outline intact.
    assert cut["wire_count"] == 2
    assert cut["face_count"] == 1
    # A face is not a solid, and must not become one by being cut.
    assert cut["solid_count"] == 0


def test_an_open_profile_makes_no_face(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Open")
    bridge.add_sketch_line(doc, "Open", 0, 0, 30, 0)
    with pytest.raises(ProfileNotClosed):
        bridge.sketch_to_face(doc, "Open", "Nope")


def test_a_failed_face_leaves_nothing_behind(bridge: Bridge, doc: str) -> None:
    """A refusal must not deposit a half-built object in the document.

    The object used to be created before the face was validated, so a failure
    left an orphan Part::Feature with a null Shape under the caller's own
    name: list_objects showed it, describe_geometry on it raised NO_SHAPE, and
    a retry collided with it. Asserting the exception was not enough - the
    damage was in the document, not the return value.

    Uses a self-intersecting bowtie, which is the path that actually reaches
    the area check: an *open* profile is refused earlier by _profile_wire,
    before any object exists, so it could never have orphaned one and proves
    nothing about this bug.
    """
    bridge.add_sketch(doc, "Bow")
    for x1, y1, x2, y2 in (
        (0, 0, 20, 20),
        (20, 20, 20, 0),
        (20, 0, 0, 20),
        (0, 20, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Bow", x1, y1, x2, y2)
    # It closes, which is why it gets as far as the face check at all.
    assert bridge.sketch_status(doc, "Bow")["closed"] is True

    with pytest.raises(EmptyResult):
        bridge.sketch_to_face(doc, "Bow", "Orphan")

    names = {obj["name"] for obj in bridge.list_objects(doc)}
    assert "Orphan" not in names, f"a failed sketch_to_face left {sorted(names)}"
    # And the name is still free, so a retry is not silently suffixed.
    bridge.add_sketch(doc, "Good")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Good", x1, y1, x2, y2)
    assert bridge.sketch_to_face(doc, "Good", "Orphan") == "Orphan"


# -- input validation ------------------------------------------------------


def test_sketch_tools_reject_a_non_sketch(bridge: Bridge, doc: str) -> None:
    """Every sketch tool must refuse a non-sketch with a named error.

    All five entry points resolve their input through _sketch, whose whole
    purpose is to avoid an opaque AttributeError from inside FreeCAD arriving
    as xmlrpc's generic code 1. Each is listed so that swapping _sketch for
    _object in any one of them fails here.
    """
    bridge.add_primitive(
        doc, "Part::Box", "B", {"Length": 10, "Width": 10, "Height": 10}
    )
    with pytest.raises(NotASketch):
        bridge.add_sketch_line(doc, "B", 0, 0, 1, 1)
    with pytest.raises(NotASketch):
        bridge.sketch_status(doc, "B")
    with pytest.raises(NotASketch):
        bridge.extrude_sketch(doc, "B", "R", 1.0)
    with pytest.raises(NotASketch):
        bridge.sketch_to_face(doc, "B", "F")
    with pytest.raises(NotASketch):
        bridge.attach_sketch_to_face(doc, "B", "B", 1)
    with pytest.raises(NotASketch):
        bridge.add_sketch_arc(doc, "B", 0, 0, 5, 0, 90)
    with pytest.raises(NotASketch):
        bridge.add_sketch_circle(doc, "B", 0, 0, 5)
    with pytest.raises(NotASketch):
        bridge.remove_sketch_geometry(doc, "B", 1)
    with pytest.raises(NotASketch):
        bridge.add_sketch_constraint(doc, "B", "Coincident", 1, 1, 1, 2, 0.0)


def test_degenerate_dimensions_are_refused(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Deg")
    with pytest.raises(BadGeometry):
        bridge.add_sketch_line(doc, "Deg", 0, 0, 0, 0)  # zero-length
    with pytest.raises(BadGeometry):
        bridge.add_sketch_circle(doc, "Deg", 0, 0, 0.0)  # zero radius
    with pytest.raises(BadGeometry):
        bridge.add_sketch_circle(doc, "Deg", 0, 0, -5.0)  # negative radius
    with pytest.raises(BadGeometry):
        bridge.add_sketch_arc(doc, "Deg", 0, 0, 5.0, 45, 45)  # zero sweep


def test_bool_is_refused_where_a_number_belongs(bridge: Bridge, doc: str) -> None:
    """A bool is an int in Python, so float(True) is 1.0.

    Without the check a caller who passed a flag gets a 1mm line and no error.
    """
    bridge.add_sketch(doc, "Bool")
    for call in (
        lambda: bridge.add_sketch_line(doc, "Bool", True, 0, 1, 1),
        lambda: bridge.add_sketch_circle(doc, "Bool", True, 0, 5.0),
        lambda: bridge.add_sketch_arc(doc, "Bool", 0, 0, True, 0, 90),
        lambda: bridge.extrude_sketch(doc, "Bool", "R", True),
    ):
        with pytest.raises(BadGeometry):
            call()


def test_add_sketch_is_idempotent(bridge: Bridge, doc: str) -> None:
    """Retrying a name must not error, matching new_document's behaviour."""
    bridge.add_sketch(doc, "Same")
    bridge.add_sketch_line(doc, "Same", 0, 0, 10, 0)
    assert bridge.add_sketch(doc, "Same") == "Same"
    # Replacing clears the old geometry rather than accumulating it.
    assert bridge.sketch_status(doc, "Same")["geometry_count"] == 0


def test_add_sketch_refuses_to_clobber_a_non_sketch(bridge: Bridge, doc: str) -> None:
    bridge.add_primitive(
        doc, "Part::Box", "Taken", {"Length": 5, "Width": 5, "Height": 5}
    )
    with pytest.raises(NotASketch):
        bridge.add_sketch(doc, "Taken")


def test_negative_depth_is_refused_with_a_direction(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "Neg")
    for x1, y1, x2, y2 in (
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (10, 10, 0, 10),
        (0, 10, 0, 0),
    ):
        bridge.add_sketch_line(doc, "Neg", x1, y1, x2, y2)
    with pytest.raises(BadGeometry) as caught:
        bridge.extrude_sketch(doc, "Neg", "Backwards", -2.0)
    assert "placement" in str(caught.value)
