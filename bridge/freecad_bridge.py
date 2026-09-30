"""XML-RPC bridge that runs *inside* FreeCAD.

This module is the only place allowed to ``import FreeCAD``. It is executed by
``freecadcmd`` under FreeCAD's own bundled Python 3.11 and is never imported by
the MCP server package, which runs on the project's own Python 3.14. The
separation is deliberate: FreeCAD links ``python311.dll`` and cannot be
imported from any other interpreter.

The server binds loopback only. It is unauthenticated, so exposing it on a
routable interface would hand anyone who can reach the port full control of
FreeCAD.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any
from xmlrpc.client import Fault
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

DEFAULT_PORT = 9875
DEFAULT_HOST = "127.0.0.1"

# XML-RPC has no null type: xmlrpc.client cannot marshal None. Dictionaries
# returned from here therefore omit keys whose value is None rather than
# sending None. Consumers must treat a missing key as "no value".

# Fault codes, so the client can raise a typed error instead of pattern
# matching on the message text. Codes are part of the bridge contract: the
# client maps each one to a specific exception class.
#
# All codes are >= 100 on purpose. xmlrpc.client serialises an *uncaught*
# server exception as faultCode 1, so 1 must not name a specific condition or
# an ordinary TypeError will be reported as one.
FAULT_INTERNAL = 1
FAULT_UNKNOWN_FUNCTION = 100
FAULT_DOCUMENT_NOT_FOUND = 101
FAULT_DOCUMENT_EXISTS = 102
FAULT_OBJECT_NOT_FOUND = 103
FAULT_PROPERTY_NOT_FOUND = 104
FAULT_NO_SHAPE = 105
FAULT_NO_SUCH_DIMENSION = 106
FAULT_EXPORT_FAILED = 107
FAULT_SAVE_FAILED = 108


def _fail(code: int, message: str) -> None:
    raise Fault(code, message)


class _QuietHandler(SimpleXMLRPCRequestHandler):
    """Suppress per-request logging to stderr."""

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _require(document: str) -> Any:
    import FreeCAD

    if document not in FreeCAD.listDocuments():
        _fail(FAULT_DOCUMENT_NOT_FOUND, f"no such document: {document}")
    return FreeCAD.getDocument(document)


def _object(doc: Any, object_name: str) -> Any:
    obj = doc.getObject(object_name)
    if obj is None:
        _fail(FAULT_OBJECT_NOT_FOUND, f"no such object: {object_name}")
    return obj


def ping() -> str:
    return "pong"


def version() -> dict[str, Any]:
    import FreeCAD

    return {"version": list(FreeCAD.Version()[0:3]), "gui_up": bool(FreeCAD.GuiUp)}


def list_documents() -> list[str]:
    import FreeCAD

    return list(FreeCAD.listDocuments())


def new_document(name: str, reuse: bool = True) -> dict[str, Any]:
    """Create a document, or return the existing one.

    Idempotent by default: agents retry document names constantly, and an
    error every time is noise. Pass ``reuse=False`` to insist on a new one.
    """
    import FreeCAD

    if name in FreeCAD.listDocuments():
        if not reuse:
            _fail(FAULT_DOCUMENT_EXISTS, f"document already exists: {name}")
        return {"name": name, "created": False}
    FreeCAD.newDocument(name)
    return {"name": name, "created": True}


def open_document(path: str) -> str:
    import FreeCAD

    doc = FreeCAD.openDocument(path)
    return doc.Name


def save_document(name: str, path: str) -> str:
    doc = _require(name)
    try:
        doc.saveAs(path)
    except Exception as error:
        _fail(FAULT_SAVE_FAILED, f"cannot save {name} to {path}: {error}")
    return path


def list_objects(name: str) -> list[dict[str, Any]]:
    """Describe every object in a document.

    Returns one dict per object. Properties whose value is None are omitted
    entirely, because XML-RPC cannot marshal None.
    """
    doc = _require(name)
    described: list[dict[str, Any]] = []
    for obj in doc.Objects:
        entry: dict[str, Any] = {
            "name": obj.Name,
            "label": obj.Label,
            "type": obj.TypeId,
        }
        described.append({k: v for k, v in entry.items() if v is not None})
    return described


def add_primitive(
    name: str,
    kind: str,
    object_name: str,
    dimensions: dict[str, float],
) -> str:
    """Add a Part primitive. ``kind`` is a FreeCAD TypeId, e.g. Part::Box."""
    doc = _require(name)
    obj = doc.addObject(kind, object_name)
    for prop, value in dimensions.items():
        if not hasattr(obj, prop):
            _fail(
                FAULT_NO_SUCH_DIMENSION,
                f"{kind} has no property {prop!r}; "
                f"available: {sorted(obj.PropertiesList)}",
            )
        setattr(obj, prop, float(value))
    doc.recompute()
    return obj.Name


# Export routes by extension. Part handles B-rep formats; Mesh handles
# tessellated ones. Nothing handles everything, so pick by suffix rather than
# guessing from the object.
_PART_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".brep"}
_MESH_SUFFIXES = {".stl", ".obj", ".off", ".ply"}


def export_object(name: str, object_name: str, path: str) -> str:
    """Write one object to disk, choosing the exporter by file extension.

    Returns the absolute path written.
    """
    doc = _require(name)
    obj = _object(doc, object_name)

    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in _PART_SUFFIXES:
        import Part

        Part.export([obj], str(target))
    elif suffix in _MESH_SUFFIXES:
        import Mesh

        Mesh.export([obj], str(target))
    else:
        _fail(
            FAULT_EXPORT_FAILED,
            f"unsupported export format {suffix!r}; "
            f"supported: {sorted(_PART_SUFFIXES | _MESH_SUFFIXES)}",
        )
    if not target.is_file():
        _fail(FAULT_EXPORT_FAILED, f"exporter wrote no file at {target}")
    return str(target.resolve())


def _scalar(value: Any) -> tuple[bool, Any]:
    """Reduce a FreeCAD property value to something XML-RPC can carry.

    Dimensional properties are ``Base.Quantity`` objects, not floats, so an
    isinstance check against (int, float, str, bool) silently drops every
    dimension. Quantities are unwrapped to their magnitude plus unit string.
    """
    if isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float, str)):
        return True, value
    unit = getattr(value, "Unit", None)
    magnitude = getattr(value, "Value", None)
    if unit is not None and isinstance(magnitude, (int, float)) and not isinstance(
        magnitude, bool
    ):
        # str(Quantity.Unit) is verbose ("Unit: mm (1,0,0,0,0,0,0,0) [Length]");
        # getUserPreferred() yields a clean ("10.00 mm", 1.0, "mm") triple.
        symbol = str(unit)
        get_preferred = getattr(value, "getUserPreferred", None)
        if callable(get_preferred):
            parts = get_preferred()
            if isinstance(parts, tuple) and len(parts) == 3:
                symbol = str(parts[2])
        return True, {"value": float(magnitude), "unit": symbol}
    return False, None


def get_properties(name: str, object_name: str) -> dict[str, Any]:
    """Return an object's scalar properties, omitting None values."""
    doc = _require(name)
    obj = _object(doc, object_name)
    values: dict[str, Any] = {}
    for prop in obj.PropertiesList:
        try:
            raw = getattr(obj, prop)
        except Exception:
            continue
        if raw is None:
            continue
        usable, reduced = _scalar(raw)
        if usable:
            values[prop] = reduced
    return values


