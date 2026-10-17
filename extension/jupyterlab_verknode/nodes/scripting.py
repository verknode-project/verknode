"""Scripting nodes."""
from __future__ import annotations

import io

import polars as pl

from verknode.node import capture_stdout, infer_type
from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

DEFAULT_SCRIPT = '''\
# Available variables:
#   input_table_1   polars.DataFrame (None if the input port is not connected)
#   flow_variables  dict of flow variables; new/changed entries are pushed downstream
#   pl              the polars module
#   chem            RDKit helpers: chem.to_mols(series), chem.mol_series(name, mols)
#   Chem            rdkit.Chem
#   set_progress    set_progress(0.5): how far the script is (0..1), shown on the canvas
# Assign the result (polars or pandas DataFrame) to output_table_1.

output_table_1 = input_table_1
'''


class PythonScript(NodeModel):
    factory = "verknode.scripting.PythonScript"
    name = "Python Script"
    category = "Scripting/Python"
    node_type = "Manipulator"
    icon = "Py"
    short_description = "Executes a Python script on the input table."
    description = (
        "Runs Python code in the workflow engine. The input table is available as "
        "<code>input_table_1</code> (polars), flow variables as <code>flow_variables</code>. "
        "Assign the output to <code>output_table_1</code>. Output printed with print() appears in the console. "
        "Long scripts can call <code>set_progress(fraction)</code> (0..1): the canvas shows it and the ETA follows it."
    )
    in_ports = [Port(TABLE, "Input table 1", "Available as input_table_1", optional=True)]
    out_ports = [Port(TABLE, "Output table 1", "Value of output_table_1")]
    fields = [F("script", "code", "Script", DEFAULT_SCRIPT, language="python")]

    def configure(self, in_specs, s, ctx):
        if not (s["script"] or "").strip():
            raise InvalidSettingsError("Script is empty")
        return [None]  # output columns are only known after execution

    def execute(self, inputs, s, ctx):
        before = ctx.variables()
        ns = {
            "pl": pl,
            "input_table_1": inputs[0],
            "flow_variables": dict(before),
            "output_table_1": None,
            "set_progress": ctx.set_progress,
        }
        try:
            from rdkit import Chem

            from verknode.node import chem

            ns.update(chem=chem, Chem=Chem)
        except ImportError:
            pass
        out = io.StringIO()
        with capture_stdout(out):
            exec(compile(s["script"], "<Python Script>", "exec"), ns)  # noqa: S102
        for line in out.getvalue().splitlines():
            ctx.log(line)
        result = ns.get("output_table_1")
        if result is None:
            raise ValueError("output_table_1 was not assigned")
        for name, value in ns.get("flow_variables", {}).items():
            if not isinstance(value, (str, int, float, bool)):
                continue
            if name not in before or before[name] != value:
                ctx.push_variable(name, value, infer_type(value))
        return [result]


WORKFLOW_SCRIPT = """# The workflow this node runs in: `workflow`. Reading is immediate; edits are
# applied when the run ends (in order, as one undo step).
#
#   workflow.me / workflow.find("label") / workflow.find_all("prefix") / workflow.node(id)
#       node.label, .name, .settings, .x, .y, .executed, .inputs(), .outputs()
#   workflow.add_node("Row Filter", x, y, label="...", column="a", operator=">", value="3")
#       (or settings={...}: for keys such as "x")
#   workflow.copy([n1, n2], dx=0, dy=150, labels={"old": "new"})   -> the copies
#   workflow.connect(a, b, src_port=1, dst_port=1)   (port 0: flow variables)
#   workflow.disconnect(a, b) / workflow.remove(n) / workflow.move(n, x, y) / workflow.label(n, "...")
#   workflow.configure(n, setting=value, ...)       workflow.widget(n, side="free", x=..., w=...)
#   workflow.group(n1, n2, text="...")              workflow.execute(n, ...)  (after the edits)
#
# Also: input_table_1, flow_variables, pl; output_table_1 defaults to the input table.

print("this node:", workflow.me, "- the workflow has", len(workflow.nodes()), "nodes")
"""


class WorkflowScript(PythonScript):
    factory = "verknode.scripting.WorkflowScript"
    name = "Workflow Script"
    icon = "Py⚙"
    node_type = "Other"
    short_description = "Runs a Python script that changes the workflow it is in."
    description = (
        "Runs Python code with <code>workflow</code>: the workflow this node is in. The script can read "
        "it (nodes by label, their settings and connections) and change it - add, copy, remove, connect, "
        "disconnect and configure nodes, move them, place widgets, add group annotations - and ask to "
        "execute nodes afterwards. Edits are applied when the run ends, in order, as one undo step (the "
        "running workflow does not change under the nodes); a run that a Workflow Script started does not "
        "apply further script edits, so scripts cannot loop. Changing a node upstream of this one resets "
        "this node too. Like the Python Script node it has <code>input_table_1</code>, "
        "<code>flow_variables</code> (changes are passed on) and <code>output_table_1</code>, which "
        "defaults to the input table."
    )
    fields = [F("script", "code", "Script", WORKFLOW_SCRIPT, language="python")]

    def execute(self, inputs, s, ctx):
        before = ctx.variables()
        passthrough = inputs[0] if inputs[0] is not None else pl.DataFrame()
        ns = {
            "pl": pl,
            "input_table_1": inputs[0],
            "flow_variables": dict(before),
            "output_table_1": passthrough,
            "set_progress": ctx.set_progress,
            "workflow": ctx.script_workflow() if ctx.script_workflow else None,
        }
        out = io.StringIO()
        with capture_stdout(out):
            exec(compile(s["script"], "<Workflow Script>", "exec"), ns)  # noqa: S102
        for line in out.getvalue().splitlines():
            ctx.log(line)
        for name, value in ns.get("flow_variables", {}).items():
            if isinstance(value, (str, int, float, bool)) and (name not in before or before[name] != value):
                ctx.push_variable(name, value, infer_type(value))
        result = ns.get("output_table_1")
        return [result if result is not None else passthrough]
