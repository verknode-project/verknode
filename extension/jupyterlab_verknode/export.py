"""Export a workflow to run anywhere: a standalone script, or a self-contained folder with as few
requirements as possible - optionally with a Dockerfile and an HTTP API server.

    python -m jupyterlab_verknode.export report.vkf --out report_export --headless --data --docker --api
    python -m jupyterlab_verknode.export report.vkf --group "Monthly totals" --snapshot --out totals_export

The folder ("bundle") holds the script and what it needs of verknode: the engine (pure Python), only the
node modules the workflow uses, and a requirements.txt of the packages it imports - measured by running
the folder in a fresh process, so a workflow of polars nodes needs nothing but polars.

prepare() works on a copy of the workflow:
- group / nodes: export a part of it - with the nodes it needs (upstream="include"), or with the data
  that comes in saved as Parquet files read in their place (upstream="snapshot");
- headless: drop what only the canvas uses - output widgets (their input goes on to the nodes after
  them), input widgets (they become workflow variables: --var parameters), views and other nodes no
  result needs - and report nodes that cannot run without the editor (chats).
"""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import polars as pl

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
VERKNODE_DIR = os.path.join(os.path.dirname(PACKAGE_DIR), "verknode")
PATH_KEYS = ("path", "file", "filename", "file_path", "folder", "directory")
RUNTIME = "vkn_runtime"  # (the folder of the bundled verknode packages in an export)
NOT_BUNDLED = {"handlers", "mcp_server", "agentapi", "export"}  # (server / authoring parts)


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------
# preparing the workflow: a part of it, headless
# ---------------------------------------------------------------------------------------------------
def _real(e):
    bounds = e._boundaries()
    return [n for n in e.nodes if n not in bounds]


def _results(e, ids) -> list[int]:
    """The last table of each branch: nodes with a table output that feeds no other node."""
    return [n for n in ids if not any(c.source_id == n for c in e.connections)
            and any(p.type == "table" for p in e.nodes[n].model.out_ports)]


def select(e, group: str | None = None, nodes=None) -> list[int]:
    if group:
        g = next((g for g in e.groups if g["text"].strip().lower() == group.strip().lower()), None)
        if g is None:
            raise ExportError(f"There is no group {group!r}; the groups are {[g['text'] for g in e.groups] or 'none'}")
        return [n for n in g["nodes"] if n in e.nodes]
    if nodes:
        out = []
        for ref in nodes:
            if isinstance(ref, int) or str(ref).lstrip("#").isdigit():
                nid = int(str(ref).lstrip("#"))
            else:
                hits = [n for n, nc in e.nodes.items() if nc.annotation == ref]
                if len(hits) != 1:
                    raise ExportError(f"{len(hits)} nodes are labelled {ref!r}: use ids")
                nid = hits[0]
            if nid not in e.nodes:
                raise ExportError(f"There is no node #{nid}")
            out.append(nid)
        return out
    return _real(e)


def prepare(wf, group: str | None = None, nodes=None, upstream: str = "include", headless: bool = False,
            data_dir: str | None = None):
    """A copy of the workflow ready for export: (copy, notes, problems)."""
    from .engine.widgets import InputWidget, OutputWidget
    from .nodes.agent import clone

    e = clone(wf)
    notes, problems = [], []
    part = group is not None or bool(nodes)
    chosen = set(select(e, group, nodes))
    if part:
        what = f"the group {group!r}" if group else f"{len(chosen)} node(s)"
        if upstream == "include":
            needed = set().union(*(e.ancestors(n) for n in chosen)) - chosen if chosen else set()
            keep = chosen | needed
            notes.append(f"exported {what}" + (f", with the {len(needed)} node(s) before it that it needs" if needed else ""))
        elif upstream == "snapshot":
            notes.append(f"exported {what}")
            _snapshot(e, chosen, data_dir, notes, problems)  # (adds the readers to chosen)
            keep = chosen
        else:
            raise ExportError("upstream is \"include\" or \"snapshot\"")
        _remove(e, [n for n in _real(e) if n not in keep and n in e.nodes])
    if headless:
        _headless(e, notes, problems, InputWidget, OutputWidget)
    for n in _real(e):
        if getattr(e.nodes[n].model, "needs_canvas", False):
            problems.append(f"{e.nodes[n].name} #{n} \"{e.nodes[n].annotation}\" waits for the canvas (a chat): "
                            "it cannot run without the editor" + ("" if headless else " (headless leaves it out if no result needs it)"))
    if not _results(e, _real(e)):
        problems.append("nothing to export: no node makes a table")
    return e, notes, problems


