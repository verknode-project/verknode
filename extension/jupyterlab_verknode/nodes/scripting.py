"""Scripting nodes."""
from __future__ import annotations

import contextlib
import io

import polars as pl

from verknode.node import infer_type
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
        with contextlib.redirect_stdout(out):
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
