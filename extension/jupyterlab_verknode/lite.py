"""verknode in the browser: the workflow engine on JupyterLite's Pyodide kernel, without a server.

The editor's REST calls (/verknode/api/...) go to a Bridge in a kernel of their own instead of the Jupyter
server (src/lite.ts runs `bridge.handle(method, endpoint, body)` there). It does what handlers.py does,
with the files of JupyterLite's drive (/drive in the kernel: the browser's storage).

The browser has no threads: a run executes before the call returns (the editor shows the result when it
is done), thread pools run one task after another, and HTTP goes through the browser (pyodide_http),
so servers must allow the page's origin (CORS).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import posixpath
import re
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import parse_qs

from .engine import defaults

WASM = sys.platform == "emscripten"


# ---------------------------------------------------------------------------------------------------
# no threads: run what would be a thread at once, pools one task after another
# ---------------------------------------------------------------------------------------------------
class InlineThread:
    """A thread that runs when started, before start() returns. One started while another runs (e.g. the
    Workflow Script edits after a run) waits until that one is done."""

    _running = 0
    _pending: list = []

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
        self.name, self.daemon, self._alive = name or "inline", daemon, False

    def start(self):
        if InlineThread._running:
            InlineThread._pending.append(self)
            return
        self._run_now()
        while InlineThread._pending:
            InlineThread._pending.pop(0)._run_now()

    def _run_now(self):
        self._alive = True
        InlineThread._running += 1
        try:
            if self._target is not None:
                self._target(*self._args, **self._kwargs)
        finally:
            InlineThread._running -= 1
            self._alive = False

    def run(self):
        self.start()

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive


_DONE = InlineThread()


class InlineExecutor(concurrent.futures.Executor):
    """A thread pool that runs each task when it is submitted."""

    def __init__(self, max_workers=None, *a, **kw):
        pass

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def patch_threads():
    """verknode's threads inline (only its own modules: libraries such as scikit-learn's joblib know that
    the browser has no threads); thread pools - which cannot start in the browser - one task at a time."""
    import types

    from .engine import parallel, workflow

    inline = types.ModuleType("threading")
    inline.__dict__.update(threading.__dict__)
    inline.Thread = InlineThread
    inline.current_thread = lambda: _DONE  # (a run's "thread": done when its after-run work starts)
    for mod in (workflow, parallel):
        mod.threading = inline
    concurrent.futures.ThreadPoolExecutor = InlineExecutor  # type: ignore[misc,assignment]

    # a node that waits for the canvas (a chat) would wait forever: the run would never return
    from .engine.node import ExecContext, InvalidSettingsError

    def no_wait(self):
        raise InvalidSettingsError("Chats and other nodes that wait for messages from the canvas need a Jupyter "
                                   "server: in the browser a run cannot wait (it must finish before the editor "
                                   "hears back)")

    ExecContext.wait_for_input = no_wait


def patch_polars():
    """polars reads and writes files by path with its own (Rust) file access, which JupyterLite's drive
    does not support ("No such device"): in the browser, file paths go through Python's files."""
    import io

    import polars as pl

    def is_path(x):
        return isinstance(x, (str, os.PathLike)) and not str(x).startswith(("http://", "https://"))

    def reader(fn):
        def wrapped(source, *a, **kw):
            if is_path(source):
                with open(source, "rb") as f:
                    source = io.BytesIO(f.read())
            return fn(source, *a, **kw)
        wrapped._vkn_patched = True
        return wrapped

    def scanner(read):
        def wrapped(source, *a, **kw):
            kw = {k: v for k, v in kw.items() if k not in ("low_memory", "cache", "rechunk", "glob")}
            return read(source, *a, **kw).lazy()
        return wrapped

    def writer(fn):
        def wrapped(self, file=None, *a, **kw):
            if is_path(file):
                buf = io.BytesIO()
                out = fn(self, buf, *a, **kw)
                with open(file, "wb") as f:
                    f.write(buf.getvalue())
                return out
            return fn(self, file, *a, **kw)
        wrapped._vkn_patched = True
        return wrapped

    for name in ("read_csv", "read_json", "read_ndjson", "read_ipc", "read_excel", "read_avro"):
        if hasattr(pl, name) and not getattr(getattr(pl, name), "_vkn_patched", False):
            setattr(pl, name, reader(getattr(pl, name)))
    pl.scan_csv = scanner(pl.read_csv)
    pl.scan_ndjson = scanner(pl.read_ndjson)
    for name in ("write_csv", "write_json", "write_ndjson", "write_ipc", "write_avro"):
        fn = getattr(pl.DataFrame, name, None)
        if fn is not None and not getattr(fn, "_vkn_patched", False):
            setattr(pl.DataFrame, name, writer(fn))


