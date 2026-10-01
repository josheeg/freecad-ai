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
FAULT_BAD_OPERATION = 109
FAULT_BAD_GEOMETRY = 110
FAULT_NO_SUCH_FEATURE = 111
FAULT_EMPTY_RESULT = 112
# Sketch-specific. Kept in the same allocated range so the client's registry
# test covers them with no special case.
FAULT_PROFILE_NOT_CLOSED = 113
FAULT_NOT_A_SKETCH = 114
FAULT_NO_SUCH_CONSTRAINT = 115
FAULT_NO_SUCH_FACE = 116

# FreeCAD 1.1 has no generic Part::Boolean; each operation is its own
# parametric feature type with Base and Tool links. Verified against 1.1.3.
_BOOLEAN_TYPES = {
    "cut": "Part::Cut",
    "fuse": "Part::Fuse",
    "common": "Part::Common",
}

# Probed by list_primitive_types, not a hardcoded contract: this FreeCAD build
# may not offer all of them (1.1.3 has no Part::Tube, for instance).
_CANDIDATE_TYPES = (
    "Part::Box",
    "Part::Cylinder",
    "Part::Sphere",
    "Part::Cone",
    "Part::Torus",
    "Part::Prism",
    "Part::Wedge",
    "Part::Helix",
    "Part::Tube",
    "Part::Circle",
    "Part::Ellipse",
    "Part::Polygon",
    "Part::Plane",
    "Part::Line",
    "Part::Vertex",
)


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
        exporter_name, module_name = "Part", "Part"
    elif suffix in _MESH_SUFFIXES:
        exporter_name, module_name = "Mesh", "Mesh"
    else:
        _fail(
            FAULT_EXPORT_FAILED,
            f"unsupported export format {suffix!r}; "
            f"supported: {sorted(_PART_SUFFIXES | _MESH_SUFFIXES)}",
        )
        return ""  # unreachable; keeps mypy aware _fail never returns

    # FreeCAD raises a bare RuntimeError here, which would surface as an
    # internal bug rather than the user's actual mistake.
    if not target.parent.is_dir():
        _fail(
            FAULT_EXPORT_FAILED,
            f"directory does not exist: {target.parent}",
        )
    module = __import__(module_name)
    try:
        module.export([obj], str(target))
    except Exception as error:
        _fail(FAULT_EXPORT_FAILED, f"{exporter_name} could not write {target}: {error}")
    if not target.is_file():
        _fail(FAULT_EXPORT_FAILED, f"{exporter_name} wrote no file at {target}")
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
    if (
        unit is not None
        and isinstance(magnitude, (int, float))
        and not isinstance(magnitude, bool)
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


def set_placement(
    name: str,
    object_name: str,
    x: float,
    y: float,
    z: float,
    axis_x: float = 0.0,
    axis_y: float = 0.0,
    axis_z: float = 1.0,
    angle: float = 0.0,
) -> dict[str, Any]:
    """Move and rotate an object.

    Placement is a FreeCAD object, not a primitive, so it cannot cross XML-RPC
    as a value — passing a dict raises "type must be 'Matrix' or 'Placement'".
    The components travel instead and are reassembled here.
    """
    import FreeCAD

    doc = _require(name)
    obj = _object(doc, object_name)
    if "Placement" not in obj.PropertiesList:
        _fail(FAULT_PROPERTY_NOT_FOUND, f"{object_name} has no Placement")
    try:
        placement = FreeCAD.Placement(
            FreeCAD.Vector(float(x), float(y), float(z)),
            FreeCAD.Vector(float(axis_x), float(axis_y), float(axis_z)),
            float(angle),
        )
    except Exception as error:
        _fail(FAULT_BAD_OPERATION, f"invalid placement: {error}")
        return {}  # unreachable
    obj.Placement = placement
    doc.recompute()
    base = obj.Placement.Base
    return {"x": base.x, "y": base.y, "z": base.z, "angle": float(angle)}


def boolean_op(
    name: str,
    base_name: str,
    tool_name: str,
    operation: str,
    result_name: str,
) -> str:
    """Combine two objects into a new parametric feature.

    ``operation`` is cut, fuse or common. The inputs are left in place; the
    result is a new object, so an agent can keep building on either side.
    """
    doc = _require(name)
    if operation not in _BOOLEAN_TYPES:
        _fail(
            FAULT_BAD_OPERATION,
            f"unsupported boolean operation {operation!r}; "
            f"supported: {sorted(_BOOLEAN_TYPES)}",
        )
    if base_name == tool_name:
        _fail(FAULT_BAD_OPERATION, "base and tool must be different objects")
    base = _object(doc, base_name)
    tool = _object(doc, tool_name)
    if getattr(base, "Shape", None) is None or getattr(tool, "Shape", None) is None:
        _fail(FAULT_NO_SHAPE, "boolean operations need two objects with shapes")

    result = doc.addObject(_BOOLEAN_TYPES[operation], result_name)
    result.Base = base
    result.Tool = tool
    doc.recompute()
    # A boolean that does not intersect does not produce a null shape. Cutting
    # two boxes that never touch yields a valid Compound holding the first,
    # and intersecting them yields a valid Compound holding nothing at all.
    # Reporting that as success hands back a result with no material in it.
    if result.Shape.isNull():
        _fail(
            FAULT_BAD_OPERATION,
            f"{operation} of {base_name} and {tool_name} produced an empty shape",
        )
    if operation == "common" and not result.Shape.Solids:
        _fail(
            FAULT_EMPTY_RESULT,
            f"{base_name} and {tool_name} do not intersect, so their common "
            f"part is empty; move them so they overlap",
        )
    return result.Name


def _parametric_properties(obj: Any) -> list[str]:
    """Named shape-affecting properties, so an agent knows what to pass.

    Filtered from PropertiesList: a fresh primitive carries a lot of engine and
    attachment properties that are not dimensions.
    """
    return [
        prop
        for prop in obj.PropertiesList
        if not prop.startswith("_")
        and prop
        not in {
            "Label",
            "Label2",
            "ExpressionEngine",
            "Visibility",
            "AttacherEngine",
            "AttacherType",
            "AttachmentOffset",
            "AttachmentSupport",
            "MapMode",
            "MapReversed",
            "MapPathParameter",
            "Refine",
            "History",
        }
    ]


def instance_id() -> dict[str, Any]:
    """Identify this FreeCAD process.

    The client must not assume the bridge it reached is the one it launched.
    If another FreeCAD already holds the port, a launch here fails to bind
    and the client would otherwise adopt that other process — and with it
    someone else's documents. The pid lets the client detect that.
    """
    import os

    return {"pid": os.getpid()}


def list_primitive_types() -> list[dict[str, Any]]:
    """Enumerate creatable Part types and the properties each one takes.

    An agent cannot guess a FreeCAD TypeId, and the dimension names differ per
    primitive (Box has Length/Width/Height, Cylinder has Radius/Height/Angle).
    This is probed live rather than hardcoded, so it tracks whatever the
    installed FreeCAD actually offers.
    """
    import FreeCAD

    catalog: list[dict[str, Any]] = []
    for kind in _CANDIDATE_TYPES:
        probe = FreeCAD.newDocument("_probe", hidden=True)
        try:
            obj = probe.addObject(kind, "probe")
            catalog.append({"type": kind, "properties": _parametric_properties(obj)})
        except Exception:
            continue
        finally:
            FreeCAD.closeDocument(probe.Name)
    return catalog


def _shape_of(document: str, object_name: str) -> Any:
    doc = _require(document)
    obj = _object(doc, object_name)
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        _fail(FAULT_NO_SHAPE, f"{object_name} has no shape")
    return shape


def _feature(name: str, kind: str, document: str) -> Any:
    doc = _require(document)
    try:
        return doc.addObject(kind, name)
    except Exception as error:
        _fail(FAULT_NO_SUCH_FEATURE, f"cannot create {kind}: {error}")
        return None  # unreachable


def describe_geometry(document: str, object_name: str) -> dict[str, Any]:
    """List an object's edges and faces so they can be referred to by name.

    Fillet and chamfer take edge numbers, which are useless unless something
    can say which edge is which. Edges are reported as FreeCAD names
    (Edge1, Edge2, ...) because that is exactly what those features expect.
    """
    shape = _shape_of(document, object_name)
    edges = []
    for index, edge in enumerate(shape.Edges, start=1):
        start = edge.Vertexes[0].Point if edge.Vertexes else None
        end = edge.Vertexes[-1].Point if edge.Vertexes else None
        entry: dict[str, Any] = {
            "name": f"Edge{index}",
            "index": index,
            "type": type(edge.Curve).__name__,
            "length": edge.Length,
        }
        if start is not None:
            entry["start"] = [round(start.x, 4), round(start.y, 4), round(start.z, 4)]
            entry["end"] = [round(end.x, 4), round(end.y, 4), round(end.z, 4)]
        edges.append(entry)

    faces = []
    for index, face in enumerate(shape.Faces, start=1):
        centre = face.CenterOfMass
        faces.append(
            {
                "name": f"Face{index}",
                "index": index,
                "type": type(face.Surface).__name__,
                "area": face.Area,
                "center": [
                    round(centre.x, 4),
                    round(centre.y, 4),
                    round(centre.z, 4),
                ],
            }
        )
    return {
        "object": object_name,
        "edge_count": len(edges),
        "face_count": len(faces),
        "edges": edges,
        "faces": faces,
    }


def _edge_pairs(
    edges: Any, size: float, document: str, object_name: str
) -> list[tuple[int, float, float]]:
    """Normalise edge input to the (index, start, end) tuples FreeCAD wants."""
    shape = _shape_of(document, object_name)
    if not isinstance(edges, list) or not edges:
        _fail(FAULT_BAD_GEOMETRY, "edges must be a non-empty list of edge numbers")
        return []
    pairs: list[tuple[int, float, float]] = []
    for item in edges:
        if isinstance(item, bool) or not isinstance(item, int):
            _fail(
                FAULT_BAD_GEOMETRY,
                f"edge must be an integer index, got {item!r}; "
                f"use describe_geometry to list them",
            )
            return []
        if not 1 <= item <= len(shape.Edges):
            _fail(
                FAULT_BAD_GEOMETRY,
                f"{object_name} has {len(shape.Edges)} edges, so Edge{item} "
                f"does not exist",
            )
            return []
        pairs.append((item, float(size), float(size)))
    return pairs


def fillet(
    document: str,
    object_name: str,
    result_name: str,
    edges: list[int],
    radius: float,
) -> str:
    """Round edges. Part::Fillet takes Base as the object and Edges as
    (index, start_radius, end_radius) tuples, 1-based."""
    if radius <= 0:
        _fail(FAULT_BAD_GEOMETRY, f"radius must be positive, got {radius}")
        return ""
    doc = _require(document)
    _object(doc, object_name)
    feature = _feature(result_name, "Part::Fillet", document)
    feature.Base = _object(doc, object_name)
    feature.Edges = _edge_pairs(edges, radius, document, object_name)
    doc.recompute()
    if feature.Shape.isNull():
        _fail(
            FAULT_BAD_GEOMETRY,
            f"a radius of {radius} does not fit on {edges}; try a smaller value",
        )
    return feature.Name


def chamfer(
    document: str,
    object_name: str,
    result_name: str,
    edges: list[int],
    size: float,
) -> str:
    """Cut edges flat. Same shape as fillet; Part::Chamfer reuses the same
    (index, start, end) tuple where both values are the chamfer size."""
    if size <= 0:
        _fail(FAULT_BAD_GEOMETRY, f"size must be positive, got {size}")
        return ""
    doc = _require(document)
    _object(doc, object_name)
    feature = _feature(result_name, "Part::Chamfer", document)
    feature.Base = _object(doc, object_name)
    feature.Edges = _edge_pairs(edges, size, document, object_name)
    doc.recompute()
    if feature.Shape.isNull():
        _fail(
            FAULT_BAD_GEOMETRY,
            f"a chamfer of {size} does not fit on {edges}; try a smaller value",
        )
    return feature.Name


def mirror(
    document: str,
    object_name: str,
    result_name: str,
    origin: list[float],
    normal: list[float],
) -> str:
    """Reflect an object through a plane given by a point and a normal."""
    import FreeCAD

    doc = _require(document)
    _object(doc, object_name)
    if len(origin) != 3 or len(normal) != 3:
        _fail(FAULT_BAD_GEOMETRY, "origin and normal must each have 3 values")
        return ""
    n = FreeCAD.Vector(*(float(v) for v in normal))
    if n.Length == 0:
        _fail(FAULT_BAD_GEOMETRY, "the mirror normal must not be a zero vector")
        return ""
    feature = _feature(result_name, "Part::Mirroring", document)
    feature.Source = _object(doc, object_name)
    feature.Base = FreeCAD.Vector(*(float(v) for v in origin))
    feature.Normal = n
    doc.recompute()
    return feature.Name


def linear_array(
    document: str,
    object_name: str,
    result_name: str,
    offset: list[float],
    count: int,
) -> str:
    """Repeat an object along a straight line.

    Built as a Part::MultiFuse of translated copies. FreeCAD's own array
    types (Part::Array, Draft::Array) are not registered in a headless
    document, and fusing copies needs no workbench.
    """
    doc = _require(document)
    source = _object(doc, object_name)
    if len(offset) != 3:
        _fail(FAULT_BAD_GEOMETRY, "offset must have 3 values")
        return ""
    if count < 1:
        _fail(FAULT_BAD_GEOMETRY, f"count must be at least 1, got {count}")
        return ""
    if count == 1:
        _fail(
            FAULT_BAD_GEOMETRY,
            "count of 1 would just copy the object; use copy if that is what you want",
        )
        return ""

    import FreeCAD

    copies = [source]
    step = FreeCAD.Vector(*(float(v) for v in offset))
    for index in range(1, count):
        duplicate = doc.addObject("Part::Feature", f"{result_name}_c{index}")
        duplicate.Shape = source.Shape.copy()
        duplicate.Placement.Base = source.Placement.Base + step * index
        copies.append(duplicate)
    doc.recompute()

    feature = _feature(result_name, "Part::MultiFuse", document)
    feature.Shapes = copies
    doc.recompute()
    if feature.Shape.isNull():
        _fail(FAULT_BAD_GEOMETRY, f"the copies of {object_name} do not form a solid")
    return feature.Name


def _vec(value: Any) -> list[float]:
    return [round(value.x, 6), round(value.y, 6), round(value.z, 6)]


def _center_of_mass(shape: Any) -> list[float]:
    """Centroid of a shape, tolerating the types that do not provide one.

    Compounds — which is what a fuse or a cut returns — have no
    ``CenterOfMass``. Measuring the result of an operation is the main reason
    this function exists, so it must not raise on the objects callers care
    about most.
    """
    try:
        return _vec(shape.CenterOfMass)
    except AttributeError:
        pass
    try:
        solids = shape.Solids
        if solids:
            return _vec(solids[0].CenterOfMass)
    except (AttributeError, IndexError):
        pass
    return []


def measure(document: str, object_name: str) -> dict[str, Any]:
    """Report a shape's mass properties and topology.

    The extra fields matter when checking work: `is_valid` catches a shape the
    kernel could not build, and `solid_count` catches a result that is
    technically fine but is not the single body that was asked for.
    """
    shape = _shape_of(document, object_name)
    box = shape.BoundBox
    return {
        "object": object_name,
        "shape_type": shape.ShapeType,
        "is_valid": bool(shape.isValid()),
        "is_closed": bool(shape.isClosed()),
        "volume": shape.Volume,
        "area": shape.Area,
        # CenterOfMass exists on solids and faces, not on compounds — and a
        # fused or cut result is a Compound, which is exactly what a caller
        # most wants to measure. Fall back to the first solid, and report
        # nothing rather than failing when there is none.
        "center_of_mass": _center_of_mass(shape),
        "bounding_box": [
            box.XMin,
            box.YMin,
            box.ZMin,
            box.XMax,
            box.YMax,
            box.ZMax,
        ],
        "solid_count": len(shape.Solids),
        "shell_count": len(shape.Shells),
        "face_count": len(shape.Faces),
        "wire_count": len(shape.Wires),
        "edge_count": len(shape.Edges),
        "vertex_count": len(shape.Vertexes),
    }


def distance(
    document: str,
    first: str,
    second: Any = "",
    point: Any = None,
) -> dict[str, Any]:
    """Closest distance between two objects, or between a point and an object.

    The caller sends an empty value for the argument it is not using, because
    XML-RPC cannot marshal None and this bridge runs with allow_none=False.

    Overlapping solids legitimately have distance 0; that is a position
    question, not an error.
    """
    shape = _shape_of(document, first)
    use_object = bool(second)
    use_point = bool(point)
    if use_object == use_point:
        _fail(
            FAULT_BAD_GEOMETRY,
            "give exactly one of second (another object) or point (3 values)",
        )
        return {}

    if use_object:
        other = _shape_of(document, str(second))
        gap, points, _ = shape.distToShape(other)
        result: dict[str, Any] = {
            "between": [first, str(second)],
            "distance": gap,
        }
        if points:
            result["point_on_first"] = _vec(points[0][0])
            result["point_on_second"] = _vec(points[0][1])
        return result

    if len(point) != 3:
        _fail(FAULT_BAD_GEOMETRY, "point must have 3 values")
        return {}
    import Part

    vertex = Part.Vertex(*(float(v) for v in point))
    gap, points, _ = vertex.distToShape(shape)
    result = {"between": [first, [float(v) for v in point]], "distance": gap}
    if points:
        result["point_on_object"] = _vec(points[0][1])
    return result


def is_inside(document: str, object_name: str, point: list[float]) -> dict[str, Any]:
    """Test whether a point lies inside a solid."""
    import FreeCAD

    shape = _shape_of(document, object_name)
    if len(point) != 3:
        _fail(FAULT_BAD_GEOMETRY, "point must have 3 values")
        return {}
    location = FreeCAD.Vector(*(float(v) for v in point))
    return {
        "object": object_name,
        "point": [float(v) for v in point],
        "inside": bool(shape.isInside(location, 1e-7, True)),
    }


def cross_section(
    document: str,
    object_name: str,
    normal: list[float],
    offset: float,
) -> dict[str, Any]:
    """Slice a shape with a plane and report the resulting section.

    The plane sits ``offset`` along ``normal`` from the origin, so for a
    horizontal cut through a part on the Z axis use normal [0,0,1] and the
    height you want.
    """
    import FreeCAD
    import Part

    shape = _shape_of(document, object_name)
    if len(normal) != 3:
        _fail(FAULT_BAD_GEOMETRY, "normal must have 3 values")
        return {}
    direction = FreeCAD.Vector(*(float(v) for v in normal))
    if direction.Length == 0:
        _fail(FAULT_BAD_GEOMETRY, "the section normal must not be a zero vector")
        return {}

    wires = shape.slice(direction, float(offset))
    if not wires:
        _fail(
            FAULT_EMPTY_RESULT,
            f"the plane does not cut {object_name}; it lies entirely to one "
            f"side of offset {offset}",
        )
        return {}
    total = 0.0
    for wire in wires:
        try:
            total += Part.Face(wire).Area
        except Exception:
            continue
    return {
        "object": object_name,
        "normal": [float(v) for v in normal],
        "offset": float(offset),
        "wire_count": len(wires),
        "area": total,
    }


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


# ---------------------------------------------------------------------------
# Sketches
#
# Probed against FreeCAD 1.1.3 before this was written; the findings that
# shaped it are in spec-freecad-ai-sketches/sketch-traps.md. Three matter:
#
#   A sketch's Shape.Area is 0.0 even for a closed profile, because a wire is
#   not a face. Area below comes from Part.Face(wire) instead.
#
#   An unclosed profile does not fail loudly. Part::Extrusion with Solid=True
#   over an open wire returned a shape rather than an error, with edges that
#   did not match the geometry drawn. So closedness is checked explicitly here
#   rather than left to the extruder.
#
#   Every Draft::* type raises TypeError in this build, so nothing here may
#   reach for one. The same conclusion AD-21 reached for arrays.
# ---------------------------------------------------------------------------

_SKETCH_TYPE = "Sketcher::SketchObject"


def _sketch(document: str, object_name: str) -> Any:
    """Resolve an object and require it to be a sketch.

    Checked by type rather than assumed, because most of these functions would
    otherwise raise an opaque AttributeError from inside FreeCAD, which
    xmlrpc reports as the generic internal fault.
    """
    obj = _object(_require(document), object_name)
    if obj.TypeId != _SKETCH_TYPE:
        _fail(
            FAULT_NOT_A_SKETCH,
            f"{object_name} is a {obj.TypeId}, not a {_SKETCH_TYPE}",
        )
    return obj


def _number(value: Any, what: str) -> float:
    """Coerce a caller-supplied dimension, refusing bools, non-numbers and NaN.

    A bool is an int in Python, so `add_sketch_circle(..., radius=True)` would
    otherwise silently become a 1mm circle.

    Non-finite values are refused because they are not merely wrong, they are
    fatal: a NaN passes every comparison-based guard in this file, because
    `nan == 0`, `nan < 0` and `nan <= 0` are all False. Verified against
    FreeCAD 1.1.3: `Part::Extrusion` with `LengthFwd = nan` *terminates the
    interpreter* rather than raising, taking every open document with it. That
    is AD-25, and this function is one of the two places it has to be enforced.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(FAULT_BAD_GEOMETRY, f"{what} must be a number, got {value!r}")
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{what} must be a finite number, got {value!r}. FreeCAD does not "
            f"reject NaN or infinity - it terminates - so this is refused here.",
        )
        return 0.0
    return float(value)


def _profile_wire(sketch: Any) -> Any:
    """The sketch's single closed wire, or a fault naming what is wrong.

    Probed: an unclosed profile produced a shape on extrude rather than an
    error, so this is the check that stands between a caller and a plausible
    wrong solid.
    """
    shape = sketch.Shape
    if shape.isNull():
        _fail(
            FAULT_PROFILE_NOT_CLOSED,
            f"{sketch.Name} has no geometry yet; add some with "
            f"add_sketch_line, add_sketch_arc or add_sketch_circle",
        )
    wires = shape.Wires
    if not wires:
        _fail(FAULT_PROFILE_NOT_CLOSED, f"{sketch.Name} produced no wire")
    if len(wires) > 1:
        # Say what it actually is. A profile with an inner boundary - the
        # obvious way to draw a plate with holes - arrives here as several
        # wires, and telling the caller their outline is malformed is both
        # wrong and unhelpful: the region is connected, only the outline is
        # not. The way out is already in the tool surface, so name it.
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{sketch.Name} has {len(wires)} separate wires, so it has an "
            f"interior boundary - a profile with holes. This surface extrudes "
            f"a single outer outline only. Cut the holes afterwards: "
            f"extrude_sketch the outline, add each hole as a primitive, then "
            f"boolean_op to cut them.",
        )
    wire = wires[0]
    if not wire.isClosed():
        # Name the gap: the caller needs to know which edges fail to meet.
        open_ends = []
        for vertex in wire.OrderedVertexes:
            open_ends.append([round(vertex.Point.x, 4), round(vertex.Point.y, 4)])
        _fail(
            FAULT_PROFILE_NOT_CLOSED,
            f"{sketch.Name} is not a closed profile: its wire has "
            f"{len(wire.OrderedEdges)} edges and {len(open_ends)} vertices "
            f"that do not meet end to end. Endpoints, in order: "
            f"{open_ends[:8]}. A sketch can only be extruded once its outline "
            f"closes.",
        )
    return wire


def add_sketch(document: str, sketch_name: str) -> str:
    """Create an empty sketch, replacing any existing one of that name.

    Idempotent like new_document: agents retry names constantly, and an error
    every time is noise rather than information.
    """
    doc = _require(document)
    existing = doc.getObject(sketch_name)
    if existing is not None:
        if existing.TypeId == _SKETCH_TYPE:
            doc.removeObject(existing.Name)
            doc.recompute()
        else:
            _fail(
                FAULT_NOT_A_SKETCH,
                f"{sketch_name} already exists and is a {existing.TypeId}",
            )
    try:
        sketch = doc.addObject(_SKETCH_TYPE, sketch_name)
    except Exception as error:
        _fail(FAULT_NO_SUCH_FEATURE, f"cannot create a sketch: {error}")
        return ""  # unreachable
    doc.recompute()
    return sketch.Name


def add_sketch_line(
    document: str,
    sketch_name: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    name: Any = None,
) -> dict[str, Any]:
    """Add a line segment between two points in the sketch plane.

    ``name`` is optional and gives the geometry a stable reference that
    survives an edit elsewhere in the sketch, which an index does not.
    """
    import FreeCAD
    import Part

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    # Validated before creation: after it there is no rollback, and a rejected
    # name would leave an unnamed piece of geometry the caller cannot refer to.
    _check_name(sketch, name, "line")
    start = FreeCAD.Vector(_number(x1, "x1"), _number(y1, "y1"), 0.0)
    end = FreeCAD.Vector(_number(x2, "x2"), _number(y2, "y2"), 0.0)
    if start.distanceToPoint(end) < 1e-9:
        _fail(FAULT_BAD_GEOMETRY, "a line needs two distinct points")
    sketch.addGeometry(Part.LineSegment(start, end), False)
    doc.recompute()
    index = sketch.GeometryCount
    _add_name(sketch, index, name)
    return {"geometry_count": index, "index": index}


def add_sketch_arc(
    document: str,
    sketch_name: str,
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    end_angle: float,
    name: Any = None,
) -> dict[str, Any]:
    """Add an arc of a circle, in degrees, counter-clockwise from +X.

    ``name`` optionally gives the arc a reference that survives an edit.

    Angles are degrees to match the placement convention AD-19 fixed. A
    mis-spanned arc is the usual way a profile fails to close, which
    sketch_status reports rather than leaving the caller to discover it at
    extrude time.
    """
    import math

    import FreeCAD
    import Part

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    _check_name(sketch, name, "arc")
    r = _number(radius, "radius")
    if r <= 0:
        _fail(FAULT_BAD_GEOMETRY, f"radius must be positive, got {r}")
    centre = FreeCAD.Vector(_number(cx, "cx"), _number(cy, "cy"), 0.0)
    start_rad = math.radians(_number(start_angle, "start_angle"))
    end_rad = math.radians(_number(end_angle, "end_angle"))
    if abs(start_rad - end_rad) < 1e-9:
        _fail(
            FAULT_BAD_GEOMETRY,
            "start_angle and end_angle must differ; equal angles give a "
            "zero-length arc",
        )
    circle = Part.Circle(centre, FreeCAD.Vector(0, 0, 1), r)
    sketch.addGeometry(Part.ArcOfCircle(circle, start_rad, end_rad), False)
    doc.recompute()
    index = sketch.GeometryCount
    _add_name(sketch, index, name)
    return {"geometry_count": index, "index": index}


def add_sketch_circle(
    document: str,
    sketch_name: str,
    cx: float,
    cy: float,
    radius: float,
    name: Any = None,
) -> dict[str, Any]:
    """Add a full circle, in the sketch plane.

    ``name`` optionally gives the circle a reference that survives an edit.
    """
    import FreeCAD
    import Part

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    _check_name(sketch, name, "circle")
    r = _number(radius, "radius")
    if r <= 0:
        _fail(FAULT_BAD_GEOMETRY, f"radius must be positive, got {r}")
    centre = FreeCAD.Vector(_number(cx, "cx"), _number(cy, "cy"), 0.0)
    sketch.addGeometry(Part.Circle(centre, FreeCAD.Vector(0, 0, 1), r), False)
    doc.recompute()
    index = sketch.GeometryCount
    _add_name(sketch, index, name)
    return {"geometry_count": index, "index": index}


# Caller-chosen names for sketch geometry, stored on the sketch itself.
#
# A name exists because an index does not survive an edit. Removing geometry
# renumbers everything after it, so an index a caller is holding silently comes
# to mean a different piece of geometry - the silent-wrong-result class reached
# through an API shape rather than a FreeCAD quirk. Probed against 1.1.3:
# deleting geometry 0 of two leaves count 1, and the surviving element moves
# from index 1 to index 0.
#
# The mapping lives on the sketch as a dynamic property rather than in the
# server, so a saved FCStd carries the names with it. It is a string because
# XML-RPC cannot marshal a dict into a FreeCAD property, and it must be
# reindexed by us on every removal - FreeCAD renumbers the geometry but knows
# nothing about the names.
_NAME_PROPERTY = "fc_geometry_names"
_NAME_GROUP = "FreeCAD-AI"


def _names(sketch: Any) -> dict[int, str]:
    """The sketch's name mapping, geometry-index to name."""
    import json

    raw = getattr(sketch, _NAME_PROPERTY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # A property the user edited by hand. Losing the names is recoverable;
        # refusing to work is not.
        return {}
    out: dict[int, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            try:
                out[int(key)] = str(value)
            except (TypeError, ValueError):
                continue
    return out


def _write_names(sketch: Any, mapping: dict[int, str]) -> None:
    import json

    if _NAME_PROPERTY not in sketch.PropertiesList:
        sketch.addProperty(
            "App::PropertyString",
            _NAME_PROPERTY,
            _NAME_GROUP,
            "Names given to sketch geometry by freecad-ai, as {index: name}",
        )
    # Plain attribute assignment, which is the one form verified to work on a
    # dynamically added property. There is no `_set_property` on a
    # DocumentObject, and setPropertyStatus alone does not mark the document
    # dirty, so the name would be lost on a save the caller never asked for.
    setattr(sketch, _NAME_PROPERTY, json.dumps(mapping, sort_keys=True))
    sketch.touch()


def _add_name(sketch: Any, index: int, name: Any) -> None:
    """Record a name for geometry that has already been added.

    Called after the geometry is in place, because the index is only known
    then. That ordering means a rejected name would leave the geometry behind -
    so every rule about the name is checked beforehand by ``_check_name``, and
    there is deliberately no validation here to fail.
    """
    if not name:
        return
    mapping = _names(sketch)
    mapping[index] = name
    _write_names(sketch, mapping)


def _check_name(sketch: Any, name: Any, what: str, existing: Any = None) -> None:
    """Validate a caller-supplied name before anything is created.

    Everything checkable about a name is checked here, before creation, so
    that recording it afterwards cannot fail and orphan the object AD-26 is
    about. ``_add_name`` then does no validation at all.

    ``existing`` supplies the names already in use for a kind of object that is
    not geometry - a constraint, whose names FreeCAD keeps and this surface does
    not. Geometry names come from the sketch instead.
    """
    # An empty string is how the wire says "no name" - XML-RPC cannot marshal
    # None here - so it means absent rather than invalid. Whitespace-only is
    # still refused, because unlike "" it looks like a name that was meant to
    # be something.
    if not name:
        return
    if not isinstance(name, str) or not name.strip():
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{what} name must be a non-empty string, got {name!r}",
        )
    if name != name.strip():
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{what} name {name!r} has leading or trailing whitespace, which "
            f"would make it impossible to pass back as a reference",
        )
    if existing is not None:
        if name in existing:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"name {name!r} is already used by another {what} in this "
                f"sketch; names must be unique within a sketch",
            )
        return
    for index, taken in _names(sketch).items():
        if taken == name:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"name {name!r} is already used by geometry {index}; names must "
                f"be unique within a sketch",
            )


