"""Jupyter Notebook node: runs a notebook with the node's inputs and collects its outputs."""
from __future__ import annotations

import json
import os
import re
import tempfile
import textwrap
import threading

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, infer_type

INJECTED_TAG = "injected-parameters"
EXPORT_TAG = "verknode-export"
MARKER_TAG = "verknode-run-marker"
#: set in the kernel of a workflow run: verknode.notebook_inputs() then returns placeholders
RUN_MARKER = "VERKNODE_NOTEBOOK_RUN"


def _kernels() -> list:
    try:
        from jupyter_client.kernelspec import KernelSpecManager

        specs = KernelSpecManager().get_all_specs()
    except Exception:  # noqa: BLE001 - no kernels found: the dialog offers the default
        specs = {}
    return [{"name": k, "display": v["spec"].get("display_name", k), "language": v["spec"].get("language", "")}
            for k, v in sorted(specs.items())]


def _cell_tags(cell) -> list:
    return list(cell.get("metadata", {}).get("tags", []))


MAX_PORTS = 9
_INPUT = re.compile(r"\binput_table_(\d+)\b")
_OUTPUT = re.compile(r"\boutput_table_(\d+)\b")


def port_counts(nb) -> tuple[int, int]:
    """Ports a notebook uses: the highest input_table_N / output_table_N in its code (at least
    one output)."""
    code = "\n".join(c.source for c in nb.cells if c.cell_type == "code"
                     and not {INJECTED_TAG, EXPORT_TAG} & set(_cell_tags(c)))
    n_in = max([int(n) for n in _INPUT.findall(code)] + [0])
    n_out = max([int(n) for n in _OUTPUT.findall(code)] + [1])
    return min(n_in, MAX_PORTS), min(n_out, MAX_PORTS)


def _in_ports(n):
    return [Port(TABLE, f"Input table {i}", f"Available as input_table_{i}", optional=True) for i in range(1, n + 1)]


def _out_ports(n):
    return [Port(TABLE, f"Output table {i}", f"Value of output_table_{i}") for i in range(1, n + 1)]