def set_property(name: str, object_name: str, prop: str, value: Any) -> str:
    doc = _require(name)
    obj = _object(doc, object_name)
    if prop not in obj.PropertiesList:
        _fail(
            FAULT_PROPERTY_NOT_FOUND,
            f"no such property: {prop}; available: {sorted(obj.PropertiesList)}",
        )
    if isinstance(value, dict) and set(value) == {"value", "unit"}:
        # Round-trip form emitted by get_properties for dimensional values.
        import FreeCAD

        value = FreeCAD.Units.Quantity(float(value["value"]), str(value["unit"]))
    setattr(obj, prop, value)
    doc.recompute()
    return prop


def remove_object(name: str, object_name: str) -> str:
    doc = _require(name)
    _object(doc, object_name)
    doc.removeObject(object_name)
    doc.recompute()
    return object_name


def shape_summary(name: str, object_name: str) -> dict[str, Any]:
    """Volume, area and bounding box for an object that has a Shape."""
    doc = _require(name)
    obj = _object(doc, object_name)
    shape = getattr(obj, "Shape", None)
    if shape is None:
        _fail(FAULT_NO_SHAPE, f"{object_name} has no shape")
    box = shape.BoundBox
    return {
        "volume": shape.Volume,
        "area": shape.Area,
        "bbox": [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax],
    }


def _dispatch(name: str, *args: Any) -> Any:
    """Call the bridge function named ``name`` with the given arguments.

    The lookup result must be *called*; returning the function object itself
    marshals as an empty struct rather than raising, so the failure looks like
    a well-formed response carrying no data.
    """
    function = globals().get(name)
    if not callable(function):
        _fail(FAULT_UNKNOWN_FUNCTION, f"no such bridge function: {name}")
    return function(*args)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the bridge until the process is terminated."""
    import FreeCAD

    server = SimpleXMLRPCServer(
        (host, port),
        requestHandler=_QuietHandler,
        allow_none=False,
        logRequests=False,
    )
    server.register_introspection_functions()
    server.register_function(_dispatch, "dispatch")

    sys.stderr.write(
        f"freecad-bridge listening on {host}:{port} "
        f"(FreeCAD {'.'.join(FreeCAD.Version()[0:3])}, GuiUp={bool(FreeCAD.GuiUp)})\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _main() -> int:
    """Entry point used when launched as ``freecadcmd bridge/freecad_bridge.py``.

    freecadcmd passes the script path as ``sys.argv[1]``, so user arguments
    begin at index 2 — reading from index 1 silently treats the script's own
    path as the host and the host as the port.
    """
    args = sys.argv[2:]
    host = args[0] if len(args) > 0 else DEFAULT_HOST
    raw_port = args[1] if len(args) > 1 else str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        sys.stderr.write(f"error: port must be an integer, got {raw_port!r}\n")
        return 2
    try:
        serve(host, port)
    except OSError as error:
        sys.stderr.write(f"error: cannot bind {host}:{port}: {error}\n")
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def _running_under_freecadcmd() -> bool:
    """True when this module was loaded by freecadcmd.

    freecadcmd does not execute a script: it *imports* it, under the module
    name taken from the filename. ``__name__`` is therefore "freecad_bridge",
    never "__main__", and a conventional ``if __name__ == "__main__"`` guard
    silently does nothing — the process starts, defines everything, and exits
    without ever binding the port.
    """
    return Path(sys.argv[0]).stem.lower().startswith("freecad")


if __name__ == "__main__" or _running_under_freecadcmd():
    _main()