def _resolve_geometry(sketch: Any, reference: Any, what: str) -> int:
    """Turn a name or a 1-based index into a 1-based geometry index.

    A name is looked up; an integer is taken as given. Names are tried first
    because a name is unambiguous, while an index shifts under the caller.
    """
    if isinstance(reference, str):
        for index, name in _names(sketch).items():
            if name == reference:
                return index
        known = sorted(_names(sketch).values())
        _fail(
            FAULT_BAD_GEOMETRY,
            f"no geometry named {reference!r} in {sketch.Name}"
            + (f"; it has {', '.join(known)}" if known else "; none are named"),
        )
    if isinstance(reference, bool) or not isinstance(reference, int):
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{what} must be a geometry name or a 1-based index, got {reference!r}",
        )
    return reference


def remove_sketch_geometry(
    document: str,
    sketch_name: str,
    reference: Any,
) -> dict[str, Any]:
    """Remove one geometry, addressed by name or by its 1-based index.

    FreeCAD renumbers geometry after a removal and says nothing about names, so
    the mapping is reindexed here. Skipping that step leaves a name attached to
    the wrong element - the exact silent-wrong-result this naming exists to
    prevent, reintroduced by the naming itself.
    """
    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    index = _resolve_geometry(sketch, reference, "index")
    count = sketch.GeometryCount
    if not 1 <= index <= count:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{sketch_name} has {count} geometries, so {index} does not exist",
        )

    # Everything after the removed element shifts down by one. Names are stored
    # with the sketch, so this must be rewritten before returning.
    removed_name = None
    mapping: dict[int, str] = {}
    for stored_index, name in _names(sketch).items():
        if stored_index == index:
            removed_name = name
        elif stored_index > index:
            mapping[stored_index - 1] = name
        else:
            mapping[stored_index] = name

    sketch.delGeometry(index - 1)
    doc.recompute()
    # Written after recompute so the stored mapping is never out of step with
    # the geometry it describes, even if recompute reports a problem.
    if _names(sketch) or mapping:
        _write_names(sketch, mapping)
    result: dict[str, Any] = {"geometry_count": sketch.GeometryCount}
    if removed_name is not None:
        result["removed_name"] = removed_name
    return result