def _remove(e, ids):
    ids = [n for n in ids if n in e.nodes]
    if ids:
        e.remove_nodes(ids)


def _snapshot(e, chosen, data_dir, notes, problems):
    """The data coming into the part from outside: saved, and read by Parquet Readers in its place."""
    if data_dir is None:
        raise ExportError("upstream=\"snapshot\" saves data: export a folder (bundle)")
    incoming: dict[tuple, list] = {}
    for c in list(e.connections):
        if c.dest_id in chosen and c.source_id not in chosen:
            if c.dest_port == 0:
                problems.append(f"flow variables from #{c.source_id} into #{c.dest_id} are not kept (set them as "
                                "workflow variables)")
                continue
            incoming.setdefault((c.source_id, c.source_port), []).append((c.dest_id, c.dest_port))
    os.makedirs(data_dir, exist_ok=True)
    for (src, sp), dests in incoming.items():
        nc = e.nodes[src]
        if not nc.executed:
            e.execute([src], wait=True)
            e.wait()
        out = (nc.outputs or [None] * (sp + 1))[sp] if nc.executed else None
        if not isinstance(out, pl.DataFrame):
            problems.append(f"#{src} \"{nc.annotation}\" did not give a table to snapshot: {nc.message}")
            continue
        name = re.sub(r"[^\w.-]+", "_", f"{nc.annotation or nc.name}_{src}_{sp}").strip("_") + ".parquet"
        out.write_parquet(os.path.join(data_dir, name))
        x = min(e.nodes[d].x for d, _ in dests) - 160
        y = min(e.nodes[d].y for d, _ in dests)
        reader = e.add_node("verknode.io.ParquetReader", x, y)
        e.set_settings(reader, {"path": f"data/{name}"})
        e.set_annotation(reader, f"{nc.annotation or nc.name} (snapshot)")
        for d, dp in dests:
            e.connect(reader, 1, d, dp)
        chosen.add(reader)
        notes.append(f"the {out.height} rows from \"{nc.annotation or nc.name}\" are saved as data/{name}")


def _headless(e, notes, problems, InputWidget, OutputWidget):
    def consumers(n, port=None):
        return [c for c in e.connections if c.source_id == n and (port is None or c.source_port == port)]

    def source_of(n, port):
        return next(((c.source_id, c.source_port) for c in e.connections if c.dest_id == n and c.dest_port == port), None)

    names = {v.name: v for v in e.workflow_variables}
    added, kept = [], {}
    changed = True
    while changed:  # (a display or input kept for nodes after it can go once those went)
        changed = _headless_pass(e, notes, names, added, kept, consumers, source_of, InputWidget, OutputWidget)
    notes.extend(kept.values())
    if added:
        e.set_workflow_variables([v.to_json() for v in e.workflow_variables] + added)
    _drop_rest(e, notes)


def _headless_pass(e, notes, names, added, kept, consumers, source_of, InputWidget, OutputWidget) -> bool:
    changed = False
    for n in list(_real(e)):
        nc = e.nodes.get(n)
        if nc is None:
            continue
        m = nc.model
        if isinstance(m, OutputWidget):
            if consumers(n, 2):  # (its selection on the canvas feeds nodes)
                kept[n] = f"kept the display \"{nc.annotation}\": its canvas selection is used after it"
                continue
            kept.pop(n, None)
            changed = True
            src = source_of(n, 1)
            after = consumers(n, 1)
            _remove(e, [n])
            if src is not None:
                for c in after:
                    e.connect(src[0], src[1], c.dest_id, c.dest_port)
            notes.append(f"dropped the display \"{nc.annotation}\" ({nc.name})")
        elif isinstance(m, InputWidget) and m.variable_type and not consumers(n, 1):
            var, value = m.column_name(nc.settings), m.value_of(nc.settings)
            if var in names and (names[var] is None or names[var].value != value):
                kept[n] = f"kept the input \"{nc.annotation}\": the workflow variable {var} is set otherwise"
                continue
            changed = True
            added.append({"name": var, "type": m.variable_type, "value": value})
            names[var] = None
            _remove(e, [n])
            notes.append(f"the input \"{nc.annotation}\" is now the workflow variable {var} = {value!r}")
    return changed


