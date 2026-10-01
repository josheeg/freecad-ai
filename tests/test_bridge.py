"""Unit tests for the bridge client.

These never start FreeCAD. End-to-end coverage lives in
``test_integration.py``, which is marked ``integration``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
import time
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from freecad_ai import bridge as bridge_module
from freecad_ai.bridge import (
    BRIDGE_SCRIPT,
    FREECAD_1_1_BIN,
    BadGeometry,
    BadOperation,
    Bridge,
    BridgeError,
    BridgeInternalError,
    DocumentExists,
    DocumentNotFound,
    EmptyResult,
    ExportFailed,
    NoShape,
    NoSuchDimension,
    NoSuchFeature,
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
    assert proxy.calls == [("export_object", ("Doc", "Box", "C:/out/part.step"))]


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


def test_bridge_script_parses_as_python_3_11() -> None:
    """The FreeCAD-side script runs under 3.11, so it must parse as 3.11.

    The project's own tooling targets 3.14, and 3.14 accepted syntax that 3.11
    does not — `except A, B:` without parentheses is PEP 758. Ruff and mypy
    were both happy; FreeCAD refused to import the script, and because its
    output was discarded the only symptom was a bridge that never came up.

    `ast.parse(feature_version=...)` applies the older grammar, which is
    exactly the check that was missing.
    """
    source = BRIDGE_SCRIPT.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(BRIDGE_SCRIPT), feature_version=(3, 11))
    except SyntaxError as error:
        pytest.fail(
            f"{BRIDGE_SCRIPT.name} is not valid Python 3.11, which is what "
            f"FreeCAD's bundled interpreter runs: {error}"
        )


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

    # The invariant is "never a pipe", not "always DEVNULL": output now goes
    # to a file so a bridge that cannot start can explain itself.
    assert captured["stdout"] is not subprocess.PIPE
    assert "stderr" not in captured or captured["stderr"] is not subprocess.PIPE


def test_freecad_output_is_retained_for_diagnosis() -> None:
    """Startup failures must be explainable, not just a timeout.

    Output once went to DEVNULL, so a bridge script that would not import
    produced no clue at all — only "did not become ready within 30s". It now
    goes to a file, which keeps the deadlock fix and restores the diagnosis.
    """
    log = Path(tempfile.gettempdir()) / "freecad-ai-bridge-19999.log"
    if log.exists():
        log.unlink()
    log.write_text("line one\nline two\n", encoding="utf-8")
    fake = SimpleNamespace(freecad_ai_log=log)
    assert "line two" in bridge_module.bridge_log_tail(fake)


def test_freecad_bin_path_is_configurable(monkeypatch: Any) -> None:
    """CI installs FreeCAD elsewhere, so the path must not be hardcoded."""
    monkeypatch.delenv(bridge_module.ENV_FREECAD_BIN, raising=False)
    assert bridge_module.configured_freecad_bin() == bridge_module.DEFAULT_FREECAD_BIN

    monkeypatch.setenv(bridge_module.ENV_FREECAD_BIN, r"C:\fc\bin")
    assert bridge_module.configured_freecad_bin() == Path(r"C:\fc\bin")
    with pytest.raises(BridgeError, match=r"freecadcmd\.exe not found"):
        bridge_module.freecadcmd_path()


def test_missing_freecad_is_reported_clearly(monkeypatch: Any) -> None:
    monkeypatch.setenv(bridge_module.ENV_FREECAD_BIN, r"C:\definitely\not\here")
    with pytest.raises(BridgeError, match=r"freecadcmd\.exe not found"):
        bridge_module.freecadcmd_path()


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
        110: BadGeometry,
        111: NoSuchFeature,
        112: EmptyResult,
    }
    for code, expected in cases.items():
        bridge = make_bridge(FakeProxy(raises=xmlrpc.client.Fault(code, "boom")))
        with pytest.raises(expected):
            bridge.ping()


def test_fault_map_matches_the_bridge_source() -> None:
    """The client's fault table and the bridge's FAULT_* constants are one set.

    Both halves are correct on their own and still drift apart: a new
    FAULT_NOTHING_SPECIFIC added on the bridge side reaches the client as an
    unmapped code, which _raise_fault turns into a bare BridgeError. The
    specific condition is lost and the caller gets no hint - a silent
    regression that every other test here would pass straight through, because
    none of them add a code.

    Reads the constants out of the source rather than importing the bridge,
    which is launched by path and must never be imported by the package.
    """
    source = BRIDGE_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BRIDGE_SCRIPT), feature_version=(3, 11))
    declared: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("FAULT_")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)
            ):
                declared[target.id] = node.value.value

    assert declared, "no FAULT_* constants found in the bridge source"
    assert len(set(declared.values())) == len(declared), (
        f"two FAULT_* constants share a code: {declared}"
    )

    client_codes = set(bridge_module._FAULT_MAP)
    bridge_codes = set(declared.values())

    only_bridge = sorted(bridge_codes - client_codes)
    assert not only_bridge, (
        f"the bridge raises {only_bridge} but the client does not map them, so "
        f"they arrive as a bare BridgeError with no hint. Add them to "
        f"_FAULT_MAP in bridge.py."
    )
    only_client = sorted(client_codes - bridge_codes - {1})
    assert not only_client, (
        f"_FAULT_MAP in bridge.py handles {only_client}, which the bridge "
        f"never raises. Dead entries hide a renamed or deleted constant."
    )

    # Code 1 is xmlrpc's own code for any uncaught exception, so the bridge
    # does not declare it as a FAULT_* constant. It is expected on one side only,
    # and it must stay mapped or a genuine bug arrives as an unknown code.
    assert client_codes & {1} == {1}
    assert bridge_module._FAULT_MAP[1] is BridgeInternalError


def test_no_fault_constant_is_dead() -> None:
    """Every FAULT_* constant is actually raised somewhere in the bridge.

    The map test above catches a *code with no constant*; it cannot catch a
    *constant nothing raises* - and that is the shape a stale entry takes when
    a condition is renamed or the code path moves. `FAULT_NO_SUCH_CONSTRAINT`
    sat that way for several rounds: declared, mapped to a client class,
    required to carry a hint by the test below, and raised nowhere. Every test
    passed, and the class was fiction.

    Read from the source because the bridge must never be imported. Only
    direct `_fail(...)` and `raise Fault(...)` references count, which is why
    the codes are searched as names rather than as numbers.
    """
    source = BRIDGE_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BRIDGE_SCRIPT), feature_version=(3, 11))
    declared = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id.startswith("FAULT_")
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
    }
    assert declared, "no FAULT_* constants found in the bridge source"

    # FAULT_INTERNAL is the one legitimate exception. It names xmlrpc's own
    # code 1 so the comment above it has something to point at, and is never
    # raised by the bridge because xmlrpc raises it for any uncaught
    # exception. Asserted to be exactly 1 so it cannot drift into colliding
    # with a real fault.
    assert "FAULT_INTERNAL" in declared
    internal = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "FAULT_INTERNAL"
    )
    assert internal == 1, (
        f"FAULT_INTERNAL is {internal}; it must mirror xmlrpc's own code, "
        f"which is 1. Anything else would shadow a declared fault."
    )

    # Load contexts are the declarations themselves, so only reads count as
    # a use. A constant read anywhere else is being passed to _fail or raised.
    dead = sorted(
        name
        for name in declared - {"FAULT_INTERNAL"}
        if not any(
            isinstance(node, ast.Name)
            and node.id == name
            and not isinstance(node.ctx, ast.Store)
            for node in ast.walk(tree)
        )
    )
    assert not dead, (
        f"{dead} are declared and mapped to a client class but never raised. "
        f"Either raise them on the path that needs them, or delete the "
        f"constant, its client class, and its _FAULT_MAP entry - a fault kind "
        f"that cannot occur is a promise the surface cannot keep."
    )


def test_every_fault_kind_carries_a_recovery_hint() -> None:
    """A typed error with no hint fails CAP-6 just as surely as a raised one.

    Checked across the whole table rather than a hand-picked few, so a newly
    added kind cannot arrive without one.
    """
    missing = [
        cls.__name__ for cls in bridge_module._FAULT_MAP.values() if not cls("x").hint
    ]
    assert not missing, f"these error kinds carry no recovery hint: {missing}"


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
    assert proxy.calls == [("add_primitive", ("Doc", "Part::Box", "Box", dimensions))]


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
        if stripped.startswith("import FreeCAD") or stripped.startswith("from FreeCAD"):
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
