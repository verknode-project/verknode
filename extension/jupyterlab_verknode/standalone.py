"""Workflows without Jupyter: build and run them in this Python process.

A workflow exported as a standalone script (to_standalone; the editor's Python > Standalone script)
rebuilds itself with this API and runs - from a terminal, cron, CI, a container or another program,
with no Jupyter server:

    python sales_report.py --var threshold=150 --out results/

    from jupyterlab_verknode.standalone import Workflow
    wf = Workflow(base_dir="data")
    sales = wf.add_node("CSV Reader", "Sales", path="sales.csv")
    top = wf.add_node("Sorter", "Top", keys=[{"column": "amount", "order": "Descending"}])
    wf.connect(sales, top)
    wf.run(top)
    top.table()                      # polars DataFrame

.vkf files run as they are, too:

    python -m jupyterlab_verknode.standalone report.vkf --var threshold=150 --out results/
"""
from __future__ import annotations

import argparse
import keyword
import os
import re
import sys
import time

import polars as pl

from .engine.flowvars import VAR_TYPES
from .engine.persistence import load_workflow, save_workflow
from .engine.workflow import Workflow as _Engine


class WorkflowRunError(RuntimeError):
    """Nodes failed (or did not run): the message says which and why."""


class Node:
    def __init__(self, wf: "Workflow", nid: int):
        self.wf, self.id = wf, nid

    @property
    def _nc(self):
        return self.wf.engine.nodes[self.id]

    @property
    def label(self) -> str:
        return self._nc.annotation

    @property
    def name(self) -> str:
        return self._nc.name

    @property
    def executed(self) -> bool:
        return self._nc.executed

    @property
    def message(self):
        return self._nc.message

    def output(self, port: int = 1):
        """What the node made at an output port (a DataFrame, a model, an image, ...)."""
        if not self.executed:
            raise WorkflowRunError(f"{self} has not run (Workflow.run runs it)")
        return self._nc.outputs[port]

    def table(self, port: int = 1) -> pl.DataFrame:
        out = self.output(port)
        if not isinstance(out, pl.DataFrame):
            raise TypeError(f"Output {port} of {self} is not a table ({type(out).__name__})")
        return out

    def __repr__(self):
        return f"<{self.name} #{self.id} {self.label!r}>"