def _drop_rest(e, notes):
    # nodes that wait for the canvas (chats): out, with what only they feed
    canvas = [n for n in _real(e) if getattr(e.nodes[n].model, "needs_canvas", False)]
    for n in canvas:
        if n not in e.nodes:
            continue
        after = e.descendants(n)
        notes.append(f"dropped \"{e.nodes[n].annotation}\" ({e.nodes[n].name}): it waits for the canvas"
                     + (f", with the {len(after)} node(s) after it" if after else ""))
        _remove(e, [n] + sorted(after))
    # what no result needs (views, charts, nodes left over)
    results = _results(e, _real(e))
    needed = set(results).union(*(e.ancestors(r) for r in results)) if results else set()
    dropped = [n for n in _real(e) if n not in needed]
    if dropped:
        names_ = ", ".join(sorted({e.nodes[n].name for n in dropped}))
        notes.append(f"dropped {len(dropped)} node(s) no result needs ({names_})")
        _remove(e, dropped)


# ---------------------------------------------------------------------------------------------------
# data files
# ---------------------------------------------------------------------------------------------------
def _is_path(field, value):
    key = field.key
    return isinstance(value, str) and value.strip() and "://" not in value and (
        getattr(field, "path", False) or key in PATH_KEYS or key.endswith(("_path", "_file")))


