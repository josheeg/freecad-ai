"""Unit tests for the bridge client.

These never start FreeCAD. End-to-end coverage lives in
``test_integration.py``, which is marked ``integration``.
"""

from __future__ import annotations

import subprocess
import xmlrpc.client
from pathlib import Path

import pytest

from freecad_ai import bridge as bridge_module
from freecad_ai.bridge import (
    BRIDGE_SCRIPT,
    FREECAD_1_1_BIN,
    Bridge,
    BridgeError,
    BridgeInternalError,
    DocumentExists,
    DocumentNotFound,
    ExportFailed,
    NoShape,
    NoSuchDimension,
    ObjectNotFound,
    PropertyNotFound,
    SaveFailed,
    UnknownBridgeFunction,
    freecadcmd_path,
    stop,
    wait_until_ready,
)


class FakeProxy:
    """Stands in for xmlrpc.client.ServerProxy."""

    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def dispatch(self, name: str, *args: object) -> object:
        self.calls.append((name, args))
        if self._raises is not None:
            raise self._raises
        return self._result


def make_bridge(proxy: FakeProxy) -> Bridge:
    instance = Bridge()
    instance._proxy = proxy
    return instance


def test_ping_returns_result() -> None:
    bridge = make_bridge(FakeProxy(result="pong"))
    assert bridge.ping() == "pong"
    assert bridge._proxy.calls == [("ping", ())]


def test_fault_is_wrapped_in_bridge_error() -> None:
    fault = xmlrpc.client.Fault(1, "no such document: missing")
    bridge = make_bridge(FakeProxy(raises=fault))
    with pytest.raises(BridgeError, match="no such document"):
        bridge.new_document("missing")


def test_unreachable_bridge_raises_bridge_error() -> None:
    bridge = make_bridge(FakeProxy(raises=OSError("connection refused")))
    with pytest.raises(BridgeError, match="cannot reach"):
        bridge.ping()


def test_new_document_forwards_name_and_reuse() -> None:
    proxy = FakeProxy(result={"name": "Doc", "created": True})
    bridge = make_bridge(proxy)
    assert bridge.new_document("Doc") == {"name": "Doc", "created": True}
    assert proxy.calls == [("new_document", ("Doc", True))]


def test_new_document_can_refuse_reuse() -> None:
    proxy = FakeProxy(result={"name": "Doc", "created": False})
    bridge = make_bridge(proxy)
    bridge.new_document("Doc", reuse=False)
    assert proxy.calls == [("new_document", ("Doc", False))]


def test_export_object_forwards_all_arguments() -> None:
    proxy = FakeProxy(result="C:/out/part.step")
    bridge = make_bridge(proxy)
    assert bridge.export_object("Doc", "Box", "C:/out/part.step").endswith("part.step")
    assert proxy.calls == [
        ("export_object", ("Doc", "Box", "C:/out/part.step"))
    ]


def test_fault_codes_map_to_typed_errors() -> None:
    """Each bridge fault code must raise its own exception class.

    A collision here is how an unrelated server TypeError gets reported as a
    specific, misleading condition.
    """
    cases = {
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
    }
    for code, expected in cases.items():
        bridge = make_bridge(FakeProxy(raises=xmlrpc.client.Fault(code, "boom")))
        with pytest.raises(expected):
            bridge.ping()


def test_typed_errors_carry_recovery_hints() -> None:
    for cls in (DocumentNotFound, ObjectNotFound, PropertyNotFound, NoShape):
        assert cls("x").hint, f"{cls.__name__} has no recovery hint"


def test_bridge_internal_error_does_not_claim_user_error() -> None:
    """Code 1 is any uncaught server exception, not a specific condition."""
    assert BridgeInternalError("boom").hint.startswith("This is a bug")


def test_add_primitive_forwards_all_arguments() -> None:
    proxy = FakeProxy(result="Box")
    bridge = make_bridge(proxy)
    dimensions = {"Length": 10.0, "Width": 20.0, "Height": 30.0}
    assert bridge.add_primitive("Doc", "Part::Box", "Box", dimensions) == "Box"
    assert proxy.calls == [
        ("add_primitive", ("Doc", "Part::Box", "Box", dimensions))
    ]


def test_wait_until_ready_times_out_when_nothing_listens() -> None:
    with pytest.raises(BridgeError, match="did not become ready"):
        wait_until_ready(port=9, timeout=0.3)


def test_wait_until_ready_rejects_unexpected_ping() -> None:
    original = bridge_module.Bridge

    class WrongPing(original):  # type: ignore[misc, valid-type]
        def ping(self) -> str:
            return "nope"

    bridge_module.Bridge = WrongPing  # type: ignore[misc]
    try:
        with pytest.raises(BridgeError, match="did not become ready"):
            wait_until_ready(port=9, timeout=0.3)
    finally:
        bridge_module.Bridge = original  # type: ignore[misc]


def test_bridge_script_lives_outside_the_server_package() -> None:
    """The FreeCAD-side module must never be importable by server code.

    It is the only place allowed to ``import FreeCAD``; keeping it out of
    ``src/`` makes the 3.11/3.14 boundary structural.
    """
    assert BRIDGE_SCRIPT.is_file()
    assert Path(*BRIDGE_SCRIPT.parts[-2:]) == Path("bridge", "freecad_bridge.py")
    assert "src" not in BRIDGE_SCRIPT.parts


def test_server_package_never_imports_freecad() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "freecad_ai").rglob("*.py")
    for path in source:
        text = path.read_text(encoding="utf-8")
        assert "import FreeCAD" not in text, f"{path} imports FreeCAD in-process"


def test_freecadcmd_path_targets_1_1() -> None:
    assert FREECAD_1_1_BIN.name == "bin"
    assert FREECAD_1_1_BIN.parent.name == "FreeCAD 1.1"
    path = freecadcmd_path()
    assert path.parent == FREECAD_1_1_BIN


def test_stop_is_a_noop_for_an_exited_process() -> None:
    process = subprocess.Popen(["cmd", "/c", "exit", "0"])
    process.wait()
    stop(process)
    assert process.poll() == 0
