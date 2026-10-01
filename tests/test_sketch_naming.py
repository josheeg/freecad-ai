"""Named geometry, named constraints, and driving a dimension.

Separate from the rest of the sketch tests because these are about *references*
rather than geometry: whether a name still refers to the right thing after an
edit renumbers it, and whether a dimension can be moved after it is set.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from freecad_ai.bridge import (
    BadGeometry,
    Bridge,
    NoSuchConstraint,
    start_headless,
    stop,
)

PORT = 9883
HOST = "127.0.0.1"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bridge() -> Iterator[Bridge]:
    process, client = start_headless(HOST, PORT, timeout=90.0)
    try:
        yield client
    finally:
        stop(process)


@pytest.fixture
def doc(bridge: Bridge) -> str:
    name = f"nm{uuid.uuid4().hex[:8]}"
    bridge.new_document(name)
    return name


@pytest.fixture
def named_rectangle(bridge: Bridge, doc: str) -> tuple[str, dict[str, str]]:
    """Four named lines forming a 40x20 rectangle, unconstrained.

    Returns the sketch name and the name given to each line, so a test can
    check that a name still points where it did rather than at a fixed index.
    """
    sketch = "S"
    bridge.add_sketch(doc, sketch)
    corners = {
        "base": (0, 0, 40, 0),
        "wall": (40, 0, 40, 20),
        "top": (40, 20, 0, 20),
        "left": (0, 20, 0, 0),
    }
    for name, points in corners.items():
        bridge.add_sketch_line(doc, sketch, *points, label=name)
    return sketch, corners


def _by_name(status: dict) -> dict[str, int]:
    return {entry["name"]: entry["geometry"] for entry in status["geometry"]}


# --------------------------------------------------------------- naming


def test_sketch_status_reports_the_name_mapping(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    sketch, _ = named_rectangle
    status = bridge.sketch_status(doc, sketch)
    assert _by_name(status) == {"base": 1, "wall": 2, "top": 3, "left": 4}
    assert status["geometry_count"] == 4
    assert status["constraint_count"] == 0


def test_a_name_survives_the_removal_that_renumbers_it(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    """The whole point of a name.

    Removing `wall` moves `top` from index 3 to index 2. If the stored mapping
    were not reindexed, resolving the name `top` afterwards would land on
    whatever now occupies index 3 - or, once the count drops, on some unrelated
    piece of geometry.

    The assertion is on *which element the constraint ended up on*, not merely
    that the call succeeded: `first_name` is read back from FreeCAD's own
    constraint, so it can disagree with the name that was asked for. That is the
    silent-wrong-result case, and only this assertion catches it.
    """
    sketch, _ = named_rectangle
    assert _by_name(bridge.sketch_status(doc, sketch))["top"] == 3

    bridge.remove_sketch_geometry(doc, sketch, "wall")
    status = bridge.sketch_status(doc, sketch)
    assert _by_name(status) == {"base": 1, "top": 2, "left": 3}

    bridge.add_sketch_constraint(doc, sketch, "Horizontal", "top", 1, "top", 2, 0.0)
    constraint = bridge.sketch_status(doc, sketch)["constraints"][0]
    assert constraint["first_name"] == "top", (
        "the name resolved to a different element than it names, which is the "
        "silent wrong result this feature exists to prevent"
    )
    assert constraint["first"] == 2
    assert bridge.ping() == "pong"


def test_removing_by_name_reports_which_one_went(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    sketch, _ = named_rectangle
    result = bridge.remove_sketch_geometry(doc, sketch, "left")
    assert result["removed_name"] == "left"
    assert result["geometry_count"] == 3


def test_a_duplicate_name_is_refused_before_anything_is_created(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    """Refused before creation, so no unnamed geometry is orphaned (AD-26)."""
    sketch, _ = named_rectangle
    with pytest.raises(BadGeometry) as caught:
        bridge.add_sketch_line(doc, sketch, 5, 5, 9, 9, label="base")
    assert "base" in str(caught.value)
    # The refusal must have left the sketch exactly as it was.
    assert bridge.sketch_status(doc, sketch)["geometry_count"] == 4


@pytest.mark.parametrize("label", ["   ", "\t"])
def test_a_whitespace_name_is_refused(bridge: Bridge, doc: str, label: str) -> None:
    """Whitespace looks like a name that was meant to be something, so it is
    refused. An empty string is not - see the next test."""
    bridge.add_sketch(doc, "S")
    with pytest.raises(BadGeometry):
        bridge.add_sketch_line(doc, "S", 0, 0, 10, 0, label=label)
    assert bridge.sketch_status(doc, "S")["geometry_count"] == 0


def test_an_omitted_name_means_none_at_all(bridge: Bridge, doc: str) -> None:
    """Pins the wire decision: "" is how "no name" travels.

    XML-RPC cannot marshal None here, so an empty string carries the absence
    instead of being an invalid name. Without this test the two readings of ""
    are indistinguishable, and a later change to reject "" would silently break
    every caller that omits the name.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0, label="")
    status = bridge.sketch_status(doc, "S")
    assert status["geometry_count"] == 1
    assert status["geometry"] == []


