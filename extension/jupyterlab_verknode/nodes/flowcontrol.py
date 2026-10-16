"""Flow control: loops, switches and flow variable nodes."""
from __future__ import annotations

import math

import polars as pl

from verknode.node import VAR_TYPES, coerce
from verknode.node import F, FLOWVAR, FLOWVARIABLE, INACTIVE, InvalidSettingsError, NodeModel, Port, TABLE
from verknode.node import dtype_kind, json_value

LOOP_HELP = (
    " Pushes the flow variables <code>currentIteration</code> (starting at 0) and "
    "<code>maxIterations</code>. Use with a <i>Loop End</i> node; executing the Loop End "
    "runs the whole loop."
)


def _push_loop_vars(ctx, it, total):
    ctx.push_variable("maxIterations", int(total), "INTEGER")
    ctx.push_variable("currentIteration", int(it), "INTEGER")


VAR_DEFAULTS = {"STRING": "", "INTEGER": 0, "DOUBLE": 0.0, "BOOLEAN": False}


def _var_type_for(dtype) -> str:
    kind = dtype_kind(dtype)
    if kind in ("integer", "long"):
        return "INTEGER"
    if kind == "double":
        return "DOUBLE"
    if kind == "boolean":
        return "BOOLEAN"
    return "STRING"


def _push_row(ctx, df: pl.DataFrame, row: int):
    for name, t in df.schema.items():
        vtype = _var_type_for(t)
        value = df[name][row] if row < df.height else None
        if value is None:
            value = VAR_DEFAULTS[vtype]
        elif vtype == "STRING":
            value = str(json_value(value))
        ctx.push_variable(name, value, vtype)


# ----------------------------------------------------------------------
# Loop support
# ----------------------------------------------------------------------
class CountingLoopStart(NodeModel):
    factory = "verknode.flow.CountingLoopStart"
    closed_by = ["verknode.flow.LoopEnd", "verknode.flow.LoopEnd2Ports", "verknode.flow.LoopEndColumnAppend", "verknode.flow.VariableConditionLoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Counting Loop Start"
    category = "Flow Control/Loop Support"
    node_type = "LoopStart"
    icon = "⟳#"
    loop_role = "start"
    short_description = "Loops a fixed number of times."
    description = "Passes the input table into the loop body a fixed number of times." + LOOP_HELP
    in_ports = [Port(TABLE, "Any input table", optional=True)]
    out_ports = [Port(TABLE, "Input table (unchanged)")]
    fields = [F("loops", "int", "Number of loops", 10)]

    def execute(self, inputs, s, ctx):
        total = max(1, s["loops"])
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= total - 1
        ctx.loop["total"] = total  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, total)
        return [inputs[0] if inputs[0] is not None else pl.DataFrame()]


