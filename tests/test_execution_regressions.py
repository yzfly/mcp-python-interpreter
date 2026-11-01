"""Focused regressions for saved snippets and MCP SDK 1/2 startup."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from mcp_python_interpreter import server


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(server, "WORKING_DIR", work)
    monkeypatch.setattr(server, "ALLOW_SYSTEM_ACCESS", False)
    monkeypatch.setattr(server, "_sessions", {})
    return work


def run(code="print('executed')", **kwargs):
    return asyncio.run(server.run_python_code(code, **kwargs))


@pytest.mark.parametrize("name", ["../outside", "../new/sub/file.txt", "absolute", "symlink-dir", "symlink-file"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_save_rejects_escape_before_execution(workspace, name, overwrite):
    outside = workspace.parent / "outside.py"
    outside.write_text("original")
    if name == "absolute":
        name = str(outside)
    elif name == "symlink-dir":
        (workspace / "link").symlink_to(workspace.parent, target_is_directory=True)
        name = "link/outside.py"
    elif name == "symlink-file":
        (workspace / "alias.py").symlink_to(outside)
        name = "alias.txt"  # Check containment after .py normalization.
    result = run(save_as=name, overwrite=overwrite)
    assert "Access denied" in result
    assert outside.read_text() == "original"
    assert not (workspace.parent / "new").exists()
    assert server._sessions == {}


def test_save_normalizes_suffix_and_runs(workspace):
    code = "print(6 * 7)"
    assert "42" in run(code, save_as="scripts/example.txt")
    assert (workspace / "scripts/example.py").read_text() == code


def test_existing_file_requires_explicit_overwrite(workspace):
    target = workspace / "saved.py"
    target.write_text("original")
    assert "overwrite=True" in run(save_as="saved")
    assert target.read_text() == "original"
    assert server._sessions == {}
    assert "executed" in run(save_as="saved", overwrite=True)
    assert target.read_text() == "print('executed')"


def test_explicit_system_access_still_allows_outside_save(workspace, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_SYSTEM_ACCESS", True)
    assert "executed" in run(save_as="../outside.py")
    assert (workspace.parent / "outside.py").is_file()


def test_invalid_mode_does_not_create_file(workspace):
    assert "Unknown execution mode" in run(execution_mode="invalid", save_as="unused.py")
    assert not (workspace / "unused.py").exists()


def test_system_exit_restores_process_and_keeps_session(workspace):
    cwd, stdout, stderr = Path.cwd(), sys.stdout, sys.stderr
    result = run("value = 41\nraise SystemExit(7)")
    assert "SystemExit: 7" in result and "--- Error ---" in result
    assert (Path.cwd(), sys.stdout, sys.stderr) == (cwd, stdout, stderr)
    assert "42" in run("value + 1")


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize("enabled", ["true", "false"])
def test_network_options_work_on_installed_sdk(monkeypatch, transport, enabled):
    entry = importlib.import_module("mcp_python_interpreter.main")
    for key in list(os.environ):
        if key.startswith("MCP_"):
            monkeypatch.delenv(key)
    for key, value in {
        "MCP_TRANSPORT": transport,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": "8765",
        "MCP_JSON_RESPONSE": enabled,
        "MCP_STATELESS_HTTP": enabled,
        "MCP_ALLOWED_HOSTS": "127.0.0.1:*, example.test:*",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(entry.mcp, "run", lambda **kwargs: calls.append(kwargs))
    entry.main()
    assert calls[0]["transport"] == transport
    options = calls[0] if server.MCP_SDK_V2 else vars(entry.mcp.settings)
    assert options["host"] == "127.0.0.1" and options["port"] == 8765
    assert options["transport_security"].allowed_hosts == ["127.0.0.1:*", "example.test:*"]
    if transport == "streamable-http":
        assert options["json_response"] is (enabled == "true")
        assert options["stateless_http"] is (enabled == "true")


def test_stdio_handshake_and_recovery_after_exit(workspace):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_python_interpreter.main", "--dir", str(workspace)],
            env={**os.environ, "MCP_TRANSPORT": "stdio"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool = next(t for t in tools.tools if t.name == "run_python_code")
                assert tool.model_dump(by_alias=True)["inputSchema"]["properties"]["overwrite"]["default"] is False
                result = await session.call_tool("run_python_code", {"code": "raise SystemExit(9)"})
                assert "SystemExit: 9" in str(result)
                result = await session.call_tool("run_python_code", {"code": "print(42)"})
                assert "42" in str(result) and not result.model_dump(by_alias=True).get("isError", False)
                denied = await session.call_tool("run_python_code", {
                    "code": "print('should not run')", "save_as": "../outside.py",
                })
                assert "Access denied" in str(denied)
                assert not (workspace.parent / "outside.py").exists()
    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_streamable_http_initializes_and_calls_tool(workspace):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "MCP_TRANSPORT": "streamable-http", "MCP_HOST": "127.0.0.1",
           "MCP_PORT": str(port), "MCP_JSON_RESPONSE": "true", "MCP_STATELESS_HTTP": "true"}
    for key in ("MCP_ALLOWED_HOSTS", "MCP_DISABLE_DNS_REBINDING_PROTECTION"):
        env.pop(key, None)
    proc = subprocess.Popen([sys.executable, "-m", "mcp_python_interpreter.main", "--dir", str(workspace)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    def request(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body,
              headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(proc.stderr.read())
            try:
                result = request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "regression-test", "version": "1"}})
                break
            except urllib.error.URLError:
                time.sleep(0.05)
        else:
            pytest.fail("HTTP server did not start")
        assert result["result"]["serverInfo"]["name"] == "python-interpreter"
        result = request("tools/call", {"name": "run_python_code", "arguments": {"code": "print(42)"}})
        assert "42" in str(result["result"]) and not result["result"].get("isError", False)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
