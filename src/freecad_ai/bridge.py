"""Client and process launcher for the FreeCAD XML-RPC bridge.

Runs on the project's own Python. Never imports FreeCAD — that module only
loads inside FreeCAD's bundled 3.11 interpreter, and the whole point of the
socket-style boundary is that the two never share one.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import threading
import time
import xmlrpc.client
from pathlib import Path
from typing import Any, cast

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9875

# The bridge is unauthenticated and binds loopback. FREECAD_AI_HOST exists so
# the value is visible and testable, but widening it hands anyone who can reach
# the port full control of FreeCAD — do not use it to expose this.
ENV_HOST = "FREECAD_AI_HOST"
ENV_PORT = "FREECAD_AI_PORT"
ENV_FREECAD_BIN = "FREECAD_AI_FREECAD_BIN"

# freecadcmd is not on PATH, and both 1.0 and 1.1 are installed side by side,
# so an unqualified path silently binds the wrong one. CI installs FreeCAD
# elsewhere, hence the override.
DEFAULT_FREECAD_BIN = Path(r"C:\Program Files\FreeCAD 1.1\bin")


def configured_freecad_bin() -> Path:
    """Directory holding ``freecadcmd.exe``, from the environment or default."""
    override = os.environ.get(ENV_FREECAD_BIN)
    return Path(override) if override else DEFAULT_FREECAD_BIN


def configured_host() -> str:
    """Host to bind, from ``FREECAD_AI_HOST`` or the loopback default."""
    return os.environ.get(ENV_HOST) or DEFAULT_HOST


def configured_port() -> int:
    """Port to bind, from ``FREECAD_AI_PORT`` or the default.

    Read at call time rather than at import so tests and a caller can change
    it, and so a malformed value is reported where it can be acted on.
    """
    raw = os.environ.get(ENV_PORT)
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as error:
        raise BridgeError(f"{ENV_PORT}={raw!r} is not an integer") from error
    if not 1 <= port <= 65535:
        raise BridgeError(f"{ENV_PORT}={port} is outside 1-65535")
    return port


# freecadcmd is not on PATH; both 1.0 and 1.1 are installed side by side, so an
# unqualified path silently binds the wrong one.
FREECAD_1_1_BIN = Path(r"C:\Program Files\FreeCAD 1.1\bin")

# The FreeCAD-side script lives inside the package so it ships in the wheel.
# It must stay a sibling of this module rather than a path relative to the
# project root: an installed wheel has no project root, and deriving one from
# __file__ resolves to site-packages' parent, which contains no script.
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "_freecad_bridge.py"


class BridgeError(RuntimeError):
    """Base class for every failure reaching the caller."""

    hint: str = ""


class BridgeUnreachable(BridgeError):
    """The FreeCAD process is not answering on the bridge port."""

    hint = (
        "FreeCAD is not running. Call `connect` to start it, "
        "or check that the port is free."
    )


class DocumentNotFound(BridgeError):
    hint = "Call `new_document` with that name first."


class DocumentExists(BridgeError):
    hint = "Pass reuse=False only if you truly need a second document."


class ObjectNotFound(BridgeError):
    hint = "Call `list_objects` to see what the document contains."


class PropertyNotFound(BridgeError):
    hint = "Call `get_properties` to see the available properties."


class NoShape(BridgeError):
    hint = "Only geometry objects have shapes; this one does not."


class NoSuchDimension(BridgeError):
    hint = "Check the primitive's real property names before setting dimensions."


class ExportFailed(BridgeError):
    hint = (
        "Check the path's directory exists and the extension is supported, "
        "for example .step or .stl."
    )


class SaveFailed(BridgeError):
    hint = "Check the path is writable and the file is not locked."


class BadOperation(BridgeError):
    hint = "Operation must be cut, fuse or common, and the two objects must differ."


class BridgeInternalError(BridgeError):
    """The bridge raised an unclassified exception."""

    hint = (
        "This is a bug in freecad-ai or the bridge; "
        "the message carries the original error."
    )


class UnknownBridgeFunction(BridgeError):
    """A bridge function that does not exist — a bug in this server."""

    hint = "This is a bug in freecad-ai, not a mistake in your request."


# Mirrors the FAULT_* codes in bridge/freecad_bridge.py. Keep the two in sync.
# 1 is xmlrpc's code for any uncaught server exception, so it must map to the
# generic error rather than a specific condition.
_FAULT_MAP: dict[int, type[BridgeError]] = {
    1: BridgeInternalError,
    100: UnknownBridgeFunction,
    101: DocumentNotFound,
    102: DocumentExists,
    103: ObjectNotFound,
    104: PropertyNotFound,
    105: NoShape,
    106: NoSuchDimension,
    107: ExportFailed,
    108: SaveFailed,
    109: BadOperation,
}


def _raise_fault(error: xmlrpc.client.Fault) -> None:
    cls = _FAULT_MAP.get(error.faultCode)
    if cls is None:
        raise BridgeError(
            f"FreeCAD bridge error {error.faultCode}: {error.faultString}"
        ) from error
    raise cls(error.faultString) from error


class Bridge:
    """Synchronous XML-RPC client for a running FreeCAD bridge.

    Not thread-safe on its own: MCPServer runs synchronous tool functions on
    anyio's worker thread pool, so parallel tool calls reach one shared
    ``ServerProxy`` from several threads at once. That object's single
    ``HTTPConnection`` cannot serve overlapping requests and raises
    ``CannotSendRequest`` or ``ResponseNotReady`` instead of queueing.

    Every call is therefore serialised. FreeCAD's ``SimpleXMLRPCServer`` is
    single-threaded too, so this matches the server rather than fighting it.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        resolved_host = host if host is not None else configured_host()
        resolved_port = port if port is not None else configured_port()
        self.host = resolved_host
        self.port = resolved_port
        self._proxy = xmlrpc.client.ServerProxy(
            f"http://{resolved_host}:{resolved_port}/", allow_none=False
        )
        self._lock = threading.Lock()

    def _call(self, name: str, *args: Any) -> Any:
        """Invoke a bridge function. The XML-RPC wire format is untyped, so
        every public method casts this result to its declared return type."""
        with self._lock:
            try:
                return self._proxy.dispatch(name, *args)
            except xmlrpc.client.Fault as error:
                _raise_fault(error)
            except (OSError, http.client.HTTPException) as error:
                raise BridgeUnreachable(
                    f"cannot reach the FreeCAD bridge on {self._proxy}: {error}"
                ) from error
        raise BridgeError(f"{name} returned no result")  # pragma: no cover

    def ping(self) -> str:
        return cast(str, self._call("ping"))

    def version(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._call("version"))

    def list_documents(self) -> list[str]:
        return cast("list[str]", self._call("list_documents"))

    def new_document(self, name: str, reuse: bool = True) -> dict[str, Any]:
        """Create a document, or return the existing one when ``reuse``."""
        return cast("dict[str, Any]", self._call("new_document", name, bool(reuse)))

    def open_document(self, path: str) -> str:
        return cast(str, self._call("open_document", path))

    def export_object(self, name: str, object_name: str, path: str) -> str:
        return cast(str, self._call("export_object", name, object_name, path))

    def save_document(self, name: str, path: str) -> str:
        return cast(str, self._call("save_document", name, path))

    def list_objects(self, name: str) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._call("list_objects", name))

    def add_primitive(
        self, name: str, kind: str, object_name: str, dimensions: dict[str, float]
    ) -> str:
        return cast(
            str, self._call("add_primitive", name, kind, object_name, dimensions)
        )

    def instance_pid(self) -> int:
        """PID of the FreeCAD process actually serving this bridge."""
        return int(cast("dict[str, Any]", self._call("instance_id"))["pid"])

    def describe_geometry(self, name: str, object_name: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]", self._call("describe_geometry", name, object_name)
        )

    def fillet(
        self,
        name: str,
        object_name: str,
        result_name: str,
        edges: list[int],
        radius: float,
    ) -> str:
        return cast(
            str,
            self._call(
                "fillet",
                name,
                object_name,
                result_name,
                [int(e) for e in edges],
                float(radius),
            ),
        )

    def chamfer(
        self,
        name: str,
        object_name: str,
        result_name: str,
        edges: list[int],
        size: float,
    ) -> str:
        return cast(
            str,
            self._call(
                "chamfer",
                name,
                object_name,
                result_name,
                [int(e) for e in edges],
                float(size),
            ),
        )

    def mirror(
        self,
        name: str,
        object_name: str,
        result_name: str,
        origin: list[float],
        normal: list[float],
    ) -> str:
        return cast(
            str,
            self._call(
                "mirror",
                name,
                object_name,
                result_name,
                [float(v) for v in origin],
                [float(v) for v in normal],
            ),
        )

    def linear_array(
        self,
        name: str,
        object_name: str,
        result_name: str,
        offset: list[float],
        count: int,
    ) -> str:
        return cast(
            str,
            self._call(
                "linear_array",
                name,
                object_name,
                result_name,
                [float(v) for v in offset],
                int(count),
            ),
        )

    def list_primitive_types(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._call("list_primitive_types"))

    def get_properties(self, name: str, object_name: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._call("get_properties", name, object_name))

    def set_property(self, name: str, object_name: str, prop: str, value: Any) -> str:
        return cast(str, self._call("set_property", name, object_name, prop, value))

    def remove_object(self, name: str, object_name: str) -> str:
        return cast(str, self._call("remove_object", name, object_name))

    def set_placement(
        self,
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
        return cast(
            "dict[str, Any]",
            self._call(
                "set_placement",
                name,
                object_name,
                float(x),
                float(y),
                float(z),
                float(axis_x),
                float(axis_y),
                float(axis_z),
                float(angle),
            ),
        )

    def boolean_op(
        self,
        name: str,
        base_name: str,
        tool_name: str,
        operation: str,
        result_name: str,
    ) -> str:
        return cast(
            str,
            self._call(
                "boolean_op", name, base_name, tool_name, operation, result_name
            ),
        )

    def shape_summary(self, name: str, object_name: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._call("shape_summary", name, object_name))


def wait_until_ready(
    host: str | None = None,
    port: int | None = None,
    timeout: float = 30.0,
) -> Bridge:
    """Block until the bridge answers, or raise once ``timeout`` elapses.

    FreeCAD needs several seconds to initialise before it binds the port, so
    callers should use this rather than a bare sleep. One Bridge is built
    outside the loop: constructing it per attempt would open and abandon a
    connection on every poll.
    """
    bridge = Bridge(host, port)
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if bridge.ping() == "pong":
                return bridge
        except BridgeError as error:
            last = error
        else:
            last = BridgeError("bridge answered ping with an unexpected value")
        time.sleep(0.25)
    raise BridgeError(f"bridge did not become ready within {timeout}s: {last}")


class PortInUse(BridgeError):
    """The port is held by a FreeCAD that is not the one we launched."""

    hint = (
        "Another FreeCAD is already using this port, so the one started here "
        "could not bind. Set FREECAD_AI_PORT to a free port, or stop the "
        "other instance."
    )


def freecadcmd_path() -> Path:
    """Full path to FreeCAD's console binary."""
    candidate = configured_freecad_bin() / "freecadcmd.exe"
    if not candidate.is_file():
        raise BridgeError(f"freecadcmd.exe not found at {candidate}")
    return candidate


def start_headless(
    host: str | None = None,
    port: int | None = None,
    timeout: float = 30.0,
) -> tuple[subprocess.Popen[bytes], Bridge]:
    """Launch ``freecadcmd`` with the bridge and return the process and client.

    Headless is the default target: it needs no display, starts in CI, and its
    Qt state is unambiguous. See AGENTS.md — Qt and FreeCADGui both import
    cleanly in headless mode, so never infer GUI capability from imports.
    """
    if not BRIDGE_SCRIPT.is_file():
        raise BridgeError(f"bridge script missing at {BRIDGE_SCRIPT}")

    resolved_host = host if host is not None else configured_host()
    resolved_port = port if port is not None else configured_port()

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # DEVNULL, not PIPE. FreeCAD writes progress output ("Recompute......")
    # continuously while modelling and exporting; an unread pipe fills its
    # ~64KB buffer, after which FreeCAD blocks in write() and stops answering
    # the bridge entirely. Nothing reads these streams, so capturing them only
    # creates a deadlock. Readiness is confirmed by ping, not by output.
    process = subprocess.Popen(
        [
            str(freecadcmd_path()),
            str(BRIDGE_SCRIPT),
            resolved_host,
            str(resolved_port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        bridge = wait_until_ready(resolved_host, resolved_port, timeout)
        # Confirm the bridge we reached is the process we launched. If the port
        # was already held, our FreeCAD failed to bind and wait_until_ready
        # answered from that other process — adopting it would hand the model
        # someone else's open documents.
        served_by = bridge.instance_pid()
        if served_by != process.pid:
            raise PortInUse(
                f"port {resolved_port} is served by FreeCAD pid {served_by}, "
                f"not the pid {process.pid} started here"
            )
    except BridgeError:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return process, bridge


def stop(process: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
    """Terminate a bridge process, escalating to kill if it ignores that."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