def remove_sketch_constraint(
    document: str,
    sketch_name: str,
    reference: Any,
) -> dict[str, Any]:
    """Remove one constraint, addressed by name or by its 1-based index.

    This exists because a sketch is otherwise add-only. A caller who adds a
    constraint and gets the wrong result has no way to undo it short of
    discarding the sketch and its geometry - and geometry is the expensive part.
    CAP-S4 claims dimensions are driven by named constraints, which is not true
    of a surface where a constraint cannot be removed.

    Constraints are renumbered by FreeCAD after a removal exactly as geometry
    are, so the same one-convention rule applies: 1-based, and a name preferred
    because FreeCAD attaches it to the constraint rather than the index.
    """
    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    index = _constraint_ref(sketch, reference)
    count = sketch.ConstraintCount
    if not 1 <= index <= count:
        _fail(
            FAULT_NO_SUCH_CONSTRAINT,
            f"{sketch_name} has {count} constraints, so {index} does not "
            f"exist. Constraints are 1-based and renumbered after a removal; "
            f"read the current list from sketch_status.",
        )
    removed_name = getattr(sketch.Constraints[index - 1], "Name", "")
    sketch.delConstraint(index - 1)
    doc.recompute()
    result: dict[str, Any] = {
        "constraint_count": sketch.ConstraintCount,
        "dof": _solve(sketch),
    }
    if removed_name:
        result["removed_name"] = removed_name
    return result


