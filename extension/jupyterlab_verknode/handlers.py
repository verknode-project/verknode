"""REST API for the workflow editor: /verknode/api/..."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import posixpath
import time
import uuid

import tornado
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import ensure_async, url_path_join

from . import nodes  # noqa: F401 - registers the built-in nodes
from .engine.node import NODE_REGISTRY
from .engine.persistence import load_workflow, save_workflow
from .agentapi import AgentAPI, LocalWorkspace, ToolError
from .commands import Recorder, open_hint, to_python
from .mcp_server import MCPServer
from .export import ExportError, export_bundle, format_report, prepare
from .standalone import to_standalone
from .engine.workflow import Workflow, WorkflowError
from .api_ops import OPS, RESULTS, _export_options, _script_path  # noqa: F401


class SessionManager:
    """Open workflows, shared per file path.

    Every editor showing a file and every Python client attached to it
    (``verknode.open(path)``) use the same session, so edits made in one place
    appear in the others. Sessions are reference counted.
    """

    def __init__(self, contents_manager, root_dir: str):
        self.cm = contents_manager
        self.root_dir = root_dir
        self.sessions: dict[str, Workflow] = {}
        self.by_path: dict[str, str] = {}
        self.refs: dict[str, int] = {}
        self.disk_mtime: dict[str, object] = {}
        self.last_access: dict[str, float] = {}

    IDLE_TIMEOUT = 30 * 60  # unused, unmodified sessions are dropped after this

    def get(self, sid: str) -> Workflow:
        if sid not in self.sessions:
            raise tornado.web.HTTPError(404, f"Unknown workflow session: {sid}")
        self.last_access[sid] = time.time()
        return self.sessions[sid]

    def _gc(self):
        now = time.time()
        for sid, wf in list(self.sessions.items()):
            if not wf.dirty and not wf.executing and now - self.last_access.get(sid, now) > self.IDLE_TIMEOUT:
                self.refs[sid] = 1
                self.close(sid)

    async def _mtime(self, path: str):
        try:
            model = await ensure_async(self.cm.get(path, content=False, type="file"))
            return model.get("last_modified")
        except tornado.web.HTTPError:
            return None

    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(path.lstrip("/"))

    def _abs(self, path: str) -> str:
        return os.path.join(self.root_dir, path.lstrip("/"))

    def _base_dir(self, path: str) -> str:
        return os.path.join(self.root_dir, posixpath.dirname(path.lstrip("/")))

    def _set_path(self, sid: str, wf: Workflow, path: str):
        path = self._norm(path)
        for p, other in list(self.by_path.items()):
            if other == sid:
                del self.by_path[p]
        self.by_path[path] = sid
        wf.base_dir = self._base_dir(path)
        wf.path = self._abs(path)
        wf.rel_path = path

    async def _read(self, path: str, create: bool = False) -> Workflow:
        try:
            model = await ensure_async(self.cm.get(path, content=True, type="file", format="base64"))
        except tornado.web.HTTPError as e:
            if e.status_code == 404 and create:
                wf = Workflow()
                wf.base_dir = self._base_dir(path)
                return wf
            raise
        data = base64.b64decode(model.get("content") or "")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, load_workflow, data, self._base_dir(path))
        except ValueError as e:
            raise tornado.web.HTTPError(400, str(e)) from None

    async def open(self, path: str, create: bool = False) -> str:
        self._gc()
        key = self._norm(path)
        sid = self.by_path.get(key)
        if sid is not None:
            wf = self.sessions[sid]
            # a session left behind (e.g. a closed browser tab) must not hide
            # changes made to the file since: reload unless it has unsaved edits
            if not wf.dirty and not wf.executing and await self._mtime(key) != self.disk_mtime.get(sid):
                if await self._mtime(key) is None and not create:
                    raise tornado.web.HTTPError(404, f"No such file: {key}")
                await self.reload(sid, key, create=True)
        if sid is None:
            sid = uuid.uuid4().hex
            wf = await self._read(key, create)
            self.sessions[sid] = wf
            self._set_path(sid, wf, key)
            self.disk_mtime[sid] = await self._mtime(key)
            wf._log("PY", f"# {open_hint(key)}")
        self.refs[sid] = self.refs.get(sid, 0) + 1
        self.last_access[sid] = time.time()
        return sid

    async def reload(self, sid: str, path: str, create: bool = False):
        old = self.get(sid)
        old.cancel()
        wf = await self._read(path, create)
        self.disk_mtime[sid] = await self._mtime(path)
        wf.version = old.version + 1  # keep watchers moving forward
        self.sessions[sid] = wf
        self._set_path(sid, wf, path)

    async def save(self, sid: str, path: str):
        wf = self.get(sid)
        if wf.executing and not wf.waiting_for_input:
            raise tornado.web.HTTPError(409, "Cannot save while the workflow is executing")
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, save_workflow, wf)
        await ensure_async(self.cm.save(
            {"type": "file", "format": "base64", "content": base64.b64encode(data).decode("ascii")}, path))
        self._set_path(sid, wf, path)
        self.disk_mtime[sid] = await self._mtime(path)
        wf.dirty = False
        wf.version += 1

    def close(self, sid: str):
        self.refs[sid] = self.refs.get(sid, 1) - 1
        if self.refs[sid] > 0:
            return
        self.refs.pop(sid, None)
        self.disk_mtime.pop(sid, None)
        self.last_access.pop(sid, None)
        wf = self.sessions.pop(sid, None)
        for p, other in list(self.by_path.items()):
            if other == sid:
                del self.by_path[p]
        if wf is not None:
            wf.cancel()


class BaseHandler(APIHandler):
    @property
    def manager(self) -> SessionManager:
        return self.settings["verknode_manager"]

    def body(self) -> dict:
        return json.loads(self.request.body or b"{}")

    def reply(self, obj):
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(obj, default=str))

    def view(self, sid: str, wf: Workflow):
        since = int(self.get_argument("log_since", "0"))
        self.reply(dict(wf.view(log_since=since), session=sid))


class NodesHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        self.reply([cls.describe() for cls in NODE_REGISTRY.values()])


class SessionsHandler(BaseHandler):
    @tornado.web.authenticated
    async def post(self):
        body = self.body()
        sid = await self.manager.open(body["path"], create=bool(body.get("create")))
        self.view(sid, self.manager.get(sid))


class SessionHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, sid):
        self.view(sid, self.manager.get(sid))

    @tornado.web.authenticated
    def delete(self, sid):
        self.manager.close(sid)
        self.set_status(204)
        self.finish()


class SaveHandler(BaseHandler):
    @tornado.web.authenticated
    async def post(self, sid):
        await self.manager.save(sid, self.body()["path"])
        self.view(sid, self.manager.get(sid))


class ReloadHandler(BaseHandler):
    @tornado.web.authenticated
    async def post(self, sid):
        await self.manager.reload(sid, self.body()["path"])
        self.view(sid, self.manager.get(sid))


class OpsHandler(BaseHandler):
    @tornado.web.authenticated
    def post(self, sid):
        wf = self.manager.get(sid)
        args = self.body()
        op = args.get("op")
        if op == "set_path":
            self.manager._set_path(sid, wf, args["path"])
            return self.view(sid, wf)
        if op not in OPS:
            raise tornado.web.HTTPError(400, f"Unknown operation: {op}")
        # ("batch": an edit of a script's batch - see Workflow.batch; a batch nobody ends expires)
        if args.get("batch"):
            wf.touch_batch()
        else:
            wf.expire_batch()
        recorder = Recorder(wf, op, args)
        try:
            result = OPS[op](wf, args)
        except (WorkflowError, KeyError, ValueError, TypeError) as e:
            raise tornado.web.HTTPError(400, str(e)) from None
        # show the equivalent verknode command in the console (learning aid)
        try:
            for line in recorder.lines({"id": result} if op in ("add_node", "add_group", "collapse") else result):
                wf._log("PY", line)
        except Exception as e:  # noqa: BLE001 - never fail an edit because of the transcript
            self.log.warning("verknode: could not describe %s: %s", op, e)
        if args.get("quiet"):
            # (scripts in a batch: no full view after every edit; they ask for one when needed)
            out = {"session": sid, "version": wf.version}
        else:
            out = dict(wf.view(log_since=int(args.get("log_since", 0))), session=sid)
        if op in RESULTS:
            out["result"] = {RESULTS[op]: result}
        self.reply(out)


class SessionNodesHandler(BaseHandler):
    """Node repository of a workflow: built-in nodes plus its custom nodes."""

    @tornado.web.authenticated
    def get(self, sid):
        self.reply(self.manager.get(sid).node_types())


class WaitHandler(BaseHandler):
    """Long poll: returns the view as soon as the workflow version differs from ?version."""

    @tornado.web.authenticated
    async def get(self, sid):
        version = int(self.get_argument("version", "-1"))
        timeout = min(float(self.get_argument("timeout", "20")), 60.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        wf = self.manager.get(sid)
        checked = 0.0
        editor = self.get_argument("editor", None) is not None  # an open workflow editor
        while wf.version == version and loop.time() < deadline:
            if editor:
                wf.editor_seen = time.time()
            if loop.time() - checked > 1.0:
                # files nodes read (e.g. a notebook saved in JupyterLab): ports, resets
                checked = loop.time()
                wf.check_files()
                if wf.version != version:
                    break
            await asyncio.sleep(0.15)
            wf = self.manager.get(sid)
        self.view(sid, wf)


class FrontendReplyHandler(BaseHandler):
    """An editor claims / answers a request of the workflow (Workflow.frontend_request)."""

    @tornado.web.authenticated
    def post(self, sid, rid):
        a = self.body()
        self.reply({"ok": self.manager.get(sid).frontend_reply(rid, a["action"], a.get("data"))})


class PythonHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, sid):
        wf = self.manager.get(sid)
        rel = getattr(wf, "rel_path", None)
        if self.get_argument("kind", "live") == "standalone":  # (runs without Jupyter)
            opts = _export_options(json.loads(self.get_argument("options", "{}")))
            try:
                e, notes, problems = prepare(wf, opts["group"], opts["nodes"], "include", opts["headless"])
            except ExportError as err:
                raise tornado.web.HTTPError(400, str(err)) from None
            self.reply({"script": to_standalone(e, rel, notes=notes), "path": _script_path(rel),
                        "notes": notes, "problems": problems})
            return
        self.reply({"script": to_python(wf, rel), "hint": open_hint(rel or "workflow.vkf")})


class PythonSaveHandler(BaseHandler):
    """The standalone script, saved next to the workflow (an existing file only with overwrite)."""

    @tornado.web.authenticated
    async def post(self, sid):
        wf = self.manager.get(sid)
        rel = getattr(wf, "rel_path", None)
        if not rel:
            raise tornado.web.HTTPError(400, "Save the workflow first")
        path, body = _script_path(rel), self.body()
        try:
            await ensure_async(self.manager.cm.get(path, content=False))
            exists = True
        except tornado.web.HTTPError:
            exists = False
        if exists and not body.get("overwrite"):
            self.reply({"saved": False, "exists": True, "path": path})
            return
        opts = _export_options(body)
        try:
            e, notes, _ = prepare(wf, opts["group"], opts["nodes"], "include", opts["headless"])
        except ExportError as err:
            raise tornado.web.HTTPError(400, str(err)) from None
        await ensure_async(self.manager.cm.save({"type": "file", "format": "text",
                                                 "content": to_standalone(e, rel, notes=notes)}, path))
        self.reply({"saved": True, "path": path})


class PythonExportHandler(BaseHandler):
    """A self-contained export folder next to the workflow (export.export_bundle)."""

    @tornado.web.authenticated
    async def post(self, sid):
        wf = self.manager.get(sid)
        rel = getattr(wf, "rel_path", None)
        if not rel:
            raise tornado.web.HTTPError(400, "Save the workflow first")
        body, opts = self.body(), _export_options(self.body())
        folder = posixpath.splitext(rel)[0] + "_export"
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(None, lambda: export_bundle(
                wf, os.path.join(self.manager.root_dir, folder), posixpath.splitext(posixpath.basename(rel))[0],
                opts["group"], opts["nodes"], opts["upstream"], opts["headless"], bool(body.get("data", True)),
                bool(body.get("docker")), bool(body.get("api")), True, bool(body.get("overwrite"))))
        except ExportError as err:
            if "exists" in str(err):
                self.reply({"exported": False, "exists": True, "folder": folder})
                return
            raise tornado.web.HTTPError(400, str(err)) from None
        report["folder"] = folder
        self.reply({"exported": True, "folder": folder, "text": format_report(report), "report": report})


class TableHandler(BaseHandler):
    """Full output table as parquet (used by the Python client: node.table())."""

    @tornado.web.authenticated
    def get(self, sid, nid, port):
        import io

        import polars as pl

        wf = self.manager.get(sid)
        try:
            nc = wf.node(int(nid))
        except WorkflowError as e:
            raise tornado.web.HTTPError(404, str(e)) from None
        obj = nc.outputs[int(port)] if nc.outputs and int(port) < len(nc.outputs) else None
        if not isinstance(obj, pl.DataFrame):
            raise tornado.web.HTTPError(400, f"Port {port} of node {nid} has no table (execute the node first)")
        buf = io.BytesIO()
        obj.write_parquet(buf)
        self.set_header("Content-Type", "application/vnd.apache.parquet")
        self.finish(buf.getvalue())


class DialogHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, sid, nid):
        try:
            self.reply(self.manager.get(sid).dialog(int(nid)))
        except WorkflowError as e:
            raise tornado.web.HTTPError(400, str(e)) from None


class ViewHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, sid, nid):
        try:
            self.reply(self.manager.get(sid).node_view(int(nid)))
        except WorkflowError as e:
            raise tornado.web.HTTPError(400, str(e)) from None


class PortHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, sid, nid, port):
        offset = int(self.get_argument("offset", "0"))
        limit = min(int(self.get_argument("limit", "100")), 5000)
        structures = [c for c in self.get_argument("structures", "").split("\t") if c]
        try:
            self.reply(self.manager.get(sid).port_data(int(nid), int(port), offset, limit, structures))
        except WorkflowError as e:
            raise tornado.web.HTTPError(400, str(e)) from None


KETCHER_DIR = os.environ.get("VERKNODE_KETCHER_DIR", "/opt/ketcher")


class DialogActionHandler(BaseHandler):
    """A node's dialog action (e.g. create its notebook) with the unsaved dialog settings."""

    @tornado.web.authenticated
    def post(self, sid, nid):
        a = self.body()
        try:
            result = self.manager.get(sid).dialog_action(int(nid), a["name"], a.get("settings"), a.get("args"))
        except (WorkflowError, ValueError, OSError) as e:
            raise tornado.web.HTTPError(400, str(e)) from None
        self.reply(result)


