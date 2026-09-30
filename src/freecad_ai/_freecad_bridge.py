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