def add_sketch_constraint(
    document: str,
    sketch_name: str,
    kind: str,
    first: Any,
    first_pos: int,
    second: Any,
    second_pos: int,
    value: float,
    name: Any = None,
) -> dict[str, Any]:
    """Add a constraint between two geometry elements.

    ``kind`` is a FreeCAD constraint name: Coincident, Horizontal, Vertical,
    Parallel, Perpendicular, Equal, or Distance. Distance is the only one that
    uses ``value``; the others take two elements and ignore it.

    ``first`` and ``second`` are **1-based**, matching every other index in this
    surface - edges, faces, sketch_status and remove_sketch_geometry. They used
    to be 0-based because FreeCAD numbers geometry internally from zero, and
    that inconsistency was the last one; AD-19's principle is one convention
    rather than two that can disagree. The translation to FreeCAD's numbering
    happens here and nowhere else.

    Either may also be a geometry **name**, which is what naming is for: a name
    still refers to the right element after an unrelated removal renumbers it.

    ``name`` gives the constraint itself a name, so a dimension can be
    re-driven later with ``set_constraint_value`` and removed by name with
    ``remove_sketch_constraint``.
    """
    import FreeCAD  # noqa: F401  (imported so a missing module fails as a fault)
    import Sketcher

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    count = sketch.GeometryCount
    # 1-based here, 0-based for FreeCAD. Named so the conversion is one obvious
    # line rather than arithmetic sprinkled through the function. Each element
    # may be given as a geometry name instead of an index, which is the point
    # of naming: a name survives an edit that would renumber an index.
    resolved = []
    for label, given in (("first", first), ("second", second)):
        index = _resolve_geometry(sketch, given, label)
        if not 1 <= index <= count:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"{label} geometry {index} does not exist; {sketch_name} has "
                f"{count}. Indices are 1-based, as they are for edges and faces.",
            )
        resolved.append(index)
    first_index = resolved[0] - 1
    second_index = resolved[1] - 1
    # PosId, as FreeCAD names them, restricted to the values that are safe here.
    # Probed against 1.1.3: 1 (start), 2 (end) and 3 (mid) build cleanly. `none`
    # (0) is a real FreeCAD PosId but SKETCHER uses it internally for
    # whole-element constraints, and passing it through CRASHES the interpreter
    # for a two-element constraint - so it is excluded rather than allowed.
    # Values above 3 are neither valid nor fatal: they are silently accepted and
    # leave the solver reporting a negative dof, which is the silent-wrong-result
    # class. All four cases are refused here, before the native constructor.
    _POS_NAMES = {1: "start", 2: "end", 3: "mid"}
    _POS_IDS = frozenset(_POS_NAMES)

    # An unknown kind is rejected before any Constraint is built. Building one
    # with the wrong arity does not raise - it terminates FreeCAD, which would
    # take the bridge down and orphan the document with it.
    _CONSTRAINT_KINDS = {
        "Coincident",
        "Horizontal",
        "Vertical",
        "Parallel",
        "Perpendicular",
        "Equal",
        "Distance",
    }
    if kind not in _CONSTRAINT_KINDS:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"unknown constraint kind {kind!r}; use one of "
            f"{', '.join(sorted(_CONSTRAINT_KINDS))}",
        )

    # PosId values, validated for the same reason the kind is. Probed against
    # 1.1.3: an out-of-range position does not raise and does not crash - it is
    # silently accepted and produces a nonsense constraint, with the solver
    # reporting a negative dof. That is the silent-wrong-result class rather
    # than the fatal one, and it is still wrong.
    #
    #  0 none, 1 start, 2 end, 3 mid. Verified that 1..3 build cleanly and that
    #  0, 4 and 99 are accepted without complaint while wrecking the solve.
    for label, pos in (("first_pos", first_pos), ("second_pos", second_pos)):
        if pos not in _POS_IDS:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"{label} must be one of "
                + ", ".join(f"{v} ({n})" for v, n in sorted(_POS_NAMES.items()))
                + f", got {pos!r}",
            )

    # A Distance constraint's value is the only one that is used, and a
    # non-positive or non-finite one produces a constraint that either cannot
    # be satisfied or cannot be built. NaN in particular reaches the native
    # constructor, which AD-25 exists to prevent.
    if kind == "Distance":
        if value != value or value in (float("inf"), float("-inf")):
            _fail(FAULT_BAD_GEOMETRY, f"distance must be finite, got {value!r}")
        if value <= 0:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"distance must be positive, got {value}. A negative or zero "
                f"Distance constrains two points to coincide or to separate "
                f"impossibly.",
            )

    try:
        if kind == "Distance":
            constraint = Sketcher.Constraint(
                kind, first_index, first_pos, second_index, second_pos, value
            )
        else:
            # Passing the value argument to a two-element constraint CRASHES
            # FreeCAD outright rather than raising, which takes the whole
            # bridge process down with it. Verified against 1.1.3: the 4-arg
            # form returns a Constraint, the 6-arg form kills the interpreter.
            constraint = Sketcher.Constraint(
                kind, first_index, first_pos, second_index, second_pos
            )
    except Exception as error:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"cannot build a {kind} constraint: {error}. Valid kinds include "
            f"Coincident, Horizontal, Vertical, Parallel, Perpendicular, "
            f"Equal, Distance.",
        )
        return {}  # unreachable
    # Named before the constraint exists, for the same reason as a geometry
    # name: nothing here can fail once the constraint is in.
    existing_names = {c.Name for c in sketch.Constraints if getattr(c, "Name", "")}
    _check_name(None, name, "constraint", existing=existing_names)

    index = sketch.addConstraint(constraint)
    doc.recompute()
    if name:
        # FreeCAD attaches the name to the constraint rather than to its index,
        # so unlike a geometry name this one needs no reindexing - verified
        # against 1.1.3: deleting an earlier constraint leaves the later name
        # attached to the right one.
        try:
            sketch.renameConstraint(index, name)
            doc.recompute()
        except Exception as error:
            _fail(FAULT_BAD_GEOMETRY, f"cannot name the constraint: {error}")
    dof = _solve(sketch)
    return {
        "constraint_index": index + 1,
        "constraint_count": sketch.ConstraintCount,
        "dof": dof,
        "fully_constrained": bool(sketch.FullyConstrained),
        # A negative dof means the sketch is over-constrained, which FreeCAD
        # reports rather than refuses. Surfaced because the constraint was
        # still added: the caller needs to know it is now fighting itself.
        "over_constrained": dof < 0,
    }