class DialogDataHandler(BaseHandler):
    """Node-specific dialog data for unsaved dialog settings (e.g. another input column)."""

    @tornado.web.authenticated
    def post(self, sid, nid):
        self.reply(self.manager.get(sid).dialog_extra(int(nid), self.body().get("settings")))


class ChemStructureHandler(BaseHandler):
    """Structure of SMILES / MolBlock text for the Molecule Sketcher (RDKit)."""

    @tornado.web.authenticated
    def post(self):
        from .chem import structure

        a = self.body()
        self.reply(structure(a.get("text"), bool(a.get("clean")), int(a.get("width", 200)), int(a.get("height", 140))))


class KetcherFileHandler(tornado.web.StaticFileHandler):
    """The Ketcher app: public static files like JupyterLab's own assets (no user data), served
    from the same origin so the sketcher dialog can use the frame's window.ketcher API."""

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "public, max-age=86400" if "/static/" in f"/{path}" else "no-cache")


VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")


class VendorFileHandler(tornado.web.StaticFileHandler):
    """Viewer libraries for the 3D structure views (NGL, Mol*): public static files (no user data)
    like JupyterLab's own assets - the views run in sandboxed frames, which load them by URL."""

    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "public, max-age=86400")


class DisplayHandler(BaseHandler):
    """A canvas widget's display when it is too big to go with every view (Workflow.view)."""

    @tornado.web.authenticated
    def get(self, sid, nid):
        try:
            self.reply(self.manager.get(sid).widget_display(int(nid)))
        except WorkflowError as e:
            raise tornado.web.HTTPError(404, str(e)) from None


class KetcherStatusHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        self.reply({"available": os.path.isfile(os.path.join(KETCHER_DIR, "index.html"))})


class LiveWorkspace(LocalWorkspace):
    """The agent API on the Jupyter server's open workflows (agentapi): what an agent changes, the open
    editors show at once (and the other way round). Saved by save_workflow, as the editor saves."""

    live = True

    def __init__(self, manager: SessionManager):
        super().__init__(manager.root_dir, autosave=False)
        self.manager = manager
        self.url_prefix = ""  # (where JupyterLab is, for links: set from the request)

    async def open(self, path: str, create: bool = False) -> Workflow:
        key = self.rel(path)
        sid = self.manager.by_path.get(key)
        if sid is None:
            try:
                sid = await self.manager.open(key, create=create)  # (held while the server runs)
            except tornado.web.HTTPError as e:
                if e.status_code == 404:
                    raise ToolError(f"There is no workflow {key} (create_workflow makes one)") from None
                raise
        wf = self.manager.get(sid)
        wf.rel_path = key
        return wf

    async def save(self, path: str, to: str | None = None) -> str:
        key = self.rel(path)
        await self.open(key)
        dest = self.rel(to) if to else key
        await self.manager.save(self.manager.by_path[key], dest)
        return dest

    def link(self, key: str) -> str | None:
        return f"{self.url_prefix}lab/tree/{key}" if self.url_prefix else None