def copy_data(e, out_dir, notes, problems):
    """Files the nodes read: copied into data/ (their settings point there); files they write go to output/."""
    taken: dict[str, str] = {}
    for n in _real(e):
        nc = e.nodes[n]
        changed = {}
        for f in nc.model.fields:
            v = nc.settings.get(f.key)
            if not _is_path(f, v) or v.startswith(("data/", "output/")):
                continue
            src = v if os.path.isabs(v) else os.path.normpath(os.path.join(e.base_dir or ".", v))
            base = os.path.basename(src.rstrip("/")) or "data"
            if os.path.exists(src):
                size = sum(os.path.getsize(os.path.join(d, x)) for d, _, fs in os.walk(src) for x in fs) \
                    if os.path.isdir(src) else os.path.getsize(src)
                if size > 500e6:
                    problems.append(f"{v} is {size / 1e6:.0f} MB: not copied (give the bundle's data/ folder yourself)")
                    continue
                name = base
                i = 2
                while name in taken and taken[name] != src:
                    stem, ext = os.path.splitext(base)
                    name, i = f"{stem}_{i}{ext}", i + 1
                taken[name] = src
                dest = os.path.join(out_dir, "data", name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if not os.path.exists(dest):
                    (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dest)
                changed[f.key] = f"data/{name}"
            elif os.path.splitext(base)[1]:  # (a file it writes)
                changed[f.key] = f"output/{base}"
        if changed:
            e.set_settings(n, changed)
            notes.append(f"\"{nc.annotation}\": " + ", ".join(f"{k} -> {v}" for k, v in changed.items()))
    os.makedirs(os.path.join(out_dir, "output"), exist_ok=True)


# ---------------------------------------------------------------------------------------------------
# the runtime: only the modules the workflow needs
# ---------------------------------------------------------------------------------------------------
def _imports(path):
    """(package modules, top-level third-party imports) of a file - package modules also lazily imported."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    here = os.path.relpath(path, os.path.dirname(PACKAGE_DIR))[:-3].replace(os.sep, ".")
    pkg = here.rsplit(".", 1)[0] if not here.endswith("__init__") else here.rsplit(".", 1)[0]
    internal, external = set(), set()
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[:len(base) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
                mods = [mod] + [f"{mod}.{a.name}" for a in node.names]
            else:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            if m.split(".")[0] in ("jupyterlab_verknode", "verknode"):
                internal.add(m)
            elif node.col_offset == 0 and m.split(".")[0] not in sys.stdlib_module_names:
                external.add(m.split(".")[0])
    return internal, external


def _file_of(module):
    root = os.path.dirname(PACKAGE_DIR)
    base = os.path.join(root, *module.split("."))
    for p in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(p):
            return p
    return None


def runtime_files(e) -> tuple[list[str], set]:
    """The package files the workflow needs (relative to the source root) and their top-level imports."""
    start = {"jupyterlab_verknode.standalone", "jupyterlab_verknode.nodes", "verknode.node"}
    start |= {f"jupyterlab_verknode.engine.{f[:-3]}" for f in os.listdir(os.path.join(PACKAGE_DIR, "engine"))
              if f.endswith(".py")}
    for n in _real(e):
        mod = type(e.nodes[n].model).__module__
        if mod.startswith("jupyterlab_verknode."):
            start.add(mod)
    # (molecules: only for the chemistry nodes - else verknode.node goes without, and so without RDKit)
    chemistry = any(m.endswith((".chemistry", ".structures")) for m in start)
    seen, files, external = set() if chemistry else {"jupyterlab_verknode.chem"}, set(), set()
    todo = list(start)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        f = _file_of(m)
        if f is None or os.path.splitext(os.path.basename(f))[0] in NOT_BUNDLED:
            continue
        files.add(f)
        internal, ext = _imports(f)
        external |= ext
        todo += internal
        parts = m.split(".")
        for i in range(1, len(parts)):  # (the packages it is in)
            todo.append(".".join(parts[:i]))
    root = os.path.dirname(PACKAGE_DIR)
    return sorted(os.path.relpath(f, root) for f in files), external


def minimize(out_dir, script, modules, budget: float = 60) -> tuple[set, list]:
    """The modules the run cannot do without: each package the run imported is blocked in turn (optional
    imports - e.g. RDKit helpers of a Python Script - go). Returns (modules, notes)."""
    by_dist: dict[str, set] = {}
    for m, req in _distributions(modules).items():
        if req.split("==")[0].lower() != "polars":
            by_dist.setdefault(req, set()).add(m)
    if not by_dist:
        return set(modules), []
    every = sorted(set().union(*by_dist.values()))
    if check_bundle(out_dir, script, block=every)["ok"]:
        return set(modules) - set(every), [f"runs without {', '.join(sorted(by_dist))}: not required"]
    t0, untested = time.time(), []

    def needed_of(reqs: list) -> set:  # (group testing: halves that run without them are optional)
        mods = sorted(set().union(*(by_dist[r] for r in reqs)))
        if time.time() - t0 > budget:
            untested.extend(reqs)
            return set(mods)
        if check_bundle(out_dir, script, block=mods)["ok"]:
            return set()
        if len(reqs) == 1:
            return set(mods)
        half = len(reqs) // 2
        return needed_of(reqs[:half]) | needed_of(reqs[half:])

    needed = needed_of(sorted(by_dist))
    optional = sorted(r for r, mods in by_dist.items() if not mods & needed)
    if optional and not check_bundle(out_dir, script, block=sorted(set(every) - needed))["ok"]:
        return set(modules), ["could not leave out optional packages (they work together): all kept"]
    notes = [f"optional, left out: {', '.join(optional)}"] if optional else []
    if untested:
        notes.append(f"not tested (time): {', '.join(untested)} - kept")
    return set(modules) - (set(every) - needed), notes


def _distributions(modules) -> dict[str, str]:
    """{module: "dist==version"} for third-party modules (not stdlib, not verknode)."""
    dists = importlib.metadata.packages_distributions()
    out = {}
    for m in sorted(modules):
        if m in sys.stdlib_module_names or m in ("jupyterlab_verknode", "verknode", "__main__") or m.startswith("_"):
            continue
        for d in dists.get(m, []):
            try:
                out[m] = f"{d}=={importlib.metadata.version(d)}"
            except importlib.metadata.PackageNotFoundError:
                out[m] = d
            break
    return out


RUNNER = """import json, os, runpy, sys
for m in filter(None, os.environ.get("VERKNODE_BLOCK", "").split(",")):
    sys.modules[m] = None  # (this package is not there: can it run without?)
here = sys.argv[1]
sys.path.insert(0, here)
os.chdir(here)
before = set(sys.modules)
sys.argv = [os.path.join(here, sys.argv[2]), "--quiet", "--rows", "0"]
ok, error = True, ""
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as e:
    ok = not e.code
    error = "exit code %s" % e.code
except BaseException as e:
    ok, error = False, "%s: %s" % (type(e).__name__, e)
import jupyterlab_verknode
json.dump({"ok": ok, "error": error, "own": jupyterlab_verknode.__file__.startswith(here),
           "modules": sorted({m.split(".")[0] for m in sys.modules})}, open(sys.argv[0] + ".check.json", "w"))
"""


def check_bundle(out_dir: str, script: str, timeout: float = 600, block=()) -> dict:
    """Run the bundle in a fresh process: does it run with its own runtime, and what does it import?"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(RUNNER)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSTARTUP")}
    env["VERKNODE_TEST_SANDBOX"] = "1"
    env["VERKNODE_BLOCK"] = ",".join(block)
    try:
        p = subprocess.run([sys.executable, "-s", f.name, out_dir, script], cwd=out_dir, env=env,
                           capture_output=True, text=True, timeout=timeout)
        result_file = os.path.join(out_dir, script + ".check.json")
        if not os.path.exists(result_file):
            return {"ok": False, "error": (p.stderr or p.stdout)[-800:], "modules": [], "own": False}
        with open(result_file) as r:
            out = json.load(r)
        os.remove(result_file)
        if not out["ok"]:
            out["error"] += "\n" + (p.stderr or "")[-800:]
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the test run took more than {timeout:.0f} s", "modules": [], "own": False}
    finally:
        os.remove(f.name)