def patch_http():
    try:
        import pyodide_http

        pyodide_http.patch_all()  # (urllib and requests through the browser's XMLHttpRequest)
    except ImportError:
        pass


# ---------------------------------------------------------------------------------------------------
# chemistry: RDKit's WebAssembly build (MinimalLib) behind RDKit's Python API (lite_rdkit)
# ---------------------------------------------------------------------------------------------------
LITE_RDKIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lite_rdkit")
# nodes that need what the WebAssembly build has not: shown as not available here, with the reason
NOT_IN_BROWSER = {
    "verknode.chem.Conformers": "3D coordinates (embedding, force fields) are not available in the browser version",
}


async def load_rdkit(url: str) -> bool:
    """Load RDKit's WebAssembly build (RDKit_minimal.js / .wasm at url, the site's static/rdkit/) into this
    worker as verknodeRDKit. False (and the chemistry nodes stay unavailable) if it is not there."""
    import js

    if getattr(js, "verknodeRDKit", None) is not None:
        return True
    from pyodide.ffi import create_proxy, to_js

    try:
        if hasattr(js, "importScripts"):
            js.importScripts(url + "RDKit_minimal.js")
        else:  # (a module worker)
            from pyodide.code import run_js
            from pyodide.http import pyfetch

            run_js(await (await pyfetch(url + "RDKit_minimal.js")).string())
        locate = create_proxy(lambda name, *_: url + name)
        module = await js.initRDKitModule(to_js({"locateFile": locate}, dict_converter=js.Object.fromEntries))
        setattr(js, "verknodeRDKit", module)
        return True
    except Exception as e:  # noqa: BLE001 - verknode runs without chemistry
        print(f"verknode: RDKit (WebAssembly) did not load: {e}")
        return False


def install_rdkit() -> bool:
    """RDKit's Python API (lite_rdkit) on the WebAssembly build, if it is loaded and the real RDKit is not
    there; nodes it cannot run are marked (NOT_IN_BROWSER)."""
    import importlib.util

    try:
        import js

        if getattr(js, "verknodeRDKit", None) is None:
            return False
    except ImportError:
        return False
    if importlib.util.find_spec("rdkit") is not None:
        return False  # (a real RDKit)
    sys.path.insert(0, LITE_RDKIT)
    importlib.invalidate_caches()
    from .engine import requirements as req

    requirements, missing = req.requirements, req.missing

    def browser_requirements(cls):
        out = requirements(cls)
        if getattr(cls, "factory", None) in NOT_IN_BROWSER:
            out = out + [{"package": "RDKit 3D (not in the browser)", "module": "rdkit_3d", "optional": False,
                          "installed": False}]
        return out

    def browser_missing(cls):
        why = NOT_IN_BROWSER.get(getattr(cls, "factory", None))
        return f"{cls.name}: {why}" if why else missing(cls)

    req.requirements, req.missing = browser_requirements, browser_missing
    return True


# ---------------------------------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------------------------------
class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


SID = r"(?P<sid>[0-9a-f]+)"
NID = r"(?P<nid>\d+)"


