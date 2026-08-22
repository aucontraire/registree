"""End-to-end stdio handshake test.

Spawns the real ``registree`` console script as a subprocess and speaks
JSON-RPC over its stdin/stdout — the same transport an MCP client uses.
This proves the wiring (entry point, transport, tool registration, and one
real tool round-trip against a project); tool logic is covered by unit
tests.

stdin must stay open until all responses arrive: the server treats EOF as
shutdown and cancels in-flight requests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any

TIMEOUT_SECONDS = 30

EXPECTED_TOOLS = {
    "server_info",
    "get_signature",
    "search_classes",
    "list_duplicates",
    "verify_snippet",
    "get_usages",
}


def _server_command(root: Path) -> list[str]:
    script = Path(sys.executable).parent / "registree"
    assert script.exists(), f"console script not installed at {script}"
    return [str(script), "serve", "--root", str(root)]


def _handshake_messages() -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "handshake-test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "server_info", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "get_signature",
                "arguments": {"class_name": "Gadget"},
            },
        },
    ]


def _collect(
    stdout: IO[str], expected: set[int], out: dict[int, dict[str, Any]]
) -> None:
    while expected - out.keys():
        line = stdout.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") is not None:
            out[msg["id"]] = msg


def _structured(call: dict[str, Any]) -> dict[str, Any]:
    assert "result" in call, call
    assert call["result"].get("isError") is not True
    payload: dict[str, Any] = call["result"].get("structuredContent") or json.loads(
        call["result"]["content"][0]["text"]
    )
    return payload


def _run_handshake(root: Path) -> dict[int, dict[str, Any]]:
    proc = subprocess.Popen(
        _server_command(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    responses: dict[int, dict[str, Any]] = {}
    try:
        for message in _handshake_messages():
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

        reader = threading.Thread(
            target=_collect,
            args=(proc.stdout, {1, 2, 3, 4}, responses),
            daemon=True,
        )
        reader.start()
        reader.join(TIMEOUT_SECONDS)
        assert (
            not reader.is_alive()
        ), f"timed out after {TIMEOUT_SECONDS}s; got ids {sorted(responses)}"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return responses


def test_stdio_handshake_end_to_end(sample_project: Path) -> None:
    responses = _run_handshake(sample_project)
    assert sorted(responses) == [1, 2, 3, 4]

    init = responses[1]
    assert "result" in init, init
    assert init["result"]["serverInfo"]["name"] == "registree"

    tools = responses[2]["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert EXPECTED_TOOLS <= tool_names

    info = _structured(responses[3])
    assert info["status"] == "ok"
    assert info["registry_classes"] > 0

    signature = _structured(responses[4])
    assert signature["found"] is True
    assert signature["ambiguous"] is False
    assert signature["definitions"][0]["required_arguments"] == ["flag", "name"]