# ---------------------------------------------------------------------------------------------------
# the bundle
# ---------------------------------------------------------------------------------------------------
def export_bundle(wf, out_dir: str, name: str | None = None, group: str | None = None, nodes=None,
                  upstream: str = "include", headless: bool = False, data: bool = True, docker: bool = False,
                  api: bool = False, check: bool = True, overwrite: bool = False, minimal: bool = True) -> dict:
    """Write a self-contained folder that runs the workflow; returns a report."""
    from .standalone import to_standalone

    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir) and os.listdir(out_dir):
        if not overwrite:
            raise ExportError(f"{out_dir} exists and is not empty (overwrite replaces it)")
        if not os.path.exists(os.path.join(out_dir, "requirements.txt")):
            raise ExportError(f"{out_dir} is not an export folder: not replaced")
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    stem = name or os.path.splitext(os.path.basename(getattr(wf, "path", None) or "workflow.vkf"))[0]
    script = re.sub(r"\W+", "_", stem).strip("_") or "workflow"
    if script[0].isdigit():
        script = "w_" + script
    t0 = time.time()
    e, notes, problems = prepare(wf, group, nodes, upstream, headless, data_dir=os.path.join(out_dir, "data"))
    if data:
        copy_data(e, out_dir, notes, problems)
    # the runtime
    files, external = runtime_files(e)
    root = os.path.dirname(PACKAGE_DIR)
    for rel in files:  # (in vkn_runtime/: the parts of verknode - not to be edited)
        dest = os.path.join(out_dir, RUNTIME, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if rel == os.path.join("jupyterlab_verknode", "__init__.py"):
            open(dest, "w").write(f'"""verknode runtime, exported with the workflow {stem}."""\n'
                                  f'__version__ = "{_version()}"\n')
        elif rel == os.path.join("verknode", "__init__.py"):
            open(dest, "w").write('"""verknode authoring API (verknode.node) for the exported workflow."""\n')
        else:
            shutil.copy2(os.path.join(root, rel), dest)
    for p in e.node_paths:  # (custom node folders)
        src = p if os.path.isabs(p) else os.path.join(e.base_dir or ".", p)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out_dir, os.path.basename(src.rstrip("/"))),
                            ignore=shutil.ignore_patterns("__pycache__", ".*"), dirs_exist_ok=True)
            for f in os.listdir(src):
                if f.endswith(".py"):
                    external |= _imports(os.path.join(src, f))[1]
    e.node_paths = [os.path.basename(p.rstrip("/")) for p in e.node_paths]
    code = to_standalone(e, f"{script}.vkf", notes=notes, runtime=RUNTIME)
    with open(os.path.join(out_dir, f"{script}.py"), "w") as f:
        f.write(code)
    # requirements: what the modules import when loaded, and what the run imports
    checked = check_bundle(out_dir, f"{script}.py") if check else None
    # (measured: what the run imported, without what it can do without; else what the bundled files
    # import when they load)
    if checked and checked["ok"]:
        used, more = minimize(out_dir, f"{script}.py", checked["modules"]) if minimal else (set(checked["modules"]), [])
        notes += more
    else:
        used = set(external)
    reqs = _distributions(used)
    for n in _real(e):  # (what nodes need in processes of their own, e.g. a notebook's kernel)
        for dist in getattr(e.nodes[n].model, "export_requirements", ()):
            try:
                reqs[dist] = f"{dist}=={importlib.metadata.version(dist)}"
            except importlib.metadata.PackageNotFoundError:
                reqs[dist] = dist
    with open(os.path.join(out_dir, "requirements.txt"), "w") as f:
        f.write(f"# the packages the workflow {stem} needs (measured by running it: {time.strftime('%Y-%m-%d')})\n")
        f.write("\n".join(sorted(set(reqs.values()))) + "\n")
    if api:
        with open(os.path.join(out_dir, "server.py"), "w") as f:
            f.write(SERVER.replace("{script}", script).replace("{stem}", stem))
    if docker:
        _docker(out_dir, script, api, sorted(set(reqs.values())))
    _readme(out_dir, stem, script, e, reqs, notes, problems, docker, api)
    report = {"folder": out_dir, "script": f"{script}.py", "requirements": sorted(set(reqs.values())),
              "notes": notes, "problems": problems, "files": len(files), "seconds": round(time.time() - t0, 1),
              "check": checked, "results": [e.nodes[n].annotation for n in _results(e, _real(e))],
              "variables": {v.name: v.value for v in e.workflow_variables}, "docker": docker, "api": api}
    if checked is not None and not checked["ok"]:
        problems.append("the test run failed: " + checked["error"].strip()[:600])
    return report


