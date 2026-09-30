"""MCP server exposing FreeCAD operations as tools.

Runs on the project's Python and talks to FreeCAD over the XML-RPC bridge. The
FreeCAD process is started lazily on first use, so importing this module — or
starting the MCP server — never requires FreeCAD to be running.
"""

from __future__ import annotations

import atexit
import functools
import inspect
import subprocess
import threading
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from mcp.server.mcpserver import MCPServer

from ._version import __version__
from .bridge import (
    Bridge,
    BridgeError,
    start_headless,
    stop,
)

P = ParamSpec("P")
R = TypeVar("R")

INSTRUCTIONS = """\
Drive FreeCAD 1.1 through a headless freecadcmd process reached over XML-RPC.

Call `connect` first, or any other tool will start FreeCAD on demand. Create a
document before adding geometry; `new_document` is idempotent, so repeating a
name is safe. Dimensions are in millimetres.

Object properties whose value is null are omitted from responses rather than
reported as null, because XML-RPC cannot represent None. Treat a missing key as
"no value". Dimensional properties arrive as {"value": 10.0, "unit": "mm"} and
can be passed straight back to `set_property`.

Tool failures come back as a result with `error`, `kind` and `hint` fields
rather than a crash. Read the hint before retrying — most failures are a
missing document, object or property, not a broken server.
"""

server = MCPServer(
    name="freecad-ai",
    version=__version__,
    instructions=INSTRUCTIONS,
)

_lock = threading.Lock()
_process: subprocess.Popen[bytes] | None = None
_bridge: Bridge | None = None
# True when the previous FreeCAD process died and had to be replaced. Every
# document went with it, and the model must be told rather than handed a
# silently empty FreeCAD.
_restarted = False


def get_bridge() -> Bridge:
    """Return a connected bridge, starting or replacing FreeCAD as needed."""
    global _process, _bridge, _restarted
    with _lock:
        if _bridge is not None:
            try:
                if _bridge.ping() == "pong":
                    return _bridge
            except BridgeError:
                _bridge = None
        else:
            _restarted = False

        if _process is not None:
            if _process.poll() is None:
                # Alive but not answering: give it up cleanly before
                # replacing it, or the port stays bound by a process we no
                # longer own.
                _process.terminate()
                try:
                    _process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _process.kill()
            _restarted = True

        _process, _bridge = start_headless()
        return _bridge


def consume_restart_notice() -> str | None:
    """Return a one-shot notice if FreeCAD was replaced, else ``None``.

    Read-and-clear, so a single restart is reported once rather than on every
    subsequent call.
    """
    global _restarted
    with _lock:
        if not _restarted:
            return None
        _restarted = False
    return (
        "FreeCAD was not responding and has been restarted. Every document "
        "and object from before is gone; re-create what you need."
    )


@atexit.register
def _shutdown() -> None:
    if _process is not None:
        stop(_process)