class Bridge:
    """The verknode REST API on the files under root (JupyterLite's drive)."""

    def __init__(self, root: str = "/drive"):
        if WASM:
            patch_threads()
            patch_polars()
            patch_http()
            install_rdkit()  # (before the nodes: the chemistry nodes import rdkit)
        from . import nodes  # noqa: F401 - registers the built-in nodes

        self.root = root
        # the user's setup (engine/home.py): .verknode in the browser's files - llm.env, defaults.json, nodes/
        os.environ.setdefault("VERKNODE_HOME", os.path.join(root, ".verknode"))
        # (the LLM settings: also an llm.env at the top of the files)
        if not os.path.isfile(os.path.join(root, ".verknode", "llm.env")) and os.path.isfile(os.path.join(root, "llm.env")):
            os.environ.setdefault("VERKNODE_LLM_CONFIG", os.path.join(root, "llm.env"))
        self.sessions: dict = {}
        self.by_path: dict[str, str] = {}
        self.refs: dict[str, int] = {}
        self.mtimes: dict[str, float | None] = {}
        self.routes = [
            ("GET", r"nodes", self.nodes),
            ("GET", r"defaults", lambda q, b: defaults.describe()),
            ("POST", r"defaults", lambda q, b: defaults.save(b.get("values") or {})),
            # (packages are installed on a Jupyter server; here: the ones the site was built with)
            ("GET", r"packages", lambda q, b: {"server": False, "api": __import__(
                "jupyterlab_verknode.engine.packages", fromlist=["API_VERSION"]).API_VERSION}),
            ("POST", r"sessions", self.open),
            ("GET", rf"sessions/{SID}", self.view),
            ("DELETE", rf"sessions/{SID}", self.close),
            ("POST", rf"sessions/{SID}/save", self.save),
            ("POST", rf"sessions/{SID}/reload", self.reload),
            ("POST", rf"sessions/{SID}/ops", self.ops),
            ("GET", rf"sessions/{SID}/nodes", self.session_nodes),
            ("GET", rf"sessions/{SID}/wait", self.wait),
            ("POST", rf"sessions/{SID}/frontend/(?P<rid>[0-9a-f]+)", self.frontend),
            ("GET", rf"sessions/{SID}/nodes/{NID}/dialog", self.dialog),
            ("POST", rf"sessions/{SID}/nodes/{NID}/dialog_data", self.dialog_data),
            ("POST", rf"sessions/{SID}/nodes/{NID}/action", self.dialog_action),
            ("GET", rf"sessions/{SID}/nodes/{NID}/view", self.node_view),
            ("GET", rf"sessions/{SID}/nodes/{NID}/display", self.display),
            ("GET", rf"sessions/{SID}/nodes/{NID}/ports/(?P<port>\d+)", self.port),
            ("GET", rf"sessions/{SID}/python", self.python),
            ("POST", rf"sessions/{SID}/python/save", self.python_save),
            ("POST", r"chem/structure", self.chem_structure),
            ("GET", r"chem/ketcher", lambda q, b: {"available": False}),
        ]

    # -- the entry point --------------------------------------------------------------------------
    def handle(self, method: str, endpoint: str, body: str = "") -> dict:
        """{"status", "data"} of a REST call (endpoint: the part after /verknode/api/, with ?query)."""
        path, _, query = endpoint.partition("?")
        q = {k: v[-1] for k, v in parse_qs(query).items()}
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {}
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path.strip("/"))
            if m == method.upper() and match:
                try:
                    out = fn(q, data, **match.groupdict())
                    return {"status": 200 if out is not None else 204, "data": out}
                except HTTPError as e:
                    return {"status": e.status, "data": {"message": str(e)}}
                except BaseException as e:  # noqa: BLE001 - the editor shows the message (polars' Rust panics too)
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    from .engine.workflow import WorkflowError

                    status = 400 if isinstance(e, (WorkflowError, KeyError, ValueError, TypeError)) else 500
                    return {"status": status, "data": {"message": str(e) or type(e).__name__,
                                                       "traceback": traceback.format_exc()[-2000:]}}
        return {"status": 404, "data": {"message": f"verknode in the browser has no {method} {path}"}}

    # -- files ------------------------------------------------------------------------------------
    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(str(path or "").lstrip("/"))

    def _abs(self, path: str) -> str:
        return os.path.join(self.root, self._norm(path))

    def _mtime(self, path: str):
        try:
            return os.path.getmtime(self._abs(path))
        except OSError:
            return None

    def _set_path(self, sid: str, wf, path: str):
        path = self._norm(path)
        for p, other in list(self.by_path.items()):
            if other == sid:
                del self.by_path[p]
        self.by_path[path] = sid
        wf.base_dir = os.path.dirname(self._abs(path))
        wf.path = self._abs(path)
        wf.rel_path = path

    def _read(self, path: str, create: bool):
        from .engine.persistence import load_workflow
        from .engine.workflow import Workflow

        full = self._abs(path)
        if not os.path.isfile(full):
            if create:
                wf = Workflow()
                wf.base_dir = os.path.dirname(full)
                return wf
            raise HTTPError(404, f"No such file: {path}")
        with open(full, "rb") as f:
            data = f.read()
        return load_workflow(data, os.path.dirname(full))

    def get(self, sid: str):
        if sid not in self.sessions:
            raise HTTPError(404, f"Unknown workflow session: {sid}")
        return self.sessions[sid]

    def _view(self, sid, q):
        return dict(self.get(sid).view(log_since=int(q.get("log_since", 0) or 0)), session=sid)

    # -- routes -----------------------------------------------------------------------------------
    def nodes(self, q, b):
        from .engine.node import NODE_REGISTRY

        return [cls.describe() for cls in NODE_REGISTRY.values()]

    def open(self, q, b):
        key = self._norm(b["path"])
        sid = self.by_path.get(key)
        if sid is not None:
            wf = self.sessions[sid]
            if not wf.dirty and not wf.executing and self._mtime(key) != self.mtimes.get(sid):
                self._reload(sid, key, create=True)
        else:
            sid = uuid.uuid4().hex
            wf = self._read(key, bool(b.get("create")))
            self.sessions[sid] = wf
            self._set_path(sid, wf, key)
            self.mtimes[sid] = self._mtime(key)
        self.refs[sid] = self.refs.get(sid, 0) + 1
        return self._view(sid, q)

    def view(self, q, b, sid):
        return self._view(sid, q)

    def close(self, q, b, sid):
        self.refs[sid] = self.refs.get(sid, 1) - 1
        if self.refs[sid] > 0:
            return None
        wf = self.sessions.pop(sid, None)
        for p, other in list(self.by_path.items()):
            if other == sid:
                del self.by_path[p]
        if wf is not None:
            wf.cancel()
        return None

    def save(self, q, b, sid):
        from .engine.persistence import save_workflow

        wf = self.get(sid)
        path = self._norm(b["path"])
        full = self._abs(path)
        data = save_workflow(wf)  # (first: a failure leaves the file as it was)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        self._set_path(sid, wf, path)
        self.mtimes[sid] = self._mtime(path)
        wf.dirty = False
        wf.version += 1
        return self._view(sid, q)

    def _reload(self, sid, path, create=False):
        old = self.get(sid)
        old.cancel()
        wf = self._read(path, create)
        wf.version = old.version + 1
        self.sessions[sid] = wf
        self._set_path(sid, wf, path)
        self.mtimes[sid] = self._mtime(path)

    def reload(self, q, b, sid):
        self._reload(sid, self._norm(b["path"]))
        return self._view(sid, q)

    def ops(self, q, b, sid):
        from .api_ops import OPS, RESULTS
        from .commands import Recorder

        wf = self.get(sid)
        op = b.get("op")
        if op == "set_path":
            self._set_path(sid, wf, b["path"])
            return self._view(sid, b)
        if op not in OPS:
            raise HTTPError(400, f"Unknown operation: {op}")
        if b.get("batch"):
            wf.touch_batch()
        else:
            wf.expire_batch()
        recorder = Recorder(wf, op, b)
        result = OPS[op](wf, b)
        try:
            for line in recorder.lines({"id": result} if op in ("add_node", "add_group", "collapse") else result):
                wf._log("PY", line)
        except Exception:  # noqa: BLE001 - the transcript is a nicety
            pass
        out = {"session": sid, "version": wf.version} if b.get("quiet") else dict(
            wf.view(log_since=int(b.get("log_since", 0) or 0)), session=sid)
        if op in RESULTS:
            out["result"] = {RESULTS[op]: result}
        return out

    def session_nodes(self, q, b, sid):
        return self.get(sid).node_types()

    def wait(self, q, b, sid):
        # (no long poll in one kernel: the editor asks again - src/lite.ts waits between the calls)
        return self._view(sid, q)

    def frontend(self, q, b, sid, rid):
        return {"ok": self.get(sid).frontend_reply(rid, b["action"], b.get("data"))}

    def dialog(self, q, b, sid, nid):
        return self.get(sid).dialog(int(nid))

    def dialog_data(self, q, b, sid, nid):
        return self.get(sid).dialog_extra(int(nid), b.get("settings"))

    def dialog_action(self, q, b, sid, nid):
        return self.get(sid).dialog_action(int(nid), b["name"], b.get("settings"), b.get("args"))

    def node_view(self, q, b, sid, nid):
        return self.get(sid).node_view(int(nid))

    def display(self, q, b, sid, nid):
        return self.get(sid).widget_display(int(nid))

    def port(self, q, b, sid, nid, port):
        structures = [c for c in (q.get("structures") or "").split("\t") if c]
        return self.get(sid).port_data(int(nid), int(port), int(q.get("offset", 0) or 0),
                                       min(int(q.get("limit", 100) or 100), 5000), structures)

    def python(self, q, b, sid):
        from .api_ops import _export_options, _script_path
        from .commands import open_hint, to_python
        from .export import prepare
        from .standalone import to_standalone

        wf = self.get(sid)
        rel = getattr(wf, "rel_path", None)
        if q.get("kind") == "standalone":
            opts = _export_options(json.loads(q.get("options") or "{}"))
            e, notes, problems = prepare(wf, opts["group"], opts["nodes"], "include", opts["headless"])
            return {"script": to_standalone(e, rel, notes=notes), "path": _script_path(rel), "notes": notes, "problems": problems}
        return {"script": to_python(wf, rel), "hint": open_hint(rel or "workflow.vkf")}

    def python_save(self, q, b, sid):
        from .api_ops import _export_options, _script_path
        from .export import prepare
        from .standalone import to_standalone

        wf = self.get(sid)
        rel = getattr(wf, "rel_path", None)
        if not rel:
            raise HTTPError(400, "Save the workflow first")
        path = _script_path(rel)
        if os.path.exists(self._abs(path)) and not b.get("overwrite"):
            return {"saved": False, "exists": True, "path": path}
        opts = _export_options(b)
        e, notes, _ = prepare(wf, opts["group"], opts["nodes"], "include", opts["headless"])
        with open(self._abs(path), "w", encoding="utf-8") as f:
            f.write(to_standalone(e, rel, notes=notes))
        return {"saved": True, "path": path}

    def chem_structure(self, q, b):
        from .chem import structure

        return structure(b.get("text"), bool(b.get("clean")), int(b.get("width", 200)), int(b.get("height", 140)))