def _version():
    try:
        from ._version import __version__

        return __version__
    except ImportError:
        return "0"


# system libraries Python packages need in a slim image (Debian packages)
SYSTEM_PACKAGES = {"rdkit": ["libxrender1", "libxext6", "libexpat1"], "opencv-python": ["libgl1", "libglib2.0-0"],
                   "lxml": [], "pyzmq": []}


def _docker(out_dir, script, api, requirements=()):
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    apt = sorted({p for r in requirements for p in SYSTEM_PACKAGES.get(r.split("==")[0].lower(), [])})
    run = '["python", "server.py"]' if api else f'["python", "{script}.py"]'
    lines = [
        f"# the workflow {script}, headless (built by verknode export)",
        f"FROM python:{py}-slim",
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1",
        "WORKDIR /app",
        *([f"RUN apt-get update && apt-get install -y --no-install-recommends {' '.join(apt)} "
           "&& rm -rf /var/lib/apt/lists/*"] if apt else []),
        "COPY requirements.txt .",
        "RUN pip install --no-cache-dir -r requirements.txt",
        "COPY . .",
        "RUN useradd --create-home app && chown -R app /app",
        "USER app",
    ]
    if api:
        lines += ["EXPOSE 8000",
                  'HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:8000/health\')"',
                  f"CMD {run}"]
    else:
        lines += [f"ENTRYPOINT {run}"]
    with open(os.path.join(out_dir, "Dockerfile"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, ".dockerignore"), "w") as f:
        f.write("__pycache__/\n*.pyc\noutput/\n.git/\n")


def _readme(out_dir, stem, script, e, reqs, notes, problems, docker, api):
    vars_ = {v.name: v.value for v in e.workflow_variables}
    one = next(iter(vars_.items()), None)
    lines = [f"# {stem}", "",
             f"The verknode workflow **{stem}**, exported to run anywhere - no Jupyter, no verknode installation:",
             "this folder has the parts of verknode it uses.", "",
             "## Run it", "", "```",
             "pip install -r requirements.txt",
             f"python {script}.py                         # run it, print the result tables",
             f"python {script}.py --out output/           # write them (--format parquet, json)"]
    if one:
        lines.append(f"python {script}.py --var {one[0]}={one[1]}   # with another workflow variable")
    lines += [f"python {script}.py --save {script}.vkf     # the workflow, to open it in verknode", "```", "",
              "Requirements: " + (", ".join(f"`{r}`" for r in sorted(set(reqs.values()))) or "none") + ".", ""]
    if vars_:
        lines += ["Workflow variables (`--var NAME=VALUE`): " + ", ".join(f"`{k}` = `{v!r}`" for k, v in vars_.items()) + ".", ""]
    if api:
        lines += ["## As an API", "", "```", "python server.py                            # http://localhost:8000",
                  "curl localhost:8000/                        # what it takes and gives",
                  "curl -X POST localhost:8000/run -d '{\"variables\": {" + (f"\"{one[0]}\": {json.dumps(one[1])}" if one else "") + "}}'",
                  "curl 'localhost:8000/run?" + (f"{one[0]}={one[1]}" if one else "") + "&format=csv&result=...'",
                  "```", "",
                  "`PORT` (8000), `HOST` (0.0.0.0), `API_KEY` (if set, requests need `Authorization: Bearer <key>`), "
                  "`MAX_RUNS` (runs at the same time, 2). `/openapi.json` describes it.", ""]
    if docker:
        lines += ["## In Docker", "", "```", f"docker build -t {script.lower().replace('_', '-')} ."]
        if api:
            lines += [f"docker run --rm -p 8000:8000 -e API_KEY=secret {script.lower().replace('_', '-')}"]
        else:
            lines += [f"docker run --rm -v \"$PWD/output:/app/output\" {script.lower().replace('_', '-')} --out output/"]
        lines += ["```", ""]
    if notes:
        lines += ["## How it was exported", ""] + [f"- {n}" for n in notes] + [""]
    if problems:
        lines += ["## Problems", ""] + [f"- {p}" for p in problems] + [""]
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write("\n".join(lines))