class ChunkLoopStart(NodeModel):
    factory = "verknode.flow.ChunkLoopStart"
    closed_by = ["verknode.flow.LoopEnd", "verknode.flow.LoopEnd2Ports", "verknode.flow.LoopEndColumnAppend", "verknode.flow.VariableConditionLoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Chunk Loop Start"
    category = "Flow Control/Loop Support"
    node_type = "LoopStart"
    icon = "⟳▤"
    loop_role = "start"
    short_description = "Loops over chunks of rows of the input table."
    description = "Splits the input table into consecutive chunks of rows, one chunk per iteration." + LOOP_HELP
    in_ports = [Port(TABLE, "Any input table")]
    out_ports = [Port(TABLE, "Chunked input")]
    fields = [
        F("mode", "enum", "Chunking", "Rows per chunk", options=["Rows per chunk", "Number of chunks"]),
        F("rows_per_chunk", "int", "Rows per chunk", 1, show_if={"mode": "Rows per chunk"}),
        F("chunks", "int", "Number of chunks", 1, show_if={"mode": "Number of chunks"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        h = df.height
        if s["mode"] == "Rows per chunk":
            size = max(1, s["rows_per_chunk"])
            total = max(1, math.ceil(h / size))
        else:
            total = max(1, min(s["chunks"], h) if h else 1)
            size = math.ceil(h / total) if h else 0
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= total - 1
        ctx.loop["total"] = total  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, total)
        return [df.slice(it * size, size)]


class TableRowToVariableLoopStart(NodeModel):
    factory = "verknode.flow.TableRowToVariableLoopStart"
    closed_by = ["verknode.flow.VariableLoopEnd", "verknode.flow.LoopEnd", "verknode.flow.VariableConditionLoopEnd"]
    name = "Table Row To Variable Loop Start"
    category = "Flow Control/Loop Support"
    node_type = "LoopStart"
    icon = "⟳V"
    loop_role = "start"
    short_description = "Loops over rows, exposing each row as flow variables."
    description = (
        "Iterates over the rows of the input table. In each iteration the values of the current "
        "row are pushed as flow variables named after the columns." + LOOP_HELP
    )
    in_ports = [Port(TABLE, "Parameters table")]
    out_ports = [Port(FLOWVARIABLE, "Variables connection")]
    fields = []

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        it = ctx.loop["iteration"]
        total = max(1, df.height)
        ctx.loop["last"] = it >= total - 1
        ctx.loop["total"] = total  # (the canvas shows "iteration / total")
        if df.height == 0:
            ctx.warn("Input table is empty; default values pushed")
        _push_row(ctx, df, it)
        _push_loop_vars(ctx, it, df.height)
        return [FLOWVAR]


class LoopEnd(NodeModel):
    factory = "verknode.flow.LoopEnd"
    name = "Loop End"
    category = "Flow Control/Loop Support"
    node_type = "LoopEnd"
    icon = "⟲"
    loop_role = "end"
    short_description = "Collects the results of all loop iterations."
    description = "Concatenates the tables arriving in each iteration. Executing this node runs the whole loop."
    in_ports = [Port(TABLE, "Collector table")]
    out_ports = [Port(TABLE, "Collected results")]
    fields = [
        F("add_iteration_column", "bool", "Add iteration column", True),
        F("ignore_empty", "bool", "Ignore empty input tables", True),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if s["add_iteration_column"]:
            df = df.with_columns(pl.lit(ctx.loop["iteration"], pl.Int32).alias("Iteration"))
        ctx.loop["collected"].append(df)
        if not ctx.loop["last"]:
            return None
        parts = [d for d in ctx.loop["collected"] if not (s["ignore_empty"] and d.height == 0)]
        if not parts:
            parts = [ctx.loop["collected"][0].clear()]
        ctx.log(f"Loop finished after {ctx.loop['iteration'] + 1} iterations")
        return [pl.concat(parts, how="diagonal_relaxed")]


class VariableLoopEnd(NodeModel):
    factory = "verknode.flow.VariableLoopEnd"
    name = "Variable Loop End"
    category = "Flow Control/Loop Support"
    node_type = "LoopEnd"
    icon = "⟲V"
    loop_role = "end"
    short_description = "Collects flow variable values of each iteration into a table."
    description = "Ends a loop whose body produces flow variables. Each iteration becomes one row with the selected variables."
    in_ports = [Port(FLOWVARIABLE, "Variables connection")]
    out_ports = [Port(TABLE, "Variable values")]
    fields = [F("variables", "varlist", "Collected variables", {"include": [], "exclude": [], "enforce": "inclusion"})]

    def execute(self, inputs, s, ctx):
        stack = {**ctx.stack}
        sel = s["variables"] or {}
        names = [n for n in stack if n in set(sel.get("include", []))] if sel.get("enforce") != "exclusion" \
            else [n for n in stack if n not in set(sel.get("exclude", []))]
        ctx.loop["collected"].append({n: stack[n] for n in names})
        if not ctx.loop["last"]:
            return None
        rows = ctx.loop["collected"]
        cols = {}
        for n in names:
            vtype = rows[-1][n].type
            dtype = {"STRING": pl.String, "INTEGER": pl.Int64, "DOUBLE": pl.Float64, "BOOLEAN": pl.Boolean}[vtype]
            cols[n] = pl.Series(n, [coerce(r[n].value, vtype) if n in r else None for r in rows], dtype=dtype)
        return [pl.DataFrame(cols)]


# ----------------------------------------------------------------------
# Switches
# ----------------------------------------------------------------------
class IfSwitch(NodeModel):
    factory = "verknode.flow.IfSwitch"
    closed_by = ["verknode.flow.EndIf"]
    name = "IF Switch"
    category = "Flow Control/Switches"
    node_type = "ScopeStart"
    icon = "IF"
    short_description = "Activates the top, bottom or both output branches."
    description = (
        "Passes the input table to the active output port(s). Nodes on inactive branches are not "
        "executed. Control the choice with a flow variable (values: top, bottom, both) via the "
        "Flow Variables tab. Close the branches with <i>End IF</i>."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Top"), Port(TABLE, "Bottom")]
    fields = [F("active", "enum", "Active port", "top", options=["top", "bottom", "both"])]

    def execute(self, inputs, s, ctx):
        choice = (s["active"] or "top").strip().lower()
        if choice not in ("top", "bottom", "both"):
            raise InvalidSettingsError(f"Invalid port choice: {s['active']!r} (use top, bottom or both)")
        df = inputs[0]
        return [df if choice in ("top", "both") else INACTIVE,
                df if choice in ("bottom", "both") else INACTIVE]


class EndIf(NodeModel):
    factory = "verknode.flow.EndIf"
    name = "End IF"
    category = "Flow Control/Switches"
    node_type = "ScopeEnd"
    icon = "END"
    handles_inactive = True
    short_description = "Merges the branches of an IF Switch."
    description = "Outputs the active input. If both inputs are active their rows are concatenated."
    in_ports = [Port(TABLE, "First input"), Port(TABLE, "Second input", optional=True)]
    out_ports = [Port(TABLE, "Output of the active branch")]
    fields = []

    def execute(self, inputs, s, ctx):
        active = [t for t in inputs if t is not None and t is not INACTIVE]
        if not active:
            return [INACTIVE]
        return [active[0] if len(active) == 1 else pl.concat(active, how="diagonal_relaxed")]


# ----------------------------------------------------------------------
# Flow variables
# ----------------------------------------------------------------------
class VariableCreator(NodeModel):
    factory = "verknode.flow.VariableCreator"
    name = "Variable Creator"
    category = "Flow Control/Flow Variable"
    node_type = "Other"
    icon = "V+"
    short_description = "Creates flow variables."
    description = "Defines new flow variables with a type and value."
    out_ports = [Port(FLOWVARIABLE, "Created variables")]
    fields = [
        F("variables", "records", "Variables", [{"name": "var1", "type": "STRING", "value": ""}], fields=[
            F("name", "string", "Name", ""),
            F("type", "enum", "Type", "STRING", options=list(VAR_TYPES)),
            F("value", "string", "Value", ""),
        ]),
    ]

    def execute(self, inputs, s, ctx):
        seen = set()
        for r in s["variables"]:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            if name in seen:
                raise InvalidSettingsError(f"Duplicate variable name: {name}")
            seen.add(name)
            try:
                ctx.push_variable(name, coerce(r.get("value", ""), r["type"]), r["type"])
            except (TypeError, ValueError):
                raise InvalidSettingsError(f'Invalid {r["type"]} value for "{name}": {r.get("value")!r}') from None
        return [FLOWVAR]


class TableRowToVariable(NodeModel):
    factory = "verknode.flow.TableRowToVariable"
    name = "Table Row to Variable"
    category = "Flow Control/Flow Variable"
    node_type = "Other"
    icon = "▤→V"
    short_description = "Converts the first row of a table into flow variables."
    description = "Each column of the first row becomes a flow variable named after the column."
    in_ports = [Port(TABLE, "Parameters table")]
    out_ports = [Port(FLOWVARIABLE, "Variables connection")]
    fields = []

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if df.height == 0:
            ctx.warn("Input table is empty; default values pushed")
        _push_row(ctx, df, 0)
        return [FLOWVAR]


class VariableToTableRow(NodeModel):
    factory = "verknode.flow.VariableToTableRow"
    name = "Variable to Table Row"
    category = "Flow Control/Flow Variable"
    node_type = "Other"
    icon = "V→▤"
    short_description = "Converts flow variables into a one-row table."
    description = "Creates a table with one row holding the values of the selected flow variables."
    in_ports = [Port(FLOWVARIABLE, "Variables connection")]
    out_ports = [Port(TABLE, "Variables as table")]
    fields = [F("variables", "varlist", "Variables", {"include": [], "exclude": [], "enforce": "exclusion"})]

    def execute(self, inputs, s, ctx):
        sel = s["variables"] or {}
        stack = ctx.stack
        if sel.get("enforce") == "exclusion":
            names = [n for n in reversed(list(stack)) if n not in set(sel.get("exclude", []))]
        else:
            names = [n for n in reversed(list(stack)) if n in set(sel.get("include", []))]
        dtypes = {"STRING": pl.String, "INTEGER": pl.Int64, "DOUBLE": pl.Float64, "BOOLEAN": pl.Boolean}
        return [pl.DataFrame({n: pl.Series(n, [stack[n].value], dtype=dtypes[stack[n].type]) for n in names})]


class MergeVariables(NodeModel):
    factory = "verknode.flow.MergeVariables"
    name = "Merge Variables"
    category = "Flow Control/Flow Variable"
    node_type = "Other"
    icon = "V∪V"
    short_description = "Merges flow variables from two branches."
    description = "Merges the flow variable stacks of both inputs; on name conflicts the top input wins."
    in_ports = [Port(FLOWVARIABLE, "First variables"), Port(FLOWVARIABLE, "Second variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Merged variables")]
    fields = []

    def execute(self, inputs, s, ctx):
        return [FLOWVAR]
