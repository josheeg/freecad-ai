"""Tests for MCP tool registration and error surfacing.

These exercise the server layer without starting FreeCAD.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from freecad_ai import server as server_module
from freecad_ai.bridge import BridgeError, DocumentNotFound


@pytest.fixture
def registry() -> Any:
    return server_module.server._tool_manager


def test_restart_notice_is_reported_once(monkeypatch: Any) -> None:
    """A replaced FreeCAD must be announced, then not repeated.

    Otherwise the model is handed an empty FreeCAD with no indication its
    documents are gone, and it carries on building a silently wrong model.
    """
    monkeypatch.setattr(server_module, "_restarted", False)
    assert server_module.consume_restart_notice() is None

    monkeypatch.setattr(server_module, "_restarted", True)
    notice = server_module.consume_restart_notice()
    assert notice is not None
    assert "restarted" in notice
    assert "gone" in notice
    # Read-and-clear: one restart is reported once, not on every later call.
    assert server_module.consume_restart_notice() is None


def test_restart_notice_reaches_the_tool_result(monkeypatch: Any) -> None:
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.list_documents.return_value = ["a"]
    monkeypatch.setattr(server_module, "get_bridge", lambda: fake)
    monkeypatch.setattr(server_module, "_restarted", True)

    tool = server_module.server._tool_manager.get_tool("list_documents")
    result = tool.fn()
    assert "notice" in result
    assert result["documents"] == ["a"]


def test_every_tool_is_registered(registry: Any) -> None:
    names = {tool.name for tool in registry._tools.values()}
    assert names == {
        "connect",
        "new_document",
        "open_document",
        "save_document",
        "list_documents",
        "list_objects",
        "add_primitive",
        "get_properties",
        "set_property",
        "remove_object",
        "shape_summary",
        "export_object",
        "boolean_op",
        "set_placement",
        "list_primitive_types",
        "describe_geometry",
        "measure",
        "distance",
        "is_inside",
        "cross_section",
        "fillet",
        "chamfer",
        "mirror",
        "linear_array",
        # Sketches: 2D profile to solid. See spec-freecad-ai-sketches.
        "add_sketch",
        "add_sketch_line",
        "add_sketch_arc",
        "add_sketch_circle",
        "remove_sketch_geometry",
        "add_sketch_constraint",
        "sketch_status",
        "extrude_sketch",
        "attach_sketch_to_face",
        "sketch_to_face",
    }


def test_bridge_error_becomes_a_result_not_an_exception(registry: Any) -> None:
    """A failing tool must return an error payload, never raise.

    MCPServer wraps any escaping exception as UnexpectedToolError, which
    reaches the model as an opaque crash with no recovery guidance.
    """
    result = registry.get_tool("list_objects").fn(document="ghost")
    assert result["kind"] == "DocumentNotFound"
    assert result["hint"]
    assert "ghost" in result["error"]


def test_error_payload_has_all_three_fields(registry: Any) -> None:
    result = registry.get_tool("list_objects").fn(document="ghost")
    assert set(result) == {"error", "kind", "hint"}


def test_tool_return_type_is_widened(registry: Any) -> None:
    """Return annotations must not be narrowed, or output validation rejects
    the error payload.

    MCPServer derives an output schema from the return annotation and
    validates every result against it. `list_objects` is annotated
    `-> list[dict[str, Any]]`; leaving that in place makes the dict error
    payload fail validation and resurface as UnexpectedToolError.
    """
    for tool in registry._tools.values():
        annotation = inspect.signature(tool.fn).return_annotation
        assert annotation is Any, f"{tool.name} still constrains its return type"


def test_tool_parameters_keep_their_annotations(registry: Any) -> None:
    """Widening the return must not lose the input schema."""
    signature = inspect.signature(registry.get_tool("add_primitive").fn)
    assert list(signature.parameters) == [
        "document",
        "kind",
        "object_name",
        "dimensions",
    ]


def test_no_tool_returns_a_bare_list(registry: Any, monkeypatch: Any) -> None:
    """A list return value is silently truncated to its first element.

    MCPServer's `_convert_to_content` treats a list as a sequence of content
    blocks and chains them, so a tool reporting three objects would reach the
    model as three separate text blocks — and `content[0]`, which is what
    clients read, would hold only the first. Wrap list results in a dict.
    """
    from unittest.mock import MagicMock

    fake = MagicMock()
    for name in (
        "version",
        "list_primitive_types",
        "list_documents",
        "list_objects",
        "get_properties",
        "shape_summary",
        "describe_geometry",
        "measure",
        "is_inside",
    ):
        setattr(fake, name, MagicMock(return_value=["a", "b", "c"]))
    monkeypatch.setattr(server_module, "get_bridge", lambda: fake)

    samples: dict[str, Any] = {"float": 1.0, "bool": True, "str": "x"}
    offenders: list[str] = []
    for tool in registry._tools.values():
        kwargs = {
            parameter.name: samples.get(str(parameter.annotation), "x")
            for parameter in inspect.signature(tool.fn).parameters.values()
        }
        if isinstance(tool.fn(**kwargs), list):
            offenders.append(tool.name)
    assert offenders == [], f"these return a bare list: {offenders}"


def test_list_results_are_wrapped_in_a_dict(registry: Any) -> None:
    """Multi-element results must reach the model intact, not as N blocks."""
    payload = registry.get_tool("list_objects").fn(document="ghost")
    assert isinstance(payload, dict), "list results must be wrapped in a dict"


def test_non_bridge_exceptions_are_not_swallowed() -> None:
    """Only BridgeError is converted; a real bug must still surface."""

    def explode() -> None:
        raise ValueError("bug")

    wrapped = server_module._tool("probe", "probe")(explode)
    with pytest.raises(ValueError, match="bug"):
        wrapped()


def test_bridge_error_base_carries_no_false_hint() -> None:
    assert BridgeError("x").hint == ""


def test_document_not_found_is_a_bridge_error() -> None:
    assert issubclass(DocumentNotFound, BridgeError)