class Workflow:
    """A workflow in this process: add nodes, connect them, run, read the results."""

    def __init__(self, base_dir: str = ".", node_paths=(), variables: dict | None = None, engine: _Engine | None = None):
        _register()
        self.engine = engine or _Engine()
        self.engine.headless = True  # (no canvas to wait for)
        if engine is None:
            self.engine.base_dir = os.path.abspath(base_dir)
            if node_paths:
                self.engine.set_node_paths(list(node_paths))
        if variables:
            self.set_variables(variables)

    # -- building -------------------------------------------------------------------------------
    def _factory(self, node_type: str) -> str:
        types = self.engine.node_types()
        for t in types:
            if node_type in (t["name"], t["factory"]):
                return t["factory"]
        low = node_type.lower()
        hit = [t["factory"] for t in types if t["name"].lower() == low]
        if hit:
            return hit[0]
        import difflib

        close = difflib.get_close_matches(node_type, [t["name"] for t in types], n=3, cutoff=0.5)
        raise ValueError(f"Unknown node type {node_type!r}" + (f" - did you mean {', '.join(close)}?" if close else ""))

    def add_node(self, node_type: str, label: str | None = None, at=None, settings: dict | None = None, **kw) -> Node:
        """A node of a type (its name, e.g. "CSV Reader"), with its settings (keyword arguments, or
        settings= for names that are not identifiers). at: (x, y) on the canvas."""
        x, y = at if at is not None else (100 + 150 * len(self.engine.nodes), 100)
        nid = self.engine.add_node(self._factory(node_type), x, y)
        node = Node(self, nid)
        given = {**(settings or {}), **kw}
        keys = {f.key for f in self.engine.nodes[nid].model.fields}
        unknown = [k for k in given if k not in keys]
        if unknown:
            self.engine.remove_nodes([nid])
            raise ValueError(f"{node_type} has no setting(s) {unknown}; its settings are {sorted(keys)}")
        selection = given.pop("selection", None)
        if given:
            self.engine.set_settings(nid, given)
        if selection is not None:  # (after the others: a change of them drops the selection)
            self.engine.set_settings(nid, {"selection": selection})
        if label:
            self.engine.set_annotation(nid, label)
        return node

    def connect(self, source: Node, dest: Node, src_port: int = 1, dst_port: int = 1) -> None:
        self.engine.connect(source.id, src_port, dest.id, dst_port)

    def bind(self, node: Node, setting: str, variable: str | None = None, expose: str | None = None) -> None:
        """Take a setting from a flow variable (and/or expose it as one)."""
        nc = self.engine.nodes[node.id]
        vs = dict(nc.var_settings)
        vs[setting] = {"used": variable, "exposed": expose}
        self.engine.set_settings(node.id, {}, vs)

    def set_variables(self, variables: dict) -> None:
        """Workflow variables: {name: value} (the type follows the value, or the variable's type)."""
        items = {v.name: v.to_json() for v in self.engine.workflow_variables}
        for name, value in variables.items():
            vtype = items.get(name, {}).get("type") or _var_type(value)
            items[name] = {"name": name, "type": vtype, "value": value}
        self.engine.set_workflow_variables(list(items.values()))

    @property
    def variables(self) -> dict:
        return {v.name: v.value for v in self.engine.workflow_variables}

    # -- reading --------------------------------------------------------------------------------
    @property
    def nodes(self) -> list[Node]:
        return [Node(self, n) for n in self.engine.topo_order()]

    def find(self, label: str) -> Node:
        hits = [n for n in self.nodes if n.label == label]
        if len(hits) != 1:
            raise KeyError(f"{len(hits)} nodes are labelled {label!r}")
        return hits[0]

    def results(self) -> dict[str, Node]:
        """The last table of each branch: nodes with a table output that feeds no other node."""
        e = self.engine
        out = {}
        for n in self.nodes:
            nc = e.nodes[n.id]
            if any(c.source_id == n.id for c in e.connections) or e._boundaries().get(n.id):
                continue
            if any(p.type == "table" for p in nc.model.out_ports):
                out[n.label or f"#{n.id}"] = n
        return out

    # -- running --------------------------------------------------------------------------------
    def run(self, *nodes: Node, check: bool = True, rerun: bool = False) -> list[dict]:
        """Run the workflow (or these nodes, with what they need). Returns a report per node; with
        check, a node that fails (or does not run) raises WorkflowRunError."""
        e = self.engine
        ids = [n.id for n in nodes] if nodes else list(e.nodes)
        targets = set(ids) | set().union(*(e.ancestors(i) for i in ids))
        if rerun:
            e.reset(list(targets))
        e.execute(ids, wait=True)
        _settle(e)
        report = []
        for nid in e.topo_order(targets & set(e.nodes)):  # (Workflow Script edits may remove nodes)
            nc = e.nodes[nid]
            msg = nc.message
            rows = next((o.height for o in (nc.outputs or [])[1:] if isinstance(o, pl.DataFrame)), None)
            report.append({"id": nid, "node": nc.name, "label": nc.annotation, "executed": nc.executed,
                           "rows": rows, "seconds": nc.exec_time, "message": msg[1] if msg else None,
                           "level": msg[0] if msg else None})
        failed = [r for r in report if not r["executed"]]
        if check and failed:
            raise WorkflowRunError("; ".join(f"{r['node']} #{r['id']} {r['label']!r}: "
                                             f"{r['message'] or 'did not run (a node before it failed?)'}" for r in failed))
        return report

    # -- files ----------------------------------------------------------------------------------
    def save(self, path: str) -> None:
        """Write the workflow as a .vkf file (to open it in the workflow editor)."""
        with open(path, "wb") as f:
            f.write(save_workflow(self.engine))

    @classmethod
    def load(cls, path: str) -> "Workflow":
        _register()  # (before loading: else its nodes are not known)
        with open(path, "rb") as f:
            engine = load_workflow(f.read(), os.path.dirname(os.path.abspath(path)))
        engine.path = os.path.abspath(path)
        if engine.node_paths:
            engine.reload_nodes(reason="load")
        return cls(engine=engine)