_bridge: Bridge | None = None


def bridge(root: str = "/drive") -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge(root)
    return _bridge


# packages Pyodide has, by the pip names the nodes say they need (engine/requirements.py)
PYODIDE_PACKAGES = {"scikit-learn", "scipy", "pandas", "matplotlib", "statsmodels", "lxml", "openpyxl", "xlsxwriter",
                    "jsonpath-ng", "requests", "networkx", "sympy", "pyarrow", "beautifulsoup4", "html5lib"}


def _needed(b: Bridge, endpoint: str, body: str) -> list[str]:
    """The packages the nodes of an execute need and this kernel has not loaded yet."""
    m = re.fullmatch(rf"sessions/{SID}/ops", endpoint.split("?")[0].strip("/"))
    if not m:
        return []
    try:
        args = json.loads(body or "{}")
    except ValueError:
        return []
    if args.get("op") != "execute" or m.group("sid") not in b.sessions:
        return []
    from .engine.requirements import requirements

    wf = b.sessions[m.group("sid")]
    ids = list(wf.nodes) if args.get("ids") is None else wf.expand_ids(args["ids"])
    plan, _ = wf._plan(ids)
    need = set()
    for nid in plan:
        for r in requirements(type(wf.nodes[nid].model)):
            if not r["installed"] and not r["optional"] and r["package"] in PYODIDE_PACKAGES:
                need.add(r["package"])
    return sorted(need)


