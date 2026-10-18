"""verknode as an MCP server: the tools of agentapi.AgentAPI for MCP clients (Claude Code, Claude Desktop,
IDEs, agent frameworks).

Headless, over stdio - workflows are .vkf files under a folder, loaded, run and saved here:

    python -m jupyterlab_verknode.mcp_server --root ~/notebooks

    claude mcp add verknode -- python -m jupyterlab_verknode.mcp_server --root ~/notebooks

Live, over HTTP inside the Jupyter server (handlers.MCPHandler) - the agent's edits appear in the open
workflow editors, and runs happen on the server:

    claude mcp add --transport http verknode http://localhost:8888/verknode/mcp \\
        --header "Authorization: token $JUPYTER_TOKEN"

MCPServer.handle answers one JSON-RPC message (the protocol: initialize, ping, tools/list, tools/call);
the transports are stdio here and HTTP (JSON responses) in handlers.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .agentapi import INSTRUCTIONS, AgentAPI, LocalWorkspace

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _version() -> str:
    try:
        from ._version import __version__

        return __version__
    except ImportError:  # pragma: no cover
        return "0"


class MCPServer:
    def __init__(self, api: AgentAPI, name: str = "verknode", instructions: str = INSTRUCTIONS):
        self.api, self.name, self.instructions = api, name, instructions

    async def handle(self, msg: dict) -> dict | None:
        """The response to a JSON-RPC message (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:  # (a notification: initialized, cancelled, ...)
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                return _result(mid, {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "title": "verknode workflows", "version": _version()},
                    "instructions": self.instructions,
                })
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": self.api.tools()})
            if method == "tools/call":
                out = await self.api.call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": out["text"]}], "isError": bool(out["error"])}
                if isinstance(out.get("data"), dict):
                    result["structuredContent"] = json.loads(json.dumps(out["data"], default=str))
                return _result(mid, result)
            if method in ("resources/list", "prompts/list"):
                return _result(mid, {method.split("/")[0]: []})
            return _error(mid, -32601, f"Method not found: {method}")
        except Exception as e:  # noqa: BLE001 - the client gets the error
            return _error(mid, -32603, f"{type(e).__name__}: {e}")


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve_stdio(server: MCPServer):
    """Newline-delimited JSON-RPC on stdin / stdout. stdout is kept for the protocol: anything else
    printed (by nodes, libraries) goes to stderr."""
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    loop = asyncio.new_event_loop()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "Parse error")
        else:
            if isinstance(msg, list):  # (a batch)
                replies = [r for r in (loop.run_until_complete(server.handle(m)) for m in msg) if r is not None]
                reply = replies or None
            else:
                reply = loop.run_until_complete(server.handle(msg))
        if reply is not None:
            out.write(json.dumps(reply, default=str) + "\n")
            out.flush()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m jupyterlab_verknode.mcp_server",
                                description="verknode workflows for MCP clients (stdio, headless)")
    p.add_argument("--root", default=".", help="the folder with the workflows (default: the current folder)")
    p.add_argument("--no-autosave", action="store_true", help="save only with save_workflow")
    a = p.parse_args(argv)
    serve_stdio(MCPServer(AgentAPI(LocalWorkspace(a.root, autosave=not a.no_autosave))))


if __name__ == "__main__":
    main()
