"""A minimal MCP client (JSON-RPC 2.0) for the examples: over stdio (a server it starts) or HTTP.

    c = MCPClient.stdio(sys.executable, "-m", "jupyterlab_verknode.mcp_server", "--root", "notebooks")
    c = MCPClient.http("http://localhost:8888/verknode/mcp", token=os.environ["JUPYTER_TOKEN"])
    c.tools                      # [{"name", "description", "inputSchema", "annotations"}, ...]
    c.call("add_node", {...})    # {"content": [{"type": "text", "text": ...}], "isError", "structuredContent"}
"""
import json
import subprocess
import urllib.request


class MCPClient:
    def __init__(self, send):
        self._send = send
        self.next_id = 0
        info = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "verknode-example", "version": "1"}})
        self.notify("notifications/initialized")
        self.instructions = info.get("instructions", "")
        self.server = info.get("serverInfo", {})
        self.tools = self.request("tools/list")["tools"]

    @classmethod
    def stdio(cls, *command):
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline()) if "id" in msg else None

        client = cls(send)
        client.proc = proc
        return client

    @classmethod
    def http(cls, url, token=None):
        def send(msg):
            req = urllib.request.Request(url, data=json.dumps(msg).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json, text/event-stream")
            req.add_header("MCP-Protocol-Version", "2025-06-18")
            if token:
                req.add_header("Authorization", f"token {token}")
            with urllib.request.urlopen(req, timeout=900) as r:
                body = r.read()
            return json.loads(body) if body else None

        return cls(send)

    def request(self, method, params=None):
        self.next_id += 1
        reply = self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        if "error" in reply:
            raise RuntimeError(reply["error"]["message"])
        return reply["result"]

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        proc = getattr(self, "proc", None)
        if proc is not None:
            proc.stdin.close()
            proc.wait(10)