def _constraint_ref(sketch: Any, reference: Any, what: str = "index") -> int:
    """Turn a constraint name or a 1-based index into a 1-based index."""
    if isinstance(reference, str):
        for index, constraint in enumerate(sketch.Constraints):
            if getattr(constraint, "Name", "") == reference:
                return index + 1
        known = sorted(c.Name for c in sketch.Constraints if getattr(c, "Name", ""))
        _fail(
            FAULT_NO_SUCH_CONSTRAINT,
            f"no constraint named {reference!r} in {sketch.Name}"
            + (f"; it has {', '.join(known)}" if known else "; none are named"),
        )
    if isinstance(reference, bool) or not isinstance(reference, int):
        _fail(
            FAULT_NO_SUCH_CONSTRAINT,
            f"{what} must be a constraint name or a 1-based index, got {reference!r}",
        )
    return reference


def set_constraint_value(
    document: str,
    sketch_name: str,
    reference: Any,
    value: float,
) -> dict[str, Any]:
    """Change the value of a dimensional constraint, by name or by index.

    This is the reason to name a constraint. A Distance constraint added by
    index can only be *removed* and re-added to change it; a named one can be
    re-driven, which is the difference between a parametric sketch and a
    finished one. FreeCAD's own solver moves the geometry when this is called,
    so a named dimension is a handle on the model rather than a label.

    Only dimensional constraints have a value. A Coincident has none, and
    setting one is refused with that said plainly rather than by whatever
    FreeCAD happens to do with it.
    """
    import FreeCAD

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    index = _constraint_ref(sketch, reference)
    amount = _number(value, "value")
    if amount <= 0:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"value must be positive, got {amount}. A zero or negative "
            f"dimension constrains two points to coincide or separate "
            f"impossibly.",
        )
    constraint = sketch.Constraints[index - 1]
    kind = getattr(constraint, "Type", "")
    if kind != "Distance":
        _fail(
            FAULT_BAD_GEOMETRY,
            f"constraint {index} is a {kind}, which has no value to set. Only "
            f"a Distance constraint is dimensional; use "
            f"remove_sketch_constraint and add_sketch_constraint to change "
            f"another kind.",
        )
    before = sketch.GeometryCount
    try:
        sketch.setDatum(index - 1, FreeCAD.Units.Quantity(f"{amount} mm"))
    except Exception as error:
        _fail(FAULT_BAD_GEOMETRY, f"cannot set the constraint: {error}")
    doc.recompute()
    if sketch.GeometryCount != before:
        # FreeCAD replaced geometry rather than moving it. The sketch is now
        # not what the caller described, and the constraint count may have
        # changed under them, so this is reported rather than passed over.
        sketch.undo()
        doc.recompute()
        _fail(
            FAULT_BAD_GEOMETRY,
            f"setting that value would have replaced sketch geometry "
            f"({before} pieces would become {sketch.GeometryCount}); the sketch "
            f"was rolled back. The dimension is probably larger than the "
            f"geometry it is driving.",
        )
    # Read back from the constraint rather than echoing what was sent: a
    # constraint the solver clamped would otherwise report the request.
    applied = getattr(sketch.Constraints[index - 1], "Value", amount)
    return {
        "constraint_index": index,
        "constraint_name": getattr(constraint, "Name", ""),
        "value": applied,
        "dof": _solve(sketch),
    }