def _tool(name: str, description: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a function as an MCP tool, preserving its signature.

    MCPServer reads the wrapped function's signature to build the tool schema,
    so the decorator must not erase parameter types.

    Bridge failures are *returned*, not raised. MCPServer 2.2 handles
    ``except MCPError: raise`` before its ``except Exception`` branch, and
    ToolError subclasses MCPError — so raising one skips the is_error result
    path entirely and the client gets a dropped request instead of a readable
    failure. Returning a payload guarantees the model sees what went wrong and
    what to do about it.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            try:
                result = func(*args, **kwargs)
            except BridgeError as error:
                return {
                    "error": str(error),
                    "kind": type(error).__name__,
                    "hint": getattr(error, "hint", ""),
                }
            # MCPServer's result conversion treats a list as a sequence of
            # content blocks and chains them, so a tool returning a list
            # silently reports only its first element. Wrapping here means no
            # tool can reintroduce that by passing a bridge value through.
            if isinstance(result, list):
                return {"items": result}
            notice = consume_restart_notice()
            if notice is None:
                return result
            if isinstance(result, dict):
                return {**result, "notice": notice}
            return {"result": result, "notice": notice}

        functools.update_wrapper(wrapper, func)
        # Publish the original signature, but with a widened return type.
        # MCPServer builds the input schema from the parameters and an output
        # schema from the return, then validates every result against it — so
        # a tool annotated `-> list[...]` would reject the dict error payload
        # below as a type mismatch and surface as an opaque UnexpectedToolError.
        # `__signature__` must be set explicitly because inspect.signature
        # follows the `__wrapped__` that update_wrapper just installed.
        wrapper.__signature__ = inspect.signature(func).replace(  # type: ignore[attr-defined]
            return_annotation=Any
        )
        server.tool(name=name, description=description)(wrapper)
        return cast("Callable[P, R]", wrapper)

    return decorator


@_tool("connect", "Start FreeCAD if needed and report its version.")
def connect() -> dict[str, Any]:
    return get_bridge().version()


@_tool(
    "new_document",
    "Create a document, or return the existing one. Set reuse=False to insist "
    "on a new document and fail if the name is taken.",
)
def new_document(name: str, reuse: bool = True) -> dict[str, Any]:
    return get_bridge().new_document(name, reuse)


@_tool("open_document", "Open an existing .FCStd file by absolute path.")
def open_document(path: str) -> str:
    return get_bridge().open_document(path)


@_tool("save_document", "Save a document to an absolute path.")
def save_document(name: str, path: str) -> str:
    return get_bridge().save_document(name, path)


@_tool("list_documents", "List the names of open documents.")
def list_documents() -> dict[str, Any]:
    # Wrapped in a dict on purpose. MCPServer's result conversion treats a bare
    # list as a sequence of content blocks and chains them together, so a tool
    # returning a list of values silently reports only its first element. A dict
    # is JSON-encoded whole.
    return {"documents": get_bridge().list_documents()}


@_tool("list_objects", "List objects in a document with name, label and type.")
def list_objects(document: str) -> dict[str, Any]:
    return {"objects": get_bridge().list_objects(document)}


@_tool(
    "list_primitive_types",
    "List the Part types this FreeCAD can create, with the properties each "
    "one takes. Call this before add_primitive: TypeIds and dimension names "
    "differ per shape and cannot be guessed.",
)
def list_primitive_types() -> dict[str, Any]:
    return {"types": get_bridge().list_primitive_types()}


@_tool(
    "add_primitive",
    "Add a Part primitive. kind is a FreeCAD TypeId such as Part::Box; call "
    "list_primitive_types for the available types and their properties.",
)
def add_primitive(
    document: str,
    kind: str,
    object_name: str,
    dimensions: dict[str, float],
) -> str:
    return get_bridge().add_primitive(document, kind, object_name, dimensions)


@_tool("get_properties", "Get an object's scalar properties.")
def get_properties(document: str, object_name: str) -> dict[str, Any]:
    return get_bridge().get_properties(document, object_name)


@_tool("set_property", "Set one property on an object and recompute.")
def set_property(document: str, object_name: str, prop: str, value: Any) -> str:
    return get_bridge().set_property(document, object_name, prop, value)


@_tool("remove_object", "Remove an object from a document.")
def remove_object(document: str, object_name: str) -> str:
    return get_bridge().remove_object(document, object_name)


@_tool(
    "set_placement",
    "Move and rotate an object. Placement is a FreeCAD object that cannot "
    "cross the bridge directly, so send its components.",
)
def set_placement(
    document: str,
    object_name: str,
    x: float,
    y: float,
    z: float,
    axis_x: float = 0.0,
    axis_y: float = 0.0,
    axis_z: float = 1.0,
    angle: float = 0.0,
) -> dict[str, Any]:
    return get_bridge().set_placement(
        document, object_name, x, y, z, axis_x, axis_y, axis_z, angle
    )


@_tool(
    "boolean_op",
    "Combine two objects into a new one. operation is cut, fuse or common. "
    "Both inputs are kept; the result is a new parametric feature.",
)
def boolean_op(
    document: str,
    base_name: str,
    tool_name: str,
    operation: str,
    result_name: str,
) -> str:
    return get_bridge().boolean_op(
        document, base_name, tool_name, operation, result_name
    )


@_tool(
    "export_object",
    "Write an object to disk. Format is chosen by extension: .step/.stp/.iges/"
    ".igs/.brep go through Part, .stl/.obj/.off/.ply through Mesh.",
)
def export_object(document: str, object_name: str, path: str) -> str:
    return get_bridge().export_object(document, object_name, path)


@_tool("shape_summary", "Volume, area and bounding box for an object.")
def shape_summary(document: str, object_name: str) -> dict[str, Any]:
    return get_bridge().shape_summary(document, object_name)


def main() -> None:
    """Console-script entry point: serve MCP over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised over the wire
    main()