SERVER = '''"""HTTP API for the verknode workflow {stem} (exported): each request runs it with the workflow
variables it gives and answers with the result tables. Standard library only.

    python server.py                       # PORT (8000), HOST (0.0.0.0), API_KEY, MAX_RUNS (2)

    GET  /               what it takes (variables, with their defaults) and gives (results)
    GET  /health
    GET  /openapi.json
    POST /run            {"variables": {...}, "results": ["label", ...], "rows": 1000}  -> JSON
    GET  /run?NAME=VALUE&result=LABEL&format=csv|json&rows=N
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import {script} as flow
from jupyterlab_verknode.standalone import Workflow, WorkflowRunError, plain

HERE = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get("API_KEY")
RUNS = threading.BoundedSemaphore(int(os.environ.get("MAX_RUNS", "2")))
DEFAULTS = dict(flow.VARIABLES)


def _workflow():
    wf = Workflow(base_dir=HERE, node_paths=getattr(flow, "NODE_PATHS", ()), variables=DEFAULTS)
    return wf, dict(flow.build(wf))


RESULTS = list(_workflow()[1])


class BadRequest(Exception):
    pass


def _coerce(name, value):
    if name not in DEFAULTS:
        raise BadRequest(f"unknown variable {name!r}; the variables are {sorted(DEFAULTS)}")
    default = DEFAULTS[name]
    try:
        if isinstance(default, bool):
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name}: {value!r} is not a {type(default).__name__}") from None


def run(variables=None, results=None, rows=1000):
    values = {k: _coerce(k, v) for k, v in (variables or {}).items()}
    wanted = results or RESULTS
    unknown = [r for r in wanted if r not in RESULTS]
    if unknown:
        raise BadRequest(f"unknown result(s) {unknown}; the results are {RESULTS}")
    with RUNS:
        wf, outs = _workflow()
        if values:
            wf.set_variables(values)
        t0 = time.time()
        wf.run(*[outs[r] for r in wanted])
        seconds = time.time() - t0
    tables = {}
    for r in wanted:
        df = plain(outs[r].table())  # (molecules as SMILES)
        tables[r] = {"columns": df.columns, "types": [str(t) for t in df.dtypes], "total": df.height,
                     "rows": df.head(rows).to_dicts(), "_frame": df}
    return {"workflow": "{stem}", "variables": {**DEFAULTS, **values}, "seconds": round(seconds, 3), "results": tables}


def openapi():
    types = {bool: "boolean", int: "integer", float: "number", str: "string"}
    props = {k: {"type": types.get(type(v), "string"), "default": v} for k, v in DEFAULTS.items()}
    return {"openapi": "3.0.3", "info": {"title": "{stem}", "version": "1"},
            "paths": {"/run": {
                "get": {"parameters": [{"name": k, "in": "query", "schema": s} for k, s in props.items()]
                        + [{"name": "result", "in": "query", "schema": {"type": "string", "enum": RESULTS}},
                           {"name": "format", "in": "query", "schema": {"type": "string", "enum": ["json", "csv"]}}],
                        "responses": {"200": {"description": "the result tables"}}},
                "post": {"requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                    "variables": {"type": "object", "properties": props},
                    "results": {"type": "array", "items": {"type": "string", "enum": RESULTS}},
                    "rows": {"type": "integer"}}}}}},
                         "responses": {"200": {"description": "the result tables"}}}}}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        if not API_KEY:
            return True
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or self.headers.get("X-API-Key")
        if given == API_KEY:
            return True
        self._send(401, {"error": "an API key is needed (Authorization: Bearer <key>)"})
        return False

    def _answer(self, variables, results, rows, fmt):
        try:
            out = run(variables, results, rows)
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        except WorkflowRunError as e:
            return self._send(500, {"error": f"the workflow failed: {e}"})
        if fmt == "csv":
            if len(out["results"]) != 1:
                return self._send(400, {"error": f"format=csv needs one result (result=...): {RESULTS}"})
            return self._send(200, next(iter(out["results"].values()))["_frame"].write_csv().encode(), "text/csv")
        for t in out["results"].values():
            t.pop("_frame")
        return self._send(200, out)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True})
        if not self._allowed():
            return
        if url.path == "/":
            return self._send(200, {"workflow": "{stem}", "variables": DEFAULTS, "results": RESULTS,
                                    "run": "POST /run {\\"variables\\": {...}} or GET /run?NAME=VALUE",
                                    "openapi": "/openapi.json"})
        if url.path == "/openapi.json":
            return self._send(200, openapi())
        if url.path == "/run":
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            fmt, rows = q.pop("format", "json"), int(q.pop("rows", 1000))
            results = [q.pop("result")] if "result" in q else None
            return self._answer(q, results, rows, fmt)
        self._send(404, {"error": "not found: GET / for what there is"})

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            return self._send(404, {"error": "POST /run"})
        if not self._allowed():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "the body is not JSON"})
        self._answer(body.get("variables") or {}, body.get("results"), int(body.get("rows") or 1000),
                     body.get("format", "json"))

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000"))
    print(f"{stem}: http://{host}:{port}/ (results: {RESULTS})", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
'''