def _solve(sketch: Any) -> int:
    """The sketch's remaining degrees of freedom, or -1 if the solver cannot say.

    Guarded because it is the one call in this area that can fail on a
    malformed sketch, and an exception escaping here would surface as an
    xmlrpc-level error rather than a typed fault - the caller would get a bare
    xmlrpc Fault instead of a ``kind`` and a ``hint``. -1 is also FreeCAD's own
    convention for "solver could not converge", so the value is meaningful
    rather than merely safe, and ``over_constrained`` is checked against it.
    """
    try:
        return int(sketch.solve())
    except Exception:
        return -1


def sketch_status(document: str, sketch_name: str) -> dict[str, Any]:
    """Report whether a profile is closed and usable, before extruding it.

    Exists because an unclosed profile does not fail at extrude time - it
    produces a wrong solid instead. ``closed`` is the thing to check first, and
    ``extrude_sketch`` refuses a profile where it is false.
    """
    import Part

    sketch = _sketch(document, sketch_name)
    shape = sketch.Shape
    geometry_count = sketch.GeometryCount

    # 0-based inside, 1-based out, so `names` can be looked up directly by the
    # element numbers the constraints report below.
    names = {index - 1: name for index, name in _names(sketch).items()}
    result: dict[str, Any] = {
        "sketch": sketch_name,
        "geometry_count": geometry_count,
        "constraint_count": sketch.ConstraintCount,
        # The name mapping, so a caller can see what it named and stop
        # counting. `geometry` is 1-based like every index in this surface.
        "geometry": [
            {"geometry": index, "name": name}
            for index, name in sorted(_names(sketch).items())
        ],
        # Every constraint, with the element references it actually resolved
        # to and the names of those elements where they have one. Without this
        # a caller cannot tell which geometry a constraint ended up on, which
        # is the one thing worth knowing after constraining by name.
        "constraints": [
            {
                "constraint": position + 1,
                "name": getattr(constraint, "Name", ""),
                "kind": getattr(constraint, "Type", ""),
                "first": getattr(constraint, "First", -1) + 1,
                "first_pos": getattr(constraint, "FirstPos", 0),
                "first_name": names.get(getattr(constraint, "First", -1), ""),
                "second": getattr(constraint, "Second", -1) + 1,
                "second_pos": getattr(constraint, "SecondPos", 0),
                "second_name": names.get(getattr(constraint, "Second", -1), ""),
                "value": getattr(constraint, "Value", None),
            }
            for position, constraint in enumerate(sketch.Constraints)
        ],
        "closed": False,
        "edge_count": 0,
        "wire_count": 0,
        "area": 0.0,
        "dof": 0,
        "fully_constrained": False,
        "over_constrained": False,
        # Why `closed` is false, so a caller does not have to infer it. The
        # distinction matters: an open profile is a drawing mistake, while
        # several wires is a plate with holes, which is a valid shape this
        # surface simply cannot extrude in one step.
        "closed_reason": "",
    }
    if geometry_count == 0 or shape.isNull():
        result["closed_reason"] = "no geometry yet"
        return result

    wires = shape.Wires
    result["edge_count"] = len(shape.Edges)
    result["wire_count"] = len(wires)
    result["dof"] = _solve(sketch)
    result["fully_constrained"] = bool(sketch.FullyConstrained)
    result["over_constrained"] = result["dof"] < 0

    if len(wires) > 1:
        result["closed_reason"] = (
            f"{len(wires)} separate wires, so the profile has an interior "
            f"boundary - a plate with holes. Extrude the outer outline and cut "
            f"the holes with boolean_op."
        )
    elif not wires:
        result["closed_reason"] = "no wire"
    elif not wires[0].isClosed():
        result["closed_reason"] = (
            "the outline does not close; its ends fail to meet end to end"
        )

    if len(wires) == 1 and wires[0].isClosed():
        result["closed"] = True
        # Part.Face, because the sketch's own Shape.Area is 0.0 for a wire.
        try:
            result["area"] = Part.Face(Part.Wire(wires[0].Edges)).Area
        except Exception:
            result["area"] = 0.0
    return result