def test_a_padded_name_is_refused(bridge: Bridge, doc: str) -> None:
    """A name with whitespace could not be passed back as a reference."""
    bridge.add_sketch(doc, "S")
    with pytest.raises(BadGeometry) as caught:
        bridge.add_sketch_circle(doc, "S", 0, 0, 5, label=" hole ")
    assert "whitespace" in str(caught.value)


def test_an_unknown_name_lists_the_ones_that_exist(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    sketch, _ = named_rectangle
    with pytest.raises(BadGeometry) as caught:
        bridge.remove_sketch_geometry(doc, sketch, "diagonal")
    message = str(caught.value)
    assert "diagonal" in message
    assert "base" in message and "wall" in message


def test_a_name_may_be_removed_by_index_too(bridge: Bridge, doc: str) -> None:
    """Indices still work, so nothing that worked before stopped working."""
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0, label="a")
    bridge.add_sketch_line(doc, "S", 10, 0, 10, 10, label="b")
    result = bridge.remove_sketch_geometry(doc, "S", 1)
    assert result == {"geometry_count": 1, "removed_name": "a"}
    assert _by_name(bridge.sketch_status(doc, "S")) == {"b": 1}


# ------------------------------------------------------- named constraints


def test_constraints_can_be_named_and_removed_by_name(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    sketch, _ = named_rectangle
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "base", 1, "base", 2, 40.0, label="width"
    )
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "wall", 1, "wall", 2, 20.0, label="height"
    )
    status = bridge.sketch_status(doc, sketch)
    assert status["constraint_count"] == 2
    assert [c["name"] for c in status["constraints"]] == ["width", "height"]

    result = bridge.remove_sketch_constraint(doc, sketch, "height")
    assert result["removed_name"] == "height"
    assert result["constraint_count"] == 1
    assert [c["name"] for c in bridge.sketch_status(doc, sketch)["constraints"]] == [
        "width"
    ]


def test_a_constraint_name_follows_the_constraint_not_the_index(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    """Deleting an earlier constraint must not leave a name on the wrong one.

    FreeCAD attaches the name to the constraint, so this holds for free there -
    the test is here because it is the property that makes names worth having,
    and because a future implementation might store names by index and break it
    without anything failing loudly.
    """
    sketch, _ = named_rectangle
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "base", 1, "base", 2, 40.0, label="width"
    )
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "wall", 1, "wall", 2, 20.0, label="height"
    )
    bridge.remove_sketch_constraint(doc, sketch, 1)

    remaining = bridge.sketch_status(doc, sketch)["constraints"]
    assert len(remaining) == 1
    assert remaining[0]["name"] == "height"
    assert remaining[0]["value"] == 20.0


def test_a_duplicate_constraint_name_is_refused(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    sketch, _ = named_rectangle
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "base", 1, "base", 2, 40.0, label="width"
    )
    with pytest.raises(BadGeometry):
        bridge.add_sketch_constraint(
            doc, sketch, "Distance", "wall", 1, "wall", 2, 20.0, label="width"
        )
    # Refused before the constraint was added, so the count is unchanged.
    assert bridge.sketch_status(doc, sketch)["constraint_count"] == 1


def test_an_unknown_constraint_name_is_refused(bridge: Bridge, doc: str) -> None:
    """`NoSuchConstraint` finally has a caller, rather than being defined only."""
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    with pytest.raises(NoSuchConstraint) as caught:
        bridge.remove_sketch_constraint(doc, "S", "depth")
    assert "depth" in str(caught.value)
    assert NoSuchConstraint.hint, "a fault class must say what to do next"