def plain(df: pl.DataFrame) -> pl.DataFrame:
    """The table with molecules as SMILES (for CSV, JSON and printing)."""
    from .engine.types import dtype_kind

    if any(dtype_kind(t) == "molecule" for t in df.schema.values()):
        try:
            from verknode.node import chem

            if chem is not None:
                return chem.molecules_to_smiles(df)
        except ImportError:
            pass
    return df


def _register():
    import jupyterlab_verknode.nodes  # noqa: F401 - registers the node types


def _settle(e, quiet: float = 0.3):
    """Wait until the engine is done - also with the runs that Workflow Script edits start after a run."""
    calm = None
    while True:
        t = e._thread
        if t is not None and t.is_alive():
            t.join(0.2)
            calm = None
        elif e.executing:
            time.sleep(0.05)
            calm = None
        elif calm is None:
            calm = time.time()
        elif time.time() - calm > quiet:
            return
        else:
            time.sleep(0.05)


def _var_type(value) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"


# ---------------------------------------------------------------------------------------------------
# the command line of standalone scripts (and of .vkf files)
# ---------------------------------------------------------------------------------------------------
def _parse_var(text: str, current: dict, types: dict):
    if "=" not in text:
        raise SystemExit(f"--var takes NAME=VALUE, not {text!r}")
    name, value = text.split("=", 1)
    vtype = types.get(name)
    if vtype is None:
        raise SystemExit(f"There is no workflow variable {name!r}; the variables are {sorted(current) or 'none'}")
    try:
        return name, {"INTEGER": int, "DOUBLE": float, "BOOLEAN": lambda v: v.lower() in ("1", "true", "yes", "on"),
                      "STRING": str}[vtype](value)
    except ValueError:
        raise SystemExit(f"--var {name}: {value!r} is not a {vtype.lower()}") from None


def _arguments(description: str):
    p = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="set a workflow variable")
    p.add_argument("--out", metavar="DIR", help="write the result tables to this folder")
    p.add_argument("--format", choices=["csv", "parquet", "json"], default="csv", help="of the written tables")
    p.add_argument("--result", action="append", default=[], metavar="LABEL",
                   help="the result node(s) (default: the last table of each branch)")
    p.add_argument("--rows", type=int, default=10, help="rows of each result to print (0: none)")
    p.add_argument("--save", metavar="FILE.vkf", help="also write the workflow (to open it in the editor)")
    p.add_argument("--quiet", action="store_true", help="print only errors")
    return p


def _finish(wf: Workflow, a, results: dict[str, Node], rerun: bool = False) -> int:
    if a.var:
        types = {v.name: v.type for v in wf.engine.workflow_variables}
        wf.set_variables(dict(_parse_var(v, wf.variables, types) for v in a.var))
    if a.result:
        results = {label: wf.find(label) for label in a.result}
    if not results:
        results = wf.results()
    if os.environ.get("VERKNODE_TEST_SANDBOX"):  # (a test run: files are read, nothing real is written)
        from .engine.sandbox import Sandbox

        wf.engine.sandbox = Sandbox(allow_unsafe=True)
    t0 = time.time()
    try:
        report = wf.run(*results.values(), rerun=rerun)
    except WorkflowRunError as e:
        print(f"The workflow failed: {e}", file=sys.stderr)
        return 1
    if not a.quiet:
        print(f"Ran {sum(r['executed'] for r in report)} nodes in {time.time() - t0:.1f} s"
              + (f" (variables: {wf.variables})" if wf.variables else ""))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    for label, node in results.items():
        out = node.output()
        if not isinstance(out, pl.DataFrame):
            continue
        if a.out:  # (molecules: kept in Parquet, as SMILES in CSV and JSON)
            path = os.path.join(a.out, re.sub(r"[^\w.-]+", "_", label).strip("_") + "." + a.format)
            table = out if a.format == "parquet" else plain(out)
            {"csv": table.write_csv, "parquet": table.write_parquet, "json": table.write_json}[a.format](path)
            if not a.quiet:
                print(f"wrote {path} ({out.height} rows)")
        out = plain(out)
        if a.rows and not a.quiet:
            with pl.Config(tbl_rows=a.rows, tbl_cols=20, fmt_str_lengths=40):
                print(f"\n{label}:\n{out.head(a.rows)}")
    if a.save:
        wf.save(a.save)
        if not a.quiet:
            print(f"saved {a.save}")
    return 0