def attach_sketch_to_face(
    document: str,
    sketch_name: str,
    target: str,
    face_name: int,
) -> dict[str, Any]:
    """Snap a sketch onto a planar face of another object, flat to it.

    The sketch takes the face's position and orientation and extrudes normal to
    it, so a profile can be drawn on a surface rather than in a plane the
    caller has to compute. Verified against 1.1.3: attaching a 30x15 sketch to
    the top face of a box moved it to z=4 and extruding 2mm gave 900mm3
    spanning z 4 to 6.

    ``face_name`` is a 1-based index as reported by describe_geometry, the
    same convention edges use. FreeCAD's property is ``AttachmentSupport``; the
    FreeCAD 0.x name was ``Support``, which raises here.
    """
    import Part

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    host = _object(doc, target)

    # A sketch attached to itself is a self-referential parametric link: the
    # attachment engine would resolve the support by recomputing the very
    # object it is recomputing. That is a plausible wedged FreeCAD process
    # rather than a catchable exception, which is the failure class AD-20 and
    # AD-22 both exist to prevent. Cheap to refuse, so refuse it.
    if host.Name == sketch.Name:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{sketch_name} cannot be attached to its own face; a sketch has no "
            f"face to attach to, and self-reference would make it depend on "
            f"itself during recompute",
        )

    # A face is addressed by a 1-based integer, exactly as edges are. The
    # previous form took a "Face{N}" string matched by regex, which meant one
    # caller mistake got three different answers across the surface: Edge99
    # raised BadGeometry, Face99 raised NoSuchFace, and "face6" raised
    # BadGeometry. One convention, one error.
    if isinstance(face_name, bool) or not isinstance(face_name, int):
        _fail(
            FAULT_BAD_GEOMETRY,
            f"face must be an integer index, got {face_name!r}; use "
            f"describe_geometry on {target} to list its faces",
        )
    index = face_name
    shape = getattr(host, "Shape", None)
    if shape is None or shape.isNull():
        _fail(FAULT_NO_SHAPE, f"{target} has no shape to attach to")
    if not 1 <= index <= len(shape.Faces):
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{target} has {len(shape.Faces)} faces, so Face{index} does not exist",
        )
    face = shape.Faces[index - 1]
    face_ref = f"Face{index}"
    # Only a planar face can carry a flat sketch. Refusing here beats an
    # attachment that silently produces a degenerate placement.
    #
    # The area test is belt-and-braces: no FreeCAD primitive probed on 1.1.3
    # yields a zero-area Part.Plane (a zero-height cylinder still has two
    # full-radius caps), so this branch is not reachable through the current
    # tool surface. It is kept because a caller can build the degenerate case
    # by other means and the cost is one comparison, but it is not the guard
    # doing the work here - planarity is.
    if not isinstance(face.Surface, Part.Plane):
        _fail(
            FAULT_NO_SUCH_FACE,
            f"{face_ref} of {target} is a {type(face.Surface).__name__}, not a "
            f"plane; a sketch can only attach flat to a planar face",
        )
    if not face.Area > 1e-9:
        _fail(
            FAULT_NO_SUCH_FACE,
            f"{face_ref} of {target} is a plane of area {face.Area:.6g}, which "
            f"is too small to carry a profile",
        )

    try:
        sketch.AttachmentSupport = [(host, (face_ref,))]
        sketch.MapMode = "FlatFace"
    except Exception as error:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"cannot attach {sketch_name} to {face_ref} of {target}: {error}",
        )
        return {}  # unreachable
    doc.recompute()

    # Post-condition. Reported rather than asserted: no reachable failure was
    # found on 1.1.3 - a bad target raises ObjectNotFound before the
    # assignment, a non-planar face is refused above, and a valid planar
    # attachment resolves. It is kept because the cost is two comparisons and
    # the alternative is trusting FreeCAD's assignment to have taken, but
    # `placement` is what a caller actually reads, and the test asserts that
    # against the face's real position rather than asserting this branch fires.
    origin = sketch.Placement.Base
    plane = face.Surface
    attached = (
        sketch.MapMode == "FlatFace"
        and abs(plane.Axis.dot(origin.sub(plane.Position))) < 1e-6
    )

    return {
        "sketch": sketch_name,
        "attached_to": target,
        "face": face_ref,
        "map_mode": sketch.MapMode,
        "placement": _vec(origin),
        "on_face_plane": attached,
    }