class MCPHandler(BaseHandler):
    """MCP over HTTP (Streamable HTTP, JSON responses): POST a JSON-RPC message, get the answer."""

    @tornado.web.authenticated
    async def post(self):
        server: MCPServer = self.settings["verknode_mcp"]
        server.api.ws.url_prefix = f"{self.request.protocol}://{self.request.host}{self.base_url}"
        try:
            msg = json.loads(self.request.body or b"null")
        except ValueError:
            self.reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return
        if isinstance(msg, list):
            replies = [r for r in [await server.handle(m) for m in msg] if r is not None]
            reply = replies or None
        else:
            reply = await server.handle(msg)
        if reply is None:  # (notifications only)
            self.set_status(202)
            self.finish()
            return
        self.reply(reply)

    @tornado.web.authenticated
    def get(self):  # (no server-sent events: answers come with the POST)
        self.set_status(405)
        self.set_header("Allow", "POST")
        self.finish()


def setup_handlers(server_app):
    web_app = server_app.web_app
    cm = web_app.settings["contents_manager"]
    manager = web_app.settings["verknode_manager"] = SessionManager(cm, os.path.abspath(cm.root_dir))
    web_app.settings["verknode_mcp"] = MCPServer(AgentAPI(LiveWorkspace(manager)))
    base = url_path_join(web_app.settings["base_url"], "verknode", "api")
    sid = r"(?P<sid>[0-9a-f]+)"
    handlers = [
        (url_path_join(base, "nodes"), NodesHandler),
        (url_path_join(base, "sessions"), SessionsHandler),
        (url_path_join(base, "sessions", sid), SessionHandler),
        (url_path_join(base, "sessions", sid, "save"), SaveHandler),
        (url_path_join(base, "sessions", sid, "reload"), ReloadHandler),
        (url_path_join(base, "sessions", sid, "ops"), OpsHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "dialog"), DialogHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "ports", r"(?P<port>\d+)"), PortHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "view"), ViewHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "display"), DisplayHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "ports", r"(?P<port>\d+)", "table"),
         TableHandler),
        (url_path_join(base, "sessions", sid, "wait"), WaitHandler),
        (url_path_join(base, "sessions", sid, "frontend", r"(?P<rid>[0-9a-f]+)"), FrontendReplyHandler),
        (url_path_join(base, "sessions", sid, "nodes"), SessionNodesHandler),
        (url_path_join(base, "sessions", sid, "python"), PythonHandler),
        (url_path_join(base, "sessions", sid, "python", "save"), PythonSaveHandler),
        (url_path_join(base, "sessions", sid, "python", "export"), PythonExportHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "dialog_data"), DialogDataHandler),
        (url_path_join(base, "sessions", sid, "nodes", r"(?P<nid>\d+)", "action"), DialogActionHandler),
        (url_path_join(base, "chem", "structure"), ChemStructureHandler),
        (url_path_join(base, "chem", "ketcher"), KetcherStatusHandler),
        # MCP for agents (agentapi, live on the open workflows)
        (url_path_join(web_app.settings["base_url"], "verknode", "mcp"), MCPHandler),
        # Ketcher editor app (embedded by the Molecule Sketcher dialog)
        (url_path_join(web_app.settings["base_url"], "verknode", "ketcher", "(.*)"), KetcherFileHandler,
         {"path": KETCHER_DIR, "default_filename": "index.html"}),
        (url_path_join(web_app.settings["base_url"], "verknode", "vendor", "(.*)"), VendorFileHandler,
         {"path": VENDOR_DIR}),
    ]
    web_app.add_handlers(".*$", handlers)