# ---------------------------------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m jupyterlab_verknode.export",
                                description="Export a verknode workflow to run without Jupyter.")
    p.add_argument("workflow", help="the .vkf file")
    p.add_argument("--out", help="the folder to write (default: <name>_export next to the workflow)")
    p.add_argument("--script-only", action="store_true",
                   help="only the script (it needs the jupyterlab-verknode package): --out is its .py path")
    p.add_argument("--group", help="export this group annotation (with what it needs)")
    p.add_argument("--nodes", nargs="+", help="export these nodes (ids or labels)")
    p.add_argument("--snapshot", action="store_true", help="with --group / --nodes: the data coming in, saved")
    p.add_argument("--headless", action="store_true", help="drop widgets and views; input widgets become variables")
    p.add_argument("--no-data", action="store_true", help="do not copy the files the workflow reads")
    p.add_argument("--docker", action="store_true", help="also a Dockerfile")
    p.add_argument("--api", action="store_true", help="also server.py: an HTTP API for the workflow")
    p.add_argument("--no-check", action="store_true", help="do not test-run the export")
    p.add_argument("--overwrite", action="store_true")
    a = p.parse_args(argv)
    from .standalone import Workflow, to_standalone

    wf = Workflow.load(a.workflow).engine
    stem = os.path.splitext(os.path.basename(a.workflow))[0]
    if a.script_only:
        e, notes, problems = prepare(wf, a.group, a.nodes, "include", a.headless)
        out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.workflow)), stem + ".py")
        with open(out, "w") as f:
            f.write(to_standalone(e, os.path.basename(a.workflow), notes=notes))
        print(f"wrote {out}" + "".join(f"\n- {n}" for n in notes) + "".join(f"\nPROBLEM: {x}" for x in problems))
        return
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.workflow)), stem + "_export")
    try:
        r = export_bundle(wf, out, stem, a.group, a.nodes, "snapshot" if a.snapshot else "include", a.headless,
                          not a.no_data, a.docker, a.api, not a.no_check, a.overwrite)
    except ExportError as e:
        raise SystemExit(str(e)) from None
    print(format_report(r))
    sys.exit(1 if r["problems"] else 0)


def format_report(r) -> str:
    lines = [f"Exported to {r['folder']} ({r['files']} runtime files, {r['seconds']} s):",
             f"  run:          python {r['script']}" + ("   |   python server.py (API)" if r["api"] else ""),
             f"  requirements: {', '.join(r['requirements']) or 'none'}",
             f"  results:      {', '.join(r['results'])}"]
    if r["variables"]:
        lines.append(f"  variables:    {r['variables']}")
    if r["check"] is not None:
        lines.append(f"  test run:     {'OK' if r['check']['ok'] else 'FAILED'}"
                     + ("" if r["check"].get("own") or not r["check"]["ok"] else " (but not on its own runtime!)"))
    lines += [f"  - {n}" for n in r["notes"]] + [f"  PROBLEM: {p}" for p in r["problems"]]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