@pytest.mark.parametrize("index", [0, 2, 99, "nope"])
def test_a_bad_constraint_reference_is_refused(
    bridge: Bridge, doc: str, index: object
) -> None:
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    with pytest.raises(NoSuchConstraint):
        bridge.remove_sketch_constraint(doc, "S", index)  # type: ignore[arg-type]
    assert bridge.sketch_status(doc, "S")["constraint_count"] == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda b, d: b.remove_sketch_constraint(d, "S", True),
        lambda b, d: b.remove_sketch_geometry(d, "S", True),
        lambda b, d: b.set_constraint_value(d, "S", True, 5.0),
    ],
)
def test_a_boolean_is_never_an_index(bridge: Bridge, doc: str, call) -> None:
    """A bool is an int in Python, so `True` would quietly become index 1.

    Refused on the client, before the wire, for every reference-taking call -
    which is why the fault is BadGeometry rather than NoSuchConstraint
    regardless of what was being addressed. One rule for all three.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    with pytest.raises(BadGeometry) as caught:
        call(bridge, doc)
    assert "1-based index" in str(caught.value)


# ------------------------------------------------------- driving a value


def test_a_named_dimension_can_be_re_driven(bridge: Bridge, doc: str) -> None:
    """The payoff of naming a constraint: change it without rebuilding.

    A Distance added by index could only be removed and re-added to change.
    This is the difference between a parametric sketch and a finished one, and
    it is what CAP-S4 means by dimensions being driven by named constraints.
    """
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 40, 0, label="base")
    bridge.add_sketch_constraint(doc, "S", "Horizontal", "base", 1, "base", 2, 0.0)
    bridge.add_sketch_constraint(
        doc, "S", "Distance", "base", 1, "base", 2, 40.0, label="length"
    )

    for wanted in (75.0, 12.5, 40.0):
        result = bridge.set_constraint_value(doc, "S", "length", wanted)
        # Read back from FreeCAD rather than echoed, so a value the solver
        # clamped or refused would be visible here.
        assert result["value"] == wanted
        assert result["constraint_name"] == "length"

    # Two constraints: the Horizontal and the named Distance.
    assert bridge.sketch_status(doc, "S")["constraint_count"] == 2
    assert bridge.ping() == "pong"


def test_a_dimension_can_be_re_driven_by_index_too(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 40, 0)
    bridge.add_sketch_constraint(doc, "S", "Distance", 1, 1, 1, 2, 40.0)
    assert bridge.set_constraint_value(doc, "S", 1, 22.0)["value"] == 22.0


def test_a_non_dimensional_constraint_refuses_a_value(bridge: Bridge, doc: str) -> None:
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    bridge.add_sketch_line(doc, "S", 10, 0, 10, 10)
    bridge.add_sketch_constraint(doc, "S", "Coincident", 1, 2, 2, 1, 0.0)

    with pytest.raises(BadGeometry) as caught:
        bridge.set_constraint_value(doc, "S", 1, 5.0)
    message = str(caught.value)
    assert "Coincident" in message
    assert "Distance" in message, "the refusal should name the kind that works"
    assert bridge.sketch_status(doc, "S")["constraint_count"] == 1


@pytest.mark.parametrize("value", [0.0, -5.0, float("nan"), float("inf")])
def test_a_re_driven_value_must_be_finite_and_positive(
    bridge: Bridge, doc: str, value: float
) -> None:
    bridge.add_sketch(doc, "S")
    bridge.add_sketch_line(doc, "S", 0, 0, 10, 0)
    bridge.add_sketch_constraint(doc, "S", "Distance", 1, 1, 1, 2, 10.0, label="len")
    with pytest.raises(BadGeometry):
        bridge.set_constraint_value(doc, "S", "len", value)
    assert bridge.sketch_status(doc, "S")["constraints"][0]["value"] == 10.0


def test_sketch_status_lists_constraints_with_the_elements_they_use(
    bridge: Bridge, doc: str, named_rectangle
) -> None:
    """A caller must be able to see what a constraint actually landed on."""
    sketch, _ = named_rectangle
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "base", 1, "base", 2, 40.0, label="width"
    )
    entry = bridge.sketch_status(doc, sketch)["constraints"][0]
    assert entry["constraint"] == 1
    assert entry["kind"] == "Distance"
    assert entry["first"] == 1
    assert entry["first_name"] == "base"
    assert entry["value"] == 40.0


def test_names_survive_a_save_and_reopen(bridge: Bridge, doc: str, tmp_path) -> None:
    """A name is stored on the sketch, so it travels with the document.

    If the mapping lived only in the server it would work in a session and
    quietly stop working after a save - which is exactly the shape of bug that
    never shows up in a test that never reopens a file.
    """
    sketch = "S"
    bridge.add_sketch(doc, sketch)
    bridge.add_sketch_line(doc, sketch, 0, 0, 10, 0, label="base")
    bridge.add_sketch_line(doc, sketch, 10, 0, 10, 10, label="wall")
    bridge.add_sketch_constraint(
        doc, sketch, "Distance", "base", 1, "base", 2, 10.0, label="width"
    )

    path = tmp_path / "named.FCStd"
    bridge.save_document(doc, str(path))
    assert path.is_file()

    # A fresh process is the only way to prove the names reached disk rather
    # than living in this one's memory.
    process, other = start_headless(HOST, PORT + 1, timeout=90.0)
    try:
        reopened = other.open_document(str(path))
        status = other.sketch_status(reopened, sketch)
        assert _by_name(status) == {"base": 1, "wall": 2}
        assert status["constraints"][0]["name"] == "width"
        # And a name still works as a reference on the reopened document.
        removed = other.remove_sketch_geometry(reopened, sketch, "base")
        assert removed["removed_name"] == "base"
        assert _by_name(other.sketch_status(reopened, sketch)) == {"wall": 1}
    finally:
        stop(process)
