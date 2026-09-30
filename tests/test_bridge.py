"""Unit tests for the bridge client.

These never start FreeCAD. End-to-end coverage lives in
``test_integration.py``, which is marked ``integration``.
"""

from __future__ import annotations

import re
import subprocess
import time
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from freecad_ai import bridge as bridge_module
from freecad_ai.bridge import (
    BRIDGE_SCRIPT,
    FREECAD_1_1_BIN,
    BadOperation,
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


def test_bridge_serialises_calls() -> None:
    """The client must hold a lock; the shared proxy cannot be re-entered.

    MCPServer runs sync tool functions on a worker thread pool, so concurrent
    calls arrive from several threads. Without the lock the underlying
    HTTPConnection raises CannotSendRequest / ResponseNotReady.
    """
    lock = Bridge()._lock
    assert hasattr(lock, "acquire")
    assert lock.acquire(blocking=False)
    lock.release()


def test_concurrent_calls_do_not_interleave() -> None:
    """Overlapping calls must be serialised, not raced."""
    overlap: list[int] = []
    active = [0]

    class SlowProxy:
        def dispatch(self, name: str, *args: object) -> str:
            active[0] += 1
            overlap.append(active[0])
            time.sleep(0.01)
            active[0] -= 1
            return "ok"

    instance = Bridge()
    instance._proxy = SlowProxy()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(instance.ping) for _ in range(4)]]

    assert results == ["ok"] * 4
    assert max(overlap) == 1, "calls overlapped inside the bridge"


def test_config_reads_host_and_port_from_env(monkeypatch: Any) -> None:
    monkeypatch.delenv(bridge_module.ENV_HOST, raising=False)
    monkeypatch.delenv(bridge_module.ENV_PORT, raising=False)
    assert bridge_module.configured_host() == bridge_module.DEFAULT_HOST
    assert bridge_module.configured_port() == bridge_module.DEFAULT_PORT

    monkeypatch.setenv(bridge_module.ENV_HOST, "0.0.0.0")
    monkeypatch.setenv(bridge_module.ENV_PORT, "12345")
    assert bridge_module.configured_host() == "0.0.0.0"
    assert bridge_module.configured_port() == 12345


@pytest.mark.parametrize("bad", ["not-a-number", "", "0", "70000", "-1"])
def test_config_rejects_bad_ports(monkeypatch: Any, bad: str) -> None:
    monkeypatch.setenv(bridge_module.ENV_PORT, bad)
    if bad == "":
        assert bridge_module.configured_port() == bridge_module.DEFAULT_PORT
        return
    with pytest.raises(BridgeError, match=bridge_module.ENV_PORT):
        bridge_module.configured_port()


def test_bridge_uses_configured_port(monkeypatch: Any) -> None:
    monkeypatch.setenv(bridge_module.ENV_PORT, "12345")
    assert Bridge().port == 12345
    assert "12345" in str(Bridge()._proxy)


def test_explicit_arguments_beat_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv(bridge_module.ENV_PORT, "12345")
    assert Bridge(port=9999).port == 9999


def test_freecad_output_streams_are_never_piped(monkeypatch: Any) -> None:
    """FreeCAD's output must not be captured, or the bridge deadlocks.

    FreeCAD writes "Recompute......" progress output continuously. With
    subprocess.PIPE and nothing reading them, the ~64KB pipe buffer fills,
    FreeCAD blocks in write(), and it stops answering the bridge entirely.
    The process is still alive, so this presents as a hang rather than an
    error, and only a heavy workload reaches the buffer limit.

    Readiness is confirmed by ping, so the streams carry nothing we need.
    """
    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop after Popen")

    monkeypatch.setattr(bridge_module.subprocess, "Popen", spy)
    with pytest.raises(RuntimeError, match="stop after Popen"):
        bridge_module.start_headless()

    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


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
        109: BadOperation,
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


def test_bridge_script_ships_inside_the_package() -> None:
    """The FreeCAD-side script must ship in the wheel.

    An installed package has no project root, so a script resolved relative to
    one cannot be found. Sibling-of-module is the only layout that works both
    in the source tree and in site-packages.
    """
    assert BRIDGE_SCRIPT.is_file()
    assert BRIDGE_SCRIPT.parent == Path(bridge_module.__file__).resolve().parent
    assert BRIDGE_SCRIPT.name == "_freecad_bridge.py"


def test_no_package_module_imports_the_bridge_script() -> None:
    """`_freecad_bridge` is launched by path, never imported.

    That is what keeps `import FreeCAD` confined to one file: importing it
    into the server process would pull FreeCAD's 3.11-only modules into 3.14.
    """
    package = Path(bridge_module.__file__).resolve().parent
    pattern = re.compile(
        r"^\s*(?:from\s+freecad_ai\._freecad_bridge\s+import|"
        r"from\s+\._freecad_bridge\s+import|"
        r"import\s+freecad_ai\._freecad_bridge|"
        r"import\s+\._freecad_bridge)",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in package.glob("*.py")
        if path.name != "_freecad_bridge.py"
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"these import the bridge script: {offenders}"


def test_only_the_bridge_script_imports_freecad() -> None:
    package = Path(bridge_module.__file__).resolve().parent
    offenders = [
        path.name
        for path in package.glob("*.py")
        if path.name != "_freecad_bridge.py"
        and "import FreeCAD" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"these import FreeCAD in-process: {offenders}"


def test_bridge_script_imports_freecad_only_inside_functions() -> None:
    """Module-level `import FreeCAD` would break the script's own import.

    freecadcmd imports the script, so a top-level FreeCAD import would run at
    import time — which is fine there, but the file must also stay parseable
    and importable by 3.14 tooling.
    """
    text = BRIDGE_SCRIPT.read_text(encoding="utf-8")
    body = text.split('if __name__ == "__main__"', 1)[0]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("import FreeCAD") or stripped.startswith(
            "from FreeCAD"
        ):
            assert line.startswith((" ", "\t")), (
                "module-level FreeCAD import in the bridge script"
            )


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