def sketch_to_face(
    document: str,
    sketch_name: str,
    result_name: str,
) -> str:
    """Turn a closed sketch profile into a planar face object.

    A wire encloses no area, which is why a sketch's own ``Shape.Area`` reads
    0.0. This wraps the wire in a real face, so the result has an area and can
    be measured, cut and exported like any other face.

    The result is a static ``Part::Feature`` snapshot, not a parametric link:
    editing the sketch afterwards does not update it. Same rule as AD-21 for
    linear_array, and the tool description says so for the same reason.

    It is also not extrudable through this surface - ``extrude_sketch``
    requires a sketch - so the docstring does not claim otherwise, which an
    earlier draft did and which no tool could satisfy.

    Verified against 1.1.3: a 30x15 profile gave a face of area 450.0, and
    cutting a 3mm hole from it gave 421.7257 = 450 - pi*9, with two wires.
    """
    import Part

    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    # Same closedness check as extrude_sketch: a face needs a closed outline,
    # and Part.Face on an open wire does not raise, it returns something wrong.
    wire = _profile_wire(sketch)

    # Build and validate the face BEFORE creating the object. Creating first
    # left an orphan Part::Feature with a null Shape in the document on every
    # failure path, under the caller's own name: list_objects showed it,
    # describe_geometry on it raised NO_SHAPE, and a retry hit the same name.
    try:
        face = Part.Face(Part.Wire(wire.Edges))
    except Exception as error:
        # Part.Face does not always raise cleanly on a degenerate wire, and an
        # exception escaping here reaches the client as xmlrpc's generic code 1
        # rather than a named sketch fault.
        _fail(
            FAULT_BAD_GEOMETRY,
            f"{sketch_name} will not make a face: {error}. A self-intersecting "
            f"or degenerate profile cannot become one",
        )
        return ""  # unreachable
    # `not > 0.0` rather than `<= 0.0`: NaN compares false against 0.0, so a
    # NaN area would otherwise pass and be reported as a valid face.
    if not face.isValid() or not face.Area > 0.0:
        _fail(
            FAULT_EMPTY_RESULT,
            f"{sketch_name} makes no usable face (valid={face.isValid()}, "
            f"area={face.Area}); a profile must enclose area and not "
            f"self-intersect. Check sketch_status",
        )

    feature = _feature(result_name, "Part::Feature", document)
    feature.Shape = face
    doc.recompute()
    return feature.Name


def extrude_sketch(
    document: str,
    sketch_name: str,
    result_name: str,
    depth: float,
) -> str:
    """Extrude a closed profile into a solid, normal to the sketch plane.

    Direction is not a parameter: the sketch's own Placement decides which way
    is out, and it is set with the same set_placement every other object uses.
    One rule for orientation rather than two that can disagree.

    Uses Part::Extrusion rather than PartDesign::Pad. Both were verified to
    produce the same volume, but Pad printed an out-of-scope warning even on
    success, so its correctness cannot be inferred from a clean exit.
    """
    doc = _require(document)
    sketch = _sketch(document, sketch_name)
    length = _number(depth, "depth")
    if length == 0:
        _fail(FAULT_BAD_GEOMETRY, "depth must not be zero")
    if length < 0:
        _fail(
            FAULT_BAD_GEOMETRY,
            f"depth must be positive; reverse the sketch placement instead of "
            f"extruding by a negative amount, got {length}",
        )

    # The check that matters. Without it this returns a wrong solid silently.
    _profile_wire(sketch)

    # Everything knowable before the feature exists is checked before it is
    # created. The solid checks below can only run after a recompute, and a
    # refusal there used to leave a half-built Part::Extrusion in the document
    # under the caller's own name - the orphan that 839a08e fixed in
    # sketch_to_face and that survived here. The feature is removed before the
    # error is raised, so a failed extrude leaves the document as it found it.
    feature = _feature(result_name, "Part::Extrusion", document)
    try:
        feature.Base = sketch
        feature.DirMode = "Normal"
        feature.LengthFwd = length
        feature.Solid = True
        doc.recompute()

        shape = feature.Shape
        if shape.isNull() or not shape.Solids:
            _fail(
                FAULT_EMPTY_RESULT,
                f"extruding {sketch_name} by {length} produced no solid; the "
                f"profile may be self-intersecting or degenerate. Check "
                f"sketch_status.",
            )
        if len(shape.Solids) > 1:
            _fail(
                FAULT_BAD_GEOMETRY,
                f"extruding {sketch_name} produced {len(shape.Solids)} separate "
                f"solids, which means the profile is not a single connected "
                f"outline. Check sketch_status.",
            )
        if not shape.isValid():
            _fail(
                FAULT_BAD_GEOMETRY,
                f"extruding {sketch_name} by {length} produced an invalid "
                f"solid; the profile is self-intersecting. Check "
                f"sketch_status.",
            )
    except Fault:
        # A Fault carries the typed error the caller should see. Remove the
        # feature first so nothing is left behind, then let it propagate.
        doc.removeObject(feature.Name)
        doc.recompute()
        raise
    return feature.Name


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