class JupyterNotebook(NodeModel):
    factory = "verknode.scripting.JupyterNotebook"
    name = "Jupyter Notebook"
    category = "Scripting/Python"
    node_type = "Manipulator"
    icon = "nb"
    has_view = True
    view_on_error = True  # the executed notebook shows where it failed
    short_description = "Runs a Jupyter notebook with the node's input tables and flow variables."
    description = (
        "Executes a notebook (headless, on its kernel) as a workflow step. The input tables "
        "are available as <code>input_table_1</code>, <code>input_table_2</code>, … (polars, "
        "<code>None</code> when not connected) and flow variables as <code>flow_variables</code>; "
        "they are injected after the cell tagged <code>parameters</code> (papermill convention), "
        "or at the top. Assign <code>output_table_1</code>, … (pandas DataFrames are converted); "
        "new or changed <code>flow_variables</code> are passed on. The node's ports follow the "
        "notebook: the highest <code>input_table_N</code> / <code>output_table_N</code> it uses "
        "(updated when the notebook is saved). The executed notebook is the node's view. "
        "With <b>Run in the open notebook</b>, a notebook open in JupyterLab runs there, cell "
        "by cell in its kernel, so you can watch it (otherwise it runs headless). "
        "<b>Create notebook</b> in the dialog writes a notebook with the inputs, outputs and "
        "imports prepared for a kernel; run interactively (<b>Open in JupyterLab</b>), its "
        "parameters cell reads the node's inputs from the live workflow with "
        "<code>verknode.notebook_inputs()</code>. Editing the notebook resets the node."
    )
    # defaults; every node takes its ports from its notebook (update_ports)
    in_ports = _in_ports(1)
    out_ports = _out_ports(1)
    fields = [
        F("path", "notebook", "Notebook", "", "The .ipynb file, relative to the workflow's folder.", path=True),
        F("kernel", "kernel", "Kernel", "", "Kernel to run it with (empty: the notebook's own kernel)."),
        F("timeout", "int", "Cell timeout (seconds)", 600, "Longest a single cell may run (0: no limit)."),
        F("live", "bool", "Run in the open notebook: when it is open in JupyterLab, run it there (cells visibly, in its kernel)", False),
        F("save_copy", "bool", "Save the executed notebook as <name>.executed.ipynb", False),
    ]

    def __init__(self):
        self.in_ports = list(type(self).in_ports)
        self.out_ports = list(type(self).out_ports)

    def update_ports(self, s, base_dir) -> bool:
        """Ports from the notebook (unchanged while it cannot be read)."""
        try:
            import nbformat

            n_in, n_out = port_counts(nbformat.read(self._path(s, base_dir), as_version=4))
        except Exception:  # noqa: BLE001 - missing / unreadable notebook: keep the ports
            return False
        if (n_in, n_out) == (len(self.in_ports), len(self.out_ports)):
            return False
        self.in_ports, self.out_ports = _in_ports(n_in), _out_ports(n_out)
        return True

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _path(s, base_dir) -> str:
        path = (s.get("path") or "").strip()
        if not path:
            raise InvalidSettingsError("Choose or create a notebook")
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))

    def external_state(self, s, base_dir):
        """The notebook file's state: editing it resets the node."""
        try:
            st = os.stat(self._path(s, base_dir))
            return f"{st.st_mtime_ns}:{st.st_size}"
        except (OSError, InvalidSettingsError):
            return None

    def configure(self, in_specs, s, ctx):
        path = self._path(s, ctx.base_dir)
        if not os.path.isfile(path):
            raise InvalidSettingsError(f"Notebook not found: {s['path']}")
        return [None, None]  # known after execution

    # ------------------------------------------------------------ dialog
    def dialog_data(self, inputs, s, connected=(), context=None):
        context = context or {}
        base = context.get("base_dir") or "."
        out = {"kernels": _kernels(), "connected": list(connected), "exists": False}
        path = (s.get("path") or "").strip()
        if not path:
            out["suggested"] = self._suggested(context)
            return out
        full = self._path(s, base)
        out["exists"] = os.path.isfile(full)
        root = context.get("root_dir")
        if root and (full == root or full.startswith(root + os.sep)):
            out["rootPath"] = os.path.relpath(full, root).replace(os.sep, "/")
        if out["exists"]:
            try:
                import nbformat

                nb = nbformat.read(full, as_version=4)
                out["kernel"] = nb.metadata.get("kernelspec", {}).get("name")
                out["parametersCell"] = any("parameters" in _cell_tags(c) for c in nb.cells)
                out["cells"] = len(nb.cells)
                out["inputs"], out["outputs"] = port_counts(nb)
            except Exception as e:  # noqa: BLE001 - shown in the dialog
                out["error"] = f"Cannot read the notebook: {e}"
        return out

    @staticmethod
    def _suggested(context) -> str:
        stem = os.path.splitext(os.path.basename(context.get("workflow_path") or "workflow"))[0]
        return f"{stem}_node{context.get('node_id', '')}.ipynb"

    def dialog_action(self, name, s, args, context=None):
        if name != "create":
            raise ValueError(f"Unknown action {name}")
        context = context or {}
        base = context.get("base_dir") or "."
        path = (s.get("path") or "").strip() or self._suggested(context)
        full = self._path({"path": path}, base)
        if os.path.exists(full) and not args.get("overwrite"):
            raise ValueError(f"{path} already exists")
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        import nbformat

        nbformat.write(self._template(s, context, full), full)
        return {"path": path, "data": self.dialog_data([], dict(s, path=path), context=context)}

    def _template(self, s, context, full):
        import nbformat
        from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

        kernels = {k["name"]: k for k in _kernels()}
        kernel = s.get("kernel") or ("python3" if "python3" in kernels or not kernels else next(iter(kernels)))
        spec = kernels.get(kernel, {"name": kernel, "display": kernel, "language": "python"})
        workflow = context.get("workflow_path")
        rel_workflow = os.path.relpath(workflow, os.path.dirname(full)) if workflow else "workflow.vkf"
        node_id = context.get("node_id")
        n_in, n_out = max(1, len(self.in_ports)), max(1, len(self.out_ports))
        try:
            import rdkit  # noqa: F401

            chem_import = "from verknode.node import chem    # RDKit helpers for molecule columns\nfrom rdkit import Chem\n"
        except ImportError:
            chem_import = ""
        nb = new_notebook()
        nb.metadata["kernelspec"] = {"name": spec["name"], "display_name": spec["display"],
                                     "language": spec.get("language") or "python"}
        nb.metadata["language_info"] = {"name": spec.get("language") or "python"}
        nb.metadata["verknode"] = {"workflow": rel_workflow, "node": node_id}
        nb.cells = [
            new_markdown_cell(textwrap.dedent(f"""\
                # Jupyter Notebook node #{node_id} of `{os.path.basename(rel_workflow)}`

                The workflow runs this notebook as a step. When it does, the node's inputs are
                injected right after the **parameters** cell below; when you run the notebook here,
                that cell reads them from the live workflow (upstream nodes are executed if needed).

                * inputs: `input_table_1`, … (polars DataFrames, `None` when not connected) and
                  `flow_variables` (dict)
                * outputs: assign `output_table_1`, …; new or changed `flow_variables` are passed on
                * the node's ports follow this notebook: use `input_table_3` or assign
                  `output_table_2` and save, and the node gets that port""")),
            new_code_cell(
                "import numpy as np\nimport polars as pl\nimport verknode\n" + chem_import + "\n"
                "# Inputs. When the workflow runs this notebook, they are injected after this cell.\n"
                f"_inputs = verknode.notebook_inputs({rel_workflow!r}, node={node_id})\n"
                + "".join(f"input_table_{i} = _inputs[\"input_table_{i}\"]\n" for i in range(1, n_in + 1))
                + "flow_variables = _inputs[\"flow_variables\"]",
                metadata={"tags": ["parameters"]},
            ),
            new_code_cell("input_table_1"),
            new_code_cell(
                "# Your analysis. Outputs of the node:\n"
                "output_table_1 = input_table_1 if input_table_1 is not None else pl.DataFrame()\n"
                + "".join(f"output_table_{i} = pl.DataFrame()\n" for i in range(2, n_out + 1))
                + "output_table_1"
            ),
        ]
        nbformat.validate(nb)
        return nb

    # ------------------------------------------------------------ execution
    @staticmethod
    def _input_code(ins, vars_in, live: bool) -> str:
        """Code giving the kernel the node's inputs (live: also for verknode.notebook_inputs())."""
        loads = "\n".join(
            f"input_table_{i} = _verknode_pl.read_parquet({p!r})" if p else f"input_table_{i} = None"
            for i, p in enumerate(ins, start=1)
        )
        code = (
            "# Inputs of the verknode workflow node (injected)\n"
            "import json as _verknode_json\nimport polars as _verknode_pl\n"
            f"{loads}\n"
            f"with open({vars_in!r}) as _verknode_f:\n    flow_variables = _verknode_json.load(_verknode_f)\n"
            "VERKNODE_WORKFLOW = True"
        )
        if live:
            # the parameters cell's verknode.notebook_inputs() returns these while the workflow runs it
            names = ", ".join(f"'input_table_{i}': input_table_{i}" for i in range(1, len(ins) + 1))
            code += f"\nimport verknode as _verknode\n_verknode._live_inputs = {{{names}, 'flow_variables': dict(flow_variables)}}"
        return code

    @staticmethod
    def _export_code(tmp, vars_out, n_out: int, live: bool) -> str:
        """Code writing the outputs of the kernel to files."""
        return textwrap.dedent(f"""\
            import json as _verknode_json
            import polars as _verknode_pl
            def _verknode_frame(value):
                if isinstance(value, _verknode_pl.DataFrame):
                    return value
                try:
                    import pandas as _verknode_pd
                    if isinstance(value, _verknode_pd.DataFrame):
                        return _verknode_pl.from_pandas(value)
                except ImportError:
                    pass
                return _verknode_pl.DataFrame(value)
            for _verknode_i in range(1, {n_out + 1}):
                _verknode_v = globals().get(f"output_table_{{_verknode_i}}")
                if _verknode_v is not None:
                    _verknode_frame(_verknode_v).write_parquet({tmp!r} + f"/out{{_verknode_i}}.parquet")
            with open({vars_out!r}, "w") as _verknode_f:
                _verknode_json.dump({{k: v for k, v in globals().get("flow_variables", {{}}).items()
                                 if isinstance(v, (str, int, float, bool))}}, _verknode_f)""") + (
            "\nimport verknode as _verknode\n_verknode._live_inputs = None" if live else "")

    def execute(self, inputs, s, ctx):
        import nbformat

        path = self._path(s, ctx.base_dir)
        if not os.path.isfile(path):
            raise InvalidSettingsError(f"Notebook not found: {s['path']}")
        variables = {k: v for k, v in ctx.variables().items() if isinstance(v, (str, int, float, bool))}
        n_out = len(self.out_ports)
        with tempfile.TemporaryDirectory(prefix="verknode-nb-") as tmp:
            ins = []
            for i, df in enumerate(inputs, start=1):
                if df is None:
                    ins.append(None)
                else:
                    p = os.path.join(tmp, f"in{i}.parquet")
                    df.write_parquet(p)
                    ins.append(p)
            vars_in = os.path.join(tmp, "vars_in.json")
            vars_out = os.path.join(tmp, "vars_out.json")
            with open(vars_in, "w") as f:
                json.dump(variables, f)
            os.chmod(tmp, 0o755)  # the kernel of an open notebook may run as another process

            result = None
            root_path = self._root_path(path, getattr(ctx, "workflow_context", {}))
            if s.get("live") and root_path and hasattr(ctx, "frontend_request"):
                # run in the notebook open in JupyterLab (an editor claims it), else headless
                result = ctx.frontend_request({
                    "kind": "notebook",
                    "path": root_path,
                    "pre": self._input_code(ins, vars_in, live=True),
                    "post": self._export_code(tmp, vars_out, n_out, live=True),
                })
                if result is None:
                    ctx.log("The notebook is not open in JupyterLab: running it headless")
            if result is not None:
                nb = nbformat.from_dict(result["notebook"]) if result.get("notebook") else None
                if nb is not None:
                    self._report(nb, ctx, s, path, stream=False)
                if not result.get("ok"):
                    raise ValueError(f"Notebook failed: {result.get('error') or 'error in a cell'}")
            else:
                self._run_headless(nbformat.read(path, as_version=4), path, ins, vars_in, vars_out, tmp, n_out, s, ctx)

            outs = []
            for i in range(1, n_out + 1):
                p = os.path.join(tmp, f"out{i}.parquet")
                if os.path.exists(p):
                    outs.append(pl.read_parquet(p))
                elif i == 1:
                    raise ValueError("The notebook did not assign output_table_1")
                else:
                    ctx.warn(f"output_table_{i} was not assigned (empty table)")
                    outs.append(pl.DataFrame())
            if os.path.exists(vars_out):
                with open(vars_out) as f:
                    for name, value in json.load(f).items():
                        if variables.get(name) != value:
                            ctx.push_variable(name, value, infer_type(value))
        return outs

    @staticmethod
    def _root_path(path, context):
        root = (context or {}).get("root_dir")
        if root and (path == root or path.startswith(root + os.sep)):
            return os.path.relpath(path, root).replace(os.sep, "/")
        return None

    def _run_headless(self, nb, path, ins, vars_in, vars_out, tmp, n_out, s, ctx):
        import nbformat
        from nbclient import NotebookClient
        from nbclient.exceptions import CellExecutionError

        injected = nbformat.v4.new_code_cell(self._input_code(ins, vars_in, live=False), metadata={"tags": [INJECTED_TAG]})
        export = nbformat.v4.new_code_cell(self._export_code(tmp, vars_out, n_out, live=False), metadata={"tags": [EXPORT_TAG]})
        at = next((i + 1 for i, c in enumerate(nb.cells) if "parameters" in _cell_tags(c)), 0)
        nb.cells.insert(at, injected)
        nb.cells.append(export)
        # first: tell verknode.notebook_inputs() in the parameters cell not to ask the workflow
        nb.cells.insert(0, nbformat.v4.new_code_cell(
            f"import os as _verknode_os\n_verknode_os.environ[{RUN_MARKER!r}] = '1'", metadata={"tags": [MARKER_TAG]}))
        kernel = s.get("kernel") or nb.metadata.get("kernelspec", {}).get("name") or "python3"
        client = NotebookClient(nb, timeout=s.get("timeout") or None, kernel_name=kernel,
                                resources={"metadata": {"path": os.path.dirname(path)}})
        failure = []

        def run():
            try:
                client.execute()
            except BaseException as e:  # noqa: BLE001 - reported below
                failure.append(e)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.2)
            if ctx.canceled():
                km = getattr(client, "km", None)
                if km is not None:
                    try:
                        km.shutdown_kernel(now=True)
                    except Exception:  # noqa: BLE001
                        pass
                worker.join(5)
                ctx.check_canceled()
        nb.cells = [c for c in nb.cells if not {EXPORT_TAG, MARKER_TAG} & set(_cell_tags(c))]
        self._report(nb, ctx, s, path)
        if failure:
            e = failure[0]
            if isinstance(e, CellExecutionError):
                raise ValueError(f"Notebook failed: {getattr(e, 'ename', '')}: {getattr(e, 'evalue', e)}") from None
            raise e

    def _report(self, nb, ctx, s, path, stream=True):
        """Node view (the executed notebook), printed output in the console, optional copy."""
        for cell in nb.cells if stream else []:
            for o in cell.get("outputs", []):
                if o.get("output_type") == "stream" and o.get("text"):
                    for line in o["text"].rstrip("\n").splitlines()[:200]:
                        ctx.log(line)
        try:
            from nbconvert import HTMLExporter

            html, _ = HTMLExporter(template_name="lab").from_notebook_node(nb)
            ctx.set_view(html)
        except Exception as e:  # noqa: BLE001 - the view is optional
            ctx.log(f"Could not render the notebook view: {e}", "WARNING")
        if s.get("save_copy"):
            import nbformat

            nbformat.write(nb, os.path.splitext(path)[0] + ".executed.ipynb")