MISSING = re.compile(r"No module named '([\w.]+)'")


def _missing_after(b: Bridge, endpoint: str) -> tuple[list[str], list[int]]:
    """After an execute: the packages its failed nodes did not find, and those nodes."""
    from .engine.requirements import PACKAGES

    m = re.fullmatch(rf"sessions/{SID}/ops", endpoint.split("?")[0].strip("/"))
    wf = b.sessions.get(m.group("sid")) if m else None
    if wf is None:
        return [], []
    pkgs, nodes = set(), []
    for nid, nc in wf.nodes.items():
        hit = MISSING.search(str(nc.message[1]) if nc.message else "")
        if hit:
            top = hit.group(1).split(".")[0]
            pkgs.add(PACKAGES.get(top, top))
            nodes.append(nid)
    return sorted(pkgs), nodes


async def acall(method: str, endpoint: str, body: str = ""):
    """What src/lite.ts runs in the kernel. Before an execute it loads the packages its nodes need (Pyodide
    loads them from its CDN); a call that fails for a module the analysis did not see coming (a node, or
    opening a workflow whose saved models are scikit-learn's) is done once more after loading it."""
    import importlib

    import piplite

    from .engine.requirements import PACKAGES

    t0 = time.time()
    b = bridge()
    need = _needed(b, endpoint, body)
    if need:
        await piplite.install(need, keep_going=True)
        importlib.invalidate_caches()
    out = b.handle(method, endpoint, body)
    is_execute = endpoint.split("?")[0].endswith("/ops") and '"execute"' in (body or "")
    if is_execute:
        more, failed = _missing_after(b, endpoint)
        if more:
            try:
                await piplite.install(more, keep_going=True)
                importlib.invalidate_caches()
                need = sorted(set(need) | set(more))
                wf = b.sessions[re.match(r"sessions/([0-9a-f]+)/ops", endpoint.strip("/")).group(1)]
                wf.reset(failed)
                out = b.handle(method, endpoint, body)
            except Exception:  # noqa: BLE001 - (not a package Pyodide has: the nodes keep their error)
                pass
    elif out["status"] >= 400:
        hit = MISSING.search(str((out.get("data") or {}).get("message", "")))
        if hit:
            top = hit.group(1).split(".")[0]
            try:
                await piplite.install([PACKAGES.get(top, top)], keep_going=True)
                importlib.invalidate_caches()
                need = sorted(set(need) | {PACKAGES.get(top, top)})
                out = b.handle(method, endpoint, body)
            except Exception:  # noqa: BLE001
                pass
    _reply(out, t0, need)


def _reply(out: dict, t0: float, loaded=()):
    from IPython.display import display

    if loaded:
        out["loaded"] = list(loaded)
    out["ms"] = round((time.time() - t0) * 1000)
    display({"application/json": json.loads(json.dumps(out, default=str))}, raw=True)


def call(method: str, endpoint: str, body: str = ""):
    """A REST call without loading packages (acall loads them)."""
    t0 = time.time()
    _reply(bridge().handle(method, endpoint, body), t0)
