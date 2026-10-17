"""Worker processes for parallel chunk loops ("Run chunks in: Processes").

Threads share the server's Python, so plain Python code in them takes turns
(the GIL). Here a chunk runs in a worker process instead: the copies of the
loop body are rebuilt there as a small workflow, fed with the chunk and the
other inputs (pickled), run with the same engine, and their results come
back. The workers are started once (spawned: the server has threads, so no
fork) and kept for later runs; they report what runs while it runs, so the
temporary metanode shows it as with threads.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
import uuid

from .node import CanceledExecutionError
from .workflow import WorkflowError

_lock = threading.Lock()
_pool = None  # (Pool, size, status queue)
_listeners: dict = {}  # run token -> fn(node id, runtime, progress, started, executed)


def _get_pool(size: int):
    """The worker pool, grown to `size` processes if smaller (the old one finishes its tasks)."""
    global _pool
    with _lock:
        if _pool is None or _pool[1] < size:
            if _pool is not None:
                _pool[0].close()
            ctx = mp.get_context("spawn")
            queue = ctx.Queue()
            pool = ctx.Pool(size, initializer=_init_worker, initargs=(queue,))
            pool.verknode_dead = False
            _pool = (pool, size, queue)
            threading.Thread(target=_dispatch, args=(queue,), daemon=True, name="verknode-chunk-status").start()
        return _pool[0]


class ChunkTransferError(WorkflowError):
    """A chunk (or its results) could not be copied to (from) a worker process."""


def running() -> bool:
    """Are the worker processes started?"""
    return _pool is not None


def stop_workers():
    """Kill the workers (a canceled run: their tasks cannot be stopped otherwise)."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool[0].verknode_dead = True
            _pool[0].terminate()
            _pool[2].put(None)
            _pool = None


def _dispatch(queue):
    while True:
        try:
            item = queue.get()
        except (EOFError, OSError):
            return
        if item is None:
            return
        fn = _listeners.get(item[0])
        if fn is not None:
            fn(*item[1:])


def run_chunk(payload: dict, size: int, on_status, cancel: threading.Event) -> dict:
    """Run one chunk in a worker (blocks; raises CanceledExecutionError when `cancel` is set)."""
    pool = _get_pool(size)
    token = uuid.uuid4().hex
    pid = []

    def listen(*item):
        if item[0] == "pid":
            pid.append(item[1])
        else:
            on_status(*item)
    _listeners[token] = listen
    try:
        result = pool.apply_async(_run_in_worker, (token, payload))
        while True:
            try:
                return result.get(timeout=0.2)
            except mp.TimeoutError:
                if cancel.is_set():
                    stop_workers()
                    raise CanceledExecutionError("Execution canceled") from None
                if pool.verknode_dead:
                    raise WorkflowError("The worker processes were stopped") from None
                if pid and not _alive(pid[0]):
                    # (the pool starts a new worker, but this chunk's task is lost)
                    raise WorkflowError("The worker process running this chunk stopped (out of memory?)") from None
    except (CanceledExecutionError, WorkflowError):
        raise
    except Exception as e:  # noqa: BLE001 - e.g. a chunk or a result that cannot be pickled
        raise ChunkTransferError(f"Worker process: {type(e).__name__}: {e}") from e
    finally:
        _listeners.pop(token, None)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][:1] not in ("Z", "X")
    except FileNotFoundError:
        return False
    except OSError:
        return True  # (no /proc: assume it runs)


# -- in the worker process ------------------------------------------------------
_queue = None
_custom = None


def _init_worker(queue):
    global _queue
    _queue = queue
    import jupyterlab_verknode.nodes  # noqa: F401 - registers the built-in nodes


_STATE = ("outputs", "out_stack", "message", "exec_time", "rows", "view", "display", "inactive", "traceback")


def _run_in_worker(token: str, payload: dict) -> dict:
    """Build the chunk's nodes as a workflow, run them, return their state and log."""
    global _custom
    from .custom import CustomNodes
    from .metanode import boundary_class, boundary_factory
    from .workflow import Connection, NodeContainer, Workflow

    wf = Workflow()
    wf.base_dir, wf.path = payload["base_dir"], payload["path"]
    folders = payload["node_paths"]
    if folders:
        if _custom is None:
            _custom = CustomNodes()
        if _custom.changed(folders):
            from .node import NODE_REGISTRY

            _custom.load(folders, NODE_REGISTRY)
        wf.custom = _custom
    body = []
    for n in payload["nodes"]:
        nc = NodeContainer(n["id"], n["factory"], model=wf._new_model(n["factory"]))
        nc.settings, nc.var_settings, nc.annotation = n["settings"], n["var_settings"], n["annotation"]
        wf.nodes[n["id"]] = nc
        body.append(n["id"])
    for i, src in enumerate(payload["sources"]):
        factory = boundary_factory("in", src["type"])
        nc = NodeContainer(src["id"], factory, model=boundary_class(factory)())
        nc.outputs, nc.out_stack = src["outputs"], src["stack"]
        wf.nodes[src["id"]] = nc
    wf.connections = [Connection(*c) for c in payload["connections"]]
    _queue.put((token, "pid", os.getpid()))

    seen = {}

    def report():
        for nid in body:
            nc = wf.nodes[nid]
            state = (nc.runtime, nc.progress, nc.started, nc.outputs is not None)
            if seen.get(nid) != state:
                seen[nid] = state
                _queue.put((token, nid) + state)

    done = threading.Event()

    def watch():
        while not done.wait(0.25):
            report()

    threading.Thread(target=watch, daemon=True).start()
    error = None
    t0, c0, p0 = time.time(), time.thread_time(), time.process_time()
    try:
        for nid in body:
            wf.nodes[nid].runtime = "QUEUED"
        for nid in wf.topo_order(set(body)):
            ready = wf._inputs_ready(nid)
            if ready and not wf._execute_single(nid):
                error = wf.nodes[nid].message[1] if wf.nodes[nid].message else "failed"
                break
            wf.nodes[nid].runtime = None
            report()
    finally:
        done.set()
        for nid in body:
            wf.nodes[nid].runtime = None
        report()
    return {
        "nodes": {nid: {k: getattr(wf.nodes[nid], k) for k in _STATE} for nid in body},
        "log": [(e["level"], e["text"], e["node"]) for e in wf.log if e["level"] != "DEBUG"],
        "error": error,
        "cpu": time.thread_time() - c0,
        "wall": time.time() - t0,
        "process_cpu": time.process_time() - p0,
    }
