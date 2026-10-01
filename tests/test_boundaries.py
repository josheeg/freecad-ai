"""Tests that cross process, protocol and packaging boundaries.

Every bug found while building this server lived at a seam the main suite
never crossed: worker threads, MCP content conversion, subprocess pipes,
source tree versus installed package. These tests deliberately sit on those
seams.

The unit and integration suites call the Bridge and `server.call_tool`
directly. That is fast and reliable, and it means none of them would have
caught a wiring problem between the process and a real MCP client.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from freecad_ai import __version__
from freecad_ai import server as server_module
from freecad_ai.bridge import stop

pytestmark = pytest.mark.integration

BOOT_TIMEOUT = 90.0


def build_wheel(outdir: Path) -> subprocess.CompletedProcess[str]:
    """Build a wheel with uv, the project's own build front end."""
    return subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(outdir)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )


def _reset_server_state() -> None:
    """Clear the module-level bridge cache so a test owns the process."""
    if server_module._process is not None:
        stop(server_module._process)
    server_module._process = None
    server_module._bridge = None
    server_module._restarted = False


@pytest.fixture
def owned_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A server whose FreeCAD process this test controls and can kill."""
    monkeypatch.setenv("FREECAD_AI_PORT", "19901")
    _reset_server_state()
    yield server_module
    _reset_server_state()


def test_survives_freecad_being_killed(owned_server: Any) -> None:
    """Kill FreeCAD mid-session; the next call must recover and say so.

    The mocked restart tests prove the notice is formatted. This proves the
    recovery actually happens: the dead process is replaced, a new FreeCAD
    binds the port, and the model is told its documents are gone rather than
    being handed an empty kernel with no warning.
    """
    first = owned_server.get_bridge()
    assert first.new_document("before_crash")["created"] is True
    first.add_primitive("before_crash", "Part::Box", "B", {"Length": 10.0})
    assert "before_crash" in first.list_documents()

    process = owned_server._process
    assert process is not None
    killed = False

    def kill_now() -> None:
        nonlocal killed
        time.sleep(0.5)
        process.kill()
        process.wait(timeout=10)
        killed = True

    killer = threading.Thread(target=kill_now)
    killer.start()
    killer.join(timeout=20)
    assert killed, "failed to kill FreeCAD"

    # The next tool call must transparently reconnect, and announce it.
    result = owned_server.list_documents()
    notice = result.get("notice", "")
    assert "restarted" in notice, result
    assert "gone" in notice, result

    # And the replacement must be a genuinely fresh kernel.
    assert result["documents"] == [], result
    new_bridge = owned_server.get_bridge()
    assert new_bridge.instance_pid() != process.pid
    assert owned_server._restarted is False


def test_works_after_recovery(owned_server: Any) -> None:
    """The server must still be usable after a replacement, not wedged."""
    owned_server.get_bridge()
    owned_server._process.kill()
    owned_server._process.wait(timeout=10)

    assert owned_server.new_document("after")["created"] is True
    owned_server.add_primitive(
        "after", "Part::Box", "B", {"Length": 10.0, "Width": 10.0, "Height": 10.0}
    )
    assert owned_server.shape_summary("after", "B")["volume"] == pytest.approx(
        1000.0, rel=1e-3
    )


def test_notice_is_reported_only_once(owned_server: Any) -> None:
    """One crash, one notice — not one per subsequent call."""
    owned_server.get_bridge()
    owned_server._process.kill()
    owned_server._process.wait(timeout=10)

    first = owned_server.list_documents()
    assert "notice" in first
    assert "notice" not in owned_server.list_documents()
    assert "notice" not in owned_server.list_documents()


class _StdioClient:
    """Minimal MCP stdio client: write a request, read one response line."""

    def __init__(self, port: int) -> None:
        # Each client gets its own port. Sharing the default would make a
        # second server hit PortInUse against the first one's still-running
        # FreeCAD — correct behaviour, wrong test setup.
        env = {**os.environ, "FREECAD_AI_PORT": str(port)}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "freecad_ai.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def read(self, timeout: float = 60.0) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if line.strip():
                return json.loads(line)
        raise AssertionError("no response within timeout")

    def initialize(self) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "boundary-test", "version": "1"},
                },
            }
        )
        return self.read()

    def call(self, name: str, arguments: dict[str, Any], ident: int = 2) -> Any:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": ident,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self.read()

    def close(self) -> None:
        """Shut the server down the way a real client does: close stdin.

        terminate() on Windows is TerminateProcess, which skips atexit — so
        the FreeCAD the server started would be orphaned, still holding its
        port. Closing stdin ends the session normally and the atexit handler
        runs.
        """
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


_PORT_SEQ = itertools.count(19910)


@pytest.fixture
def stdio_client() -> Any:
    # A distinct port per client. hash() is salted per process, so it can
    # collide across runs; a counter cannot.
    client = _StdioClient(next(_PORT_SEQ))
    try:
        yield client
    finally:
        client.close()


def test_real_stdio_handshake(stdio_client: _StdioClient) -> None:
    """The server must speak real JSON-RPC over stdio, not just in-process.

    Every other test calls `server.call_tool`, an internal API. If framing,
    the initialize handshake or content serialisation were wrong, the whole
    suite would pass and no MCP client could talk to the server.
    """
    response = stdio_client.initialize()
    assert response["id"] == 1
    result = response["result"]
    assert result["serverInfo"]["name"] == "freecad-ai"
    assert result["serverInfo"]["version"] == __version__
    assert "instructions" in result

    stdio_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_tool_call_over_the_wire_returns_whole_payloads(
    stdio_client: _StdioClient,
) -> None:
    """A multi-element result must survive the wire, not arrive truncated."""
    stdio_client.initialize()
    stdio_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    made = stdio_client.call("new_document", {"name": "wire"})
    assert made["result"]["content"][0]["text"].strip().startswith("{")

    for index in range(3):
        stdio_client.call(
            "add_primitive",
            {
                "document": "wire",
                "kind": "Part::Box",
                "object_name": f"W{index}",
                "dimensions": {"Length": 10.0},
            },
            ident=10 + index,
        )

    listed = stdio_client.call("list_objects", {"document": "wire"}, ident=50)
    payload = json.loads(listed["result"]["content"][0]["text"])
    assert isinstance(payload, dict), payload
    assert len(payload["objects"]) == 3, payload
    assert {obj["name"] for obj in payload["objects"]} == {"W0", "W1", "W2"}


def test_tool_error_arrives_as_data_over_the_wire(
    stdio_client: _StdioClient,
) -> None:
    """A failure must reach the client as readable content, not a dropped
    request or a bare protocol error."""
    stdio_client.initialize()
    stdio_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = stdio_client.call("list_objects", {"document": "ghost"}, ident=60)
    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["kind"] == "DocumentNotFound"
    assert payload["hint"]


def test_wheel_contains_a_working_package(tmp_path: Path) -> None:
    """The wheel must ship the FreeCAD bridge, or an installed server cannot
    start FreeCAD at all.

    The source tree always has the script next to the package, so the whole
    suite passes while an installed copy is broken. This asserts the file is
    actually in the built artifact.
    """
    build = build_wheel(tmp_path)
    if build.returncode != 0:
        pytest.skip(f"build unavailable: {build.stderr.strip()[:200]}")

    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, f"no wheel produced: {build.stdout[-400:]}"
    names = zipfile.ZipFile(wheels[0]).namelist()
    assert "freecad_ai/_freecad_bridge.py" in names, names
    for expected in ("__init__.py", "bridge.py", "server.py", "_version.py"):
        assert f"freecad_ai/{expected}" in names, expected
    assert "bridge/freecad_bridge.py" not in names, (
        "the bridge must live inside the package, not beside it"
    )


def test_installed_package_resolves_its_bridge_script(tmp_path: Path) -> None:
    """Laid out as an install, the script path must still resolve.

    This is the regression that shipped: BRIDGE_SCRIPT walked out of the
    package to find a project root, which exists in the source tree and does
    not exist in site-packages.

    The wheel is extracted rather than pip-installed into a venv. The thing
    under test is the file arrangement, and extracting reaches it without
    pulling the MCP dependency tree over the network.
    """
    build = build_wheel(tmp_path)
    if build.returncode != 0:
        pytest.skip(f"build unavailable: {build.stderr.strip()[:200]}")
    wheels = list(tmp_path.glob("*.whl"))
    if not wheels:
        pytest.skip("no wheel produced")

    site = tmp_path / "site-packages"
    site.mkdir()
    with zipfile.ZipFile(wheels[0]) as archive:
        archive.extractall(site)
    assert (site / "freecad_ai" / "_freecad_bridge.py").is_file()

    # Import from the extracted layout, not the repo's own copy.
    probe = (
        "import freecad_ai.bridge as b, pathlib;"
        "print(pathlib.Path(b.__file__).parent);"
        "print(b.BRIDGE_SCRIPT.is_file())"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(site)},
    )
    assert result.returncode == 0, result.stderr[-500:]
    location, resolved = result.stdout.strip().splitlines()[-2:]
    assert str(site) in location, location
    assert resolved == "True", f"bridge script not found from {location}"