def cli(build, variables: dict | None = None, base_dir: str = ".", node_paths=(), description: str = "",
        argv=None) -> None:
    """The command line of a standalone script: build(wf) adds the nodes and returns the result nodes
    ({label: node}); --var, --out, --format, --result, --rows, --save, --base-dir, --quiet."""
    p = _arguments(description)
    p.add_argument("--base-dir", default=base_dir, help="the folder file paths are relative to "
                   "(default: the script's folder)")
    a = p.parse_args(argv)
    wf = Workflow(base_dir=a.base_dir, node_paths=node_paths, variables=variables)
    results = build(wf) or {}
    sys.exit(_finish(wf, a, dict(results)))


def main(argv=None) -> None:
    """python -m jupyterlab_verknode.standalone WORKFLOW.vkf [--var NAME=VALUE ...] [--out DIR] ..."""
    p = _arguments("Run a verknode workflow (.vkf) without Jupyter.")
    p.add_argument("workflow", help="the .vkf file")
    p.add_argument("--cached", action="store_true", help="keep the results saved in the file (default: run afresh)")
    a = p.parse_args(argv)
    wf = Workflow.load(a.workflow)
    sys.exit(_finish(wf, a, {}, rerun=not a.cached))


# ---------------------------------------------------------------------------------------------------
# export: a workflow as a standalone script
# ---------------------------------------------------------------------------------------------------
def _identifier(label: str, taken: set) -> str:
    name = re.sub(r"\W+", "_", (label or "").strip().lower()).strip("_")
    if not name or name[0].isdigit() or keyword.iskeyword(name) or name in ("wf", "os", "cli", "build", "VARIABLES"):
        name = f"n_{name}" if name else "node"
    base, i = name, 2
    while name in taken:
        name, i = f"{base}_{i}", i + 1
    taken.add(name)
    return name


def to_standalone(wf: _Engine, path: str | None = None, notes=()) -> str:
    """A Python script that rebuilds the workflow with this module's API and runs it (no Jupyter)."""
    from .commands import _lit, _changed_settings

    target = os.path.basename(path or "workflow.vkf")
    stem = os.path.splitext(target)[0]
    script = re.sub(r"\W+", "_", stem).strip("_") or "workflow"
    bounds = wf._boundaries()
    taken: set = set()
    names: dict[int, str] = {}
    body = []
    for nid in sorted((n for n in wf.nodes if n not in bounds), key=lambda n: (wf.nodes[n].x, wf.nodes[n].y, n)):
        nc = wf.nodes[nid]
        label = nc.annotation if nc.annotation and nc.annotation != f"Node {nid}" else ""
        var = names[nid] = _identifier(label or nc.name, taken)
        settings = _changed_settings(nc)
        plain = {k: v for k, v in settings.items() if k.isidentifier() and not keyword.iskeyword(k)
                 and k not in ("node_type", "label", "at", "settings")}
        rest = {k: v for k, v in settings.items() if k not in plain}
        args = [repr(nc.name)] + ([repr(label)] if label else [])
        args += [f"{k}={_lit(v)}" for k, v in plain.items()]
        if rest:
            args.append(f"settings={_lit(rest)}")
        args.append(f"at=({round(nc.x)}, {round(nc.y)})")
        line = f"{var} = wf.add_node({', '.join(args)})"
        if len(line) > 110:  # (one argument a line)
            line = f"{var} = wf.add_node(\n    " + ",\n    ".join(args) + ",\n)"
        body.append(line)
        for key, b in (nc.var_settings or {}).items():
            if b.get("used") or b.get("exposed"):
                extra = f", {b['used']!r}" if b.get("used") else ""
                extra += f", expose={b['exposed']!r}" if b.get("exposed") else ""
                body.append(f"wf.bind({var}, {key!r}{extra})")
    if wf.connections:
        body.append("")

    def targets(d, dp):  # (through metanode ports: to the nodes inside)
        if d not in bounds:
            return [(d, dp)]
        return [t for c in wf.connections if c.source_id == d for t in targets(c.dest_id, c.dest_port)]

    def sources(s, sp):  # (out of metanodes: from the nodes inside)
        if s not in bounds:
            return [(s, sp)]
        return [t for c in wf.connections if c.dest_id == s for t in sources(c.source_id, c.source_port)]

    seen = set()
    for c in wf.connections:
        for s, sp in sources(c.source_id, c.source_port):
            for d, dp in targets(c.dest_id, c.dest_port):
                if (s, sp, d, dp) in seen or s in bounds or d in bounds:
                    continue
                seen.add((s, sp, d, dp))
                ports = (f", src_port={sp}" if sp != 1 else "") + (f", dst_port={dp}" if dp != 1 else "")
                body.append(f"wf.connect({names[s]}, {names[d]}{ports})")
    # (the results: the last table of each branch - fed to no other node)
    results = [n for n in names if not any(c.source_id == n for c in wf.connections)
               and any(p.type == "table" for p in wf.nodes[n].model.out_ports)]
    labels = {n: (wf.nodes[n].annotation if wf.nodes[n].annotation != f"Node {n}" else names[n]) for n in results}
    body.append("")
    body.append("return {" + ", ".join(f"{labels[n]!r}: {names[n]}" for n in results) + "}")
    variables = {v.name: v.value for v in wf.workflow_variables}
    usage = [(f"python {script}.py", "run it, print the results")]
    if variables:
        name, value = next(iter(variables.items()))
        usage.append((f"python {script}.py --var {name}={value}", "with another workflow variable"))
    usage += [(f"python {script}.py --out results/", "and write the result tables (--format parquet, json)"),
              (f"python {script}.py --save {script}.vkf", "and write the workflow, to open it in the editor")]
    width = max(len(c) for c, _ in usage) + 2
    lines = [
        f'"""{stem}: the verknode workflow {target} as a standalone script - it runs without Jupyter',
        "(a terminal, cron, CI, a container, another program).",
        "",
        *(f"    {c.ljust(width)}# {d}" for c, d in usage),
        "",
        "File paths are relative to --base-dir (default: this script's folder). It needs the",
        "jupyterlab-verknode package (and the packages its nodes use) - not a Jupyter server.",
        f"Exported by verknode on {time.strftime('%Y-%m-%d')}" + (":" if notes else "."),
        *(f"- {n}" for n in notes),
        '"""',
        "import os",
        "",
        "from jupyterlab_verknode.standalone import cli",
        "",
        f"VARIABLES = {_lit(variables)}  # workflow variables (--var NAME=VALUE)",
    ]
    if wf.node_paths:
        lines.append(f"NODE_PATHS = {_lit(list(wf.node_paths))}  # custom node folders (relative to --base-dir)")
    lines += [
        "",
        "",
        "def build(wf):",
        '    """The nodes and their connections; returns the result nodes."""',
    ]
    lines += [("    " + line.replace("\n", "\n    ")) if line else "" for line in body]
    lines += [
        "",
        "",
        'if __name__ == "__main__":',
        f"    cli(build, VARIABLES, base_dir=os.path.dirname(os.path.abspath(__file__)),"
        + (" node_paths=NODE_PATHS," if wf.node_paths else "")
        + f" description=__doc__)",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
