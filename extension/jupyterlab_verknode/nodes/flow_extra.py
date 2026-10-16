"""More flow control nodes: loops, CASE switches, error handling, variables, Wait."""
from __future__ import annotations

import datetime as dt
import getpass
import math
import os
import platform
import socket
import sys
import tempfile
import time

import polars as pl

from verknode.node import infer_type
from verknode.node import (
    F,
    FLOWVAR,
    FLOWVARIABLE,
    INACTIVE,
    InvalidSettingsError,
    NodeModel,
    Port,
    TABLE,
    require_column,
)
from verknode.node import compile_rules, rules_expression
from .flowcontrol import LOOP_HELP, VAR_DEFAULTS, _push_loop_vars, _var_type_for
from .io import PATH_HELP
from .manipulation import selected_columns

LOOPS = "Flow Control/Loop Support"
SWITCHES = "Flow Control/Switches"
ERRORS = "Flow Control/Error Handling"
VARIABLES = "Flow Control/Flow Variable"
VAR_DTYPES = {"STRING": pl.String, "INTEGER": pl.Int64, "DOUBLE": pl.Float64, "BOOLEAN": pl.Boolean}


def _active(obj) -> bool:
    return obj is not None and obj is not INACTIVE


def _state(ctx) -> dict:
    return ctx.loop.setdefault("state", {})


def _with_iteration(df: pl.DataFrame, ctx, add: bool) -> pl.DataFrame:
    if add:
        return df.with_columns(pl.lit(ctx.loop["iteration"], pl.Int32).alias("Iteration"))
    return df


def _concat_collected(parts: list, ignore_empty: bool) -> pl.DataFrame:
    keep = [d for d in parts if not (ignore_empty and d.height == 0)]
    return pl.concat(keep or [parts[0].clear()], how="diagonal_relaxed")


def _compare(value, op: str, text: str) -> bool:
    """Compare a flow variable value with a dialog value (typed by the variable)."""
    if isinstance(value, bool):
        other = text.strip().lower() in ("true", "1", "yes")
    elif isinstance(value, (int, float)):
        try:
            other = float(text)
        except ValueError:
            raise InvalidSettingsError(f"Value {text!r} is not a number") from None
    else:
        other = text
    return {
        "=": value == other,
        "!=": value != other,
        "<": value < other,
        "<=": value <= other,
        ">": value > other,
        ">=": value >= other,
    }[op]


def _vars_selection(sel, stack: dict) -> list:
    sel = sel or {}
    names = list(reversed(list(stack)))
    if sel.get("enforce") == "exclusion":
        excl = set(sel.get("exclude", []))
        return [n for n in names if n not in excl]
    incl = set(sel.get("include", []))
    return [n for n in names if n in incl]


# ----------------------------------------------------------------------
# Loop starts
# ----------------------------------------------------------------------
class GroupLoopStart(NodeModel):
    factory = "verknode.flow.GroupLoopStart"
    closed_by = ["verknode.flow.LoopEnd", "verknode.flow.LoopEnd2Ports", "verknode.flow.LoopEndColumnAppend", "verknode.flow.VariableConditionLoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Group Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳G"
    loop_role = "start"
    short_description = "Loops over groups of rows."
    description = (
        "Each iteration outputs the rows of one group, defined by the distinct value combinations "
        "of the group columns (in order of first appearance)." + LOOP_HELP
    )
    in_ports = [Port(TABLE, "Any input table")]
    out_ports = [Port(TABLE, "Grouped input")]
    fields = [F("groups", "columns", "Group columns", {"include": [], "exclude": [], "enforce": "inclusion"})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["groups"], df.schema)
        if not cols:
            raise InvalidSettingsError("No group columns selected")
        keys = df.select(cols).unique(maintain_order=True)
        total = max(1, keys.height)
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= total - 1
        ctx.loop["total"] = total  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, keys.height)
        if keys.height == 0:
            return [df]
        row = keys.row(it, named=True)
        cond = pl.lit(True)
        for c in cols:
            cond = cond & pl.col(c).eq_missing(pl.lit(row[c], dtype=df.schema[c]))
        return [df.filter(cond)]


class GenericLoopStart(NodeModel):
    factory = "verknode.flow.GenericLoopStart"
    closed_by = ["verknode.flow.VariableConditionLoopEnd", "verknode.flow.LoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Generic Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳"
    loop_role = "start"
    short_description = "Starts a loop whose end is decided by the loop end node."
    description = (
        "Passes the input table into the loop body in every iteration and pushes "
        "<code>currentIteration</code>. Use with a <i>Variable Condition Loop End</i>, which decides "
        "when the loop stops."
    )
    in_ports = [Port(TABLE, "Any input table", optional=True)]
    out_ports = [Port(TABLE, "Input table (unchanged)")]
    fields = []

    def execute(self, inputs, s, ctx):
        ctx.loop["last"] = False
        ctx.push_variable("currentIteration", int(ctx.loop["iteration"]), "INTEGER")
        return [inputs[0] if inputs[0] is not None else pl.DataFrame()]


class IntervalLoopStart(NodeModel):
    factory = "verknode.flow.IntervalLoopStart"
    closed_by = ["verknode.flow.LoopEnd", "verknode.flow.LoopEnd2Ports", "verknode.flow.LoopEndColumnAppend", "verknode.flow.VariableConditionLoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Interval Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳i"
    loop_role = "start"
    short_description = "Loops over an interval of numbers."
    description = (
        "Iterates from <i>From</i> to <i>To</i> (inclusive) in increments of <i>Step</i> and pushes the "
        "current value as a flow variable (default <code>loop_value</code>)." + LOOP_HELP
    )
    in_ports = [Port(TABLE, "Any input table", optional=True)]
    out_ports = [Port(TABLE, "Input table (unchanged)")]
    fields = [
        F("start", "float", "From", 0.0),
        F("end", "float", "To", 1.0),
        F("step", "float", "Step", 0.1, "Negative for descending intervals."),
        F("type", "enum", "Loop variable type", "Double", options=["Double", "Integer"]),
        F("variable", "string", "Loop variable name", "loop_value"),
    ]

    def _values(self, s):
        start, end, step = s["start"], s["end"], s["step"]
        if step == 0:
            raise InvalidSettingsError("Step must not be 0")
        if (end - start) * step < 0:
            raise InvalidSettingsError("Step moves away from the end of the interval")
        n = int(math.floor((end - start) / step + 1e-9)) + 1
        values = [start + i * step for i in range(n)]
        if s["type"] == "Integer":
            values = [int(round(v)) for v in values]
        return values

    def validate(self, s, in_specs):
        self._values(s)

    def execute(self, inputs, s, ctx):
        values = self._values(s)
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= len(values) - 1
        ctx.loop["total"] = len(values)  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, len(values))
        ctx.push_variable(s["variable"] or "loop_value", values[min(it, len(values) - 1)],
                          "INTEGER" if s["type"] == "Integer" else "DOUBLE")
        return [inputs[0] if inputs[0] is not None else pl.DataFrame()]


class ColumnListLoopStart(NodeModel):
    factory = "verknode.flow.ColumnListLoopStart"
    closed_by = ["verknode.flow.LoopEndColumnAppend", "verknode.flow.LoopEnd"]
    name = "Column List Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳▥"
    loop_role = "start"
    short_description = "Loops over a list of columns."
    description = (
        "Each iteration outputs one of the selected columns (and, optionally, all columns that are not "
        "iterated over). Pushes <code>currentColumnName</code>. Combine with <i>Loop End (Column "
        "Append)</i> to process columns one by one." + LOOP_HELP
    )
    in_ports = [Port(TABLE, "Any input table")]
    out_ports = [Port(TABLE, "Table with the current column")]
    fields = [
        F("columns", "columns", "Columns to iterate over", {"include": [], "exclude": [], "enforce": "exclusion"}),
        F("keep_others", "bool", "Include columns not iterated over", True),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= len(cols) - 1
        ctx.loop["total"] = len(cols)  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, len(cols))
        current = cols[min(it, len(cols) - 1)]
        ctx.push_variable("currentColumnName", current, "STRING")
        keep = [c for c in df.columns if c == current or (s["keep_others"] and c not in cols)]
        return [df.select(keep)]


class WindowLoopStart(NodeModel):
    factory = "verknode.flow.WindowLoopStart"
    closed_by = ["verknode.flow.LoopEnd", "verknode.flow.LoopEnd2Ports", "verknode.flow.LoopEndColumnAppend", "verknode.flow.VariableConditionLoopEnd", "verknode.flow.VariableLoopEnd"]
    name = "Window Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳⊟"
    loop_role = "start"
    short_description = "Loops over sliding windows of rows."
    description = (
        "Each iteration outputs a window of consecutive rows. Windows move by <i>Step size</i> rows. "
        "<b>Forward</b>: the window starts at the current row, <b>Central</b>: it is centered on it, "
        "<b>Backward</b>: it ends at it. With <i>Limit window to table</i> only complete windows are "
        "used." + LOOP_HELP
    )
    in_ports = [Port(TABLE, "Any input table")]
    out_ports = [Port(TABLE, "Window")]
    fields = [
        F("window", "int", "Window size", 10),
        F("step", "int", "Step size", 1),
        F("mode", "enum", "Window definition", "Forward", options=["Forward", "Central", "Backward"]),
        F("limit", "bool", "Limit window to table", False),
    ]

    def _windows(self, h, s):
        size, step = max(1, s["window"]), max(1, s["step"])
        out = []
        for c in range(0, h, step):
            if s["mode"] == "Forward":
                lo = c
            elif s["mode"] == "Central":
                lo = c - (size - 1) // 2
            else:
                lo = c - size + 1
            hi = lo + size
            if s["limit"] and (lo < 0 or hi > h):
                continue
            out.append((max(0, lo), min(h, hi)))
        return out

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        windows = self._windows(df.height, s)
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= len(windows) - 1
        ctx.loop["total"] = len(windows)  # (the canvas shows "iteration / total")
        _push_loop_vars(ctx, it, len(windows))
        if not windows:
            return [df.clear()]
        lo, hi = windows[min(it, len(windows) - 1)]
        return [df.slice(lo, hi - lo)]


class RecursiveLoopStart(NodeModel):
    factory = "verknode.flow.RecursiveLoopStart"
    closed_by = ["verknode.flow.RecursiveLoopEnd"]
    name = "Recursive Loop Start"
    category = LOOPS
    node_type = "LoopStart"
    icon = "⟳↺"
    loop_role = "start"
    short_description = "Starts a loop that feeds its result back into the next iteration."
    description = (
        "In the first iteration the input table is passed on; in later iterations the table "
        "arriving at the second port of the <i>Recursive Loop End</i> is used. Pushes "
        "<code>currentIteration</code>."
    )
    in_ports = [Port(TABLE, "Initial table")]
    out_ports = [Port(TABLE, "Recursion table")]
    fields = []

    def execute(self, inputs, s, ctx):
        ctx.loop["last"] = False
        it = ctx.loop["iteration"]
        ctx.push_variable("currentIteration", int(it), "INTEGER")
        fed_back = _state(ctx).get("recursion")
        return [inputs[0] if it == 0 or fed_back is None else fed_back]


# ----------------------------------------------------------------------
# Loop ends
# ----------------------------------------------------------------------
class RecursiveLoopEnd(NodeModel):
    factory = "verknode.flow.RecursiveLoopEnd"
    name = "Recursive Loop End"
    category = LOOPS
    node_type = "LoopEnd"
    icon = "⟲↺"
    loop_role = "end"
    short_description = "Ends a recursive loop; feeds the second input back to the loop start."
    description = (
        "The table at the first port is collected; the table at the second port is passed back to the "
        "<i>Recursive Loop Start</i> for the next iteration. The loop stops after the maximum number of "
        "iterations, when the recursion table has fewer than the minimal number of rows, or when the "
        "end-loop flow variable is \"true\"."
    )
    in_ports = [Port(TABLE, "Collecting data"), Port(TABLE, "Recursion data")]
    out_ports = [Port(TABLE, "Collected data")]
    fields = [
        F("max_iterations", "int", "Maximal number of iterations", 100),
        F("min_rows", "int", "Minimal number of rows", 1, "Stop when the recursion table has fewer rows."),
        F("end_variable", "string", "End loop with variable (optional)", "",
          "Stop when this flow variable is \"true\"."),
        F("last_only", "bool", "Collect data from last iteration only", False),
        F("add_iteration_column", "bool", "Add iteration column", False),
    ]

    def execute(self, inputs, s, ctx):
        collect, recursion = inputs
        it = ctx.loop["iteration"]
        stop = it + 1 >= max(1, s["max_iterations"]) or recursion.height < s["min_rows"]
        if s["end_variable"]:
            var = ctx.variable(s["end_variable"])
            if var is not None and str(var.value).strip().lower() == "true":
                stop = True
        collect = _with_iteration(collect, ctx, s["add_iteration_column"])
        if s["last_only"]:
            ctx.loop["collected"] = [collect]
        else:
            ctx.loop["collected"].append(collect)
        _state(ctx)["recursion"] = recursion
        if not (stop or ctx.loop["last"]):
            return None
        ctx.loop["last"] = True
        ctx.log(f"Recursion finished after {it + 1} iterations")
        return [_concat_collected(ctx.loop["collected"], False)]


OPS = ["=", "!=", "<", "<=", ">", ">="]


class VariableConditionLoopEnd(NodeModel):
    factory = "verknode.flow.VariableConditionLoopEnd"
    name = "Variable Condition Loop End"
    category = LOOPS
    node_type = "LoopEnd"
    icon = "⟲?"
    loop_role = "end"
    short_description = "Ends a loop when a flow variable meets a condition."
    description = (
        "Collects the input rows of each iteration and stops the loop as soon as the selected flow "
        "variable fulfills the condition (or the loop start has no more iterations). The second "
        "output holds the variable's value per iteration."
    )
    in_ports = [Port(TABLE, "Collector table")]
    out_ports = [Port(TABLE, "Collected rows"), Port(TABLE, "Variable values")]
    fields = [
        F("variable", "variable", "Flow variable", "", "The flow variable to check."),
        F("operator", "enum", "Finish loop if variable is", "=", options=OPS),
        F("value", "string", "Value", ""),
        F("last_only", "bool", "Collect rows from last iteration only", False),
        F("add_iteration_column", "bool", "Add iteration column", True),
    ]

    def validate(self, s, in_specs):
        if not (s["variable"] or "").strip():
            raise InvalidSettingsError("No flow variable selected")

    def execute(self, inputs, s, ctx):
        df = _with_iteration(inputs[0], ctx, s["add_iteration_column"])
        var = ctx.variable(s["variable"])
        if var is None and not ctx.dry:
            raise InvalidSettingsError(f'Flow variable "{s["variable"]}" not found')
        vtype = var.type if var is not None else "STRING"
        if s["last_only"]:
            ctx.loop["collected"] = [df]
        else:
            ctx.loop["collected"].append(df)
        values = _state(ctx).setdefault("values", [])
        values.append((ctx.loop["iteration"], var.value if var is not None else None))
        done = ctx.loop["last"] or _compare(var.value, s["operator"], s["value"] or "")
        if not done:
            return None
        ctx.loop["last"] = True
        table = pl.DataFrame({
            "Iteration": pl.Series([i for i, _ in values], dtype=pl.Int32),
            s["variable"]: pl.Series([v for _, v in values], dtype=VAR_DTYPES[vtype]),
        })
        return [_concat_collected(ctx.loop["collected"], False), table]


class LoopEnd2Ports(NodeModel):
    factory = "verknode.flow.LoopEnd2Ports"
    name = "Loop End (2 ports)"
    category = LOOPS
    node_type = "LoopEnd"
    icon = "⟲²"
    loop_role = "end"
    short_description = "Collects two tables over all loop iterations."
    description = "Like <i>Loop End</i>, but collects and concatenates the tables of two input ports separately."
    in_ports = [Port(TABLE, "Collector table 1"), Port(TABLE, "Collector table 2")]
    out_ports = [Port(TABLE, "Collected results 1"), Port(TABLE, "Collected results 2")]
    fields = [
        F("add_iteration_column", "bool", "Add iteration column", True),
        F("ignore_empty", "bool", "Ignore empty input tables", True),
    ]

    def execute(self, inputs, s, ctx):
        ctx.loop["collected"].append([_with_iteration(d, ctx, s["add_iteration_column"]) for d in inputs])
        if not ctx.loop["last"]:
            return None
        parts = ctx.loop["collected"]
        return [_concat_collected([p[i] for p in parts], s["ignore_empty"]) for i in range(2)]


class LoopEndColumnAppend(NodeModel):
    factory = "verknode.flow.LoopEndColumnAppend"
    name = "Loop End (Column Append)"
    category = LOOPS
    node_type = "LoopEnd"
    icon = "⟲▥"
    loop_role = "end"
    short_description = "Appends the columns of all loop iterations side by side."
    description = (
        "Joins the tables of all iterations column-wise (they must have the same number of rows). "
        "Columns that are identical to an already collected column are added only once; other "
        "duplicate names get the suffix <code>(Iter #n)</code>."
    )
    in_ports = [Port(TABLE, "Collector table")]
    out_ports = [Port(TABLE, "Appended columns")]
    fields = []

    def execute(self, inputs, s, ctx):
        ctx.loop["collected"].append(inputs[0])
        if not ctx.loop["last"]:
            return None
        parts = ctx.loop["collected"]
        out = parts[0]
        for i, d in enumerate(parts[1:], start=1):
            if d.height != out.height:
                raise ValueError(f"Iteration {i} has {d.height} rows, expected {out.height}")
            new = []
            for c in d.columns:
                if c in out.columns:
                    if out[c].equals(d[c]):
                        continue
                    new.append(d[c].alias(f"{c} (Iter #{i})"))
                else:
                    new.append(d[c])
            if new:
                out = out.hstack(new)
        return [out]


# ----------------------------------------------------------------------
# Switches
# ----------------------------------------------------------------------
class CaseSwitchDataStart(NodeModel):
    factory = "verknode.flow.CaseSwitchDataStart"
    closed_by = ["verknode.flow.CaseSwitchDataEnd"]
    name = "CASE Switch Data (Start)"
    category = SWITCHES
    node_type = "ScopeStart"
    icon = "CASE"
    short_description = "Activates one of three output branches."
    description = (
        "Passes the input table to the selected output port (0, 1 or 2); the other branches become "
        "inactive. Control the port with a flow variable (e.g. <code>PortIndex</code>) in the Flow "
        "Variables tab. Close the branches with <i>CASE Switch Data (End)</i>."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Port 0"), Port(TABLE, "Port 1"), Port(TABLE, "Port 2")]
    fields = [F("active", "enum", "Select active port", "0", options=["0", "1", "2"])]

    def execute(self, inputs, s, ctx):
        choice = str(s["active"]).strip()
        if choice not in ("0", "1", "2"):
            raise InvalidSettingsError(f"Invalid port index: {s['active']!r} (use 0, 1 or 2)")
        return [inputs[0] if str(i) == choice else INACTIVE for i in range(3)]


MULTIPLE = ["Fail", "Use first non-inactive input", "Concatenate"]


class CaseSwitchDataEnd(NodeModel):
    factory = "verknode.flow.CaseSwitchDataEnd"
    name = "CASE Switch Data (End)"
    category = SWITCHES
    node_type = "ScopeEnd"
    icon = "END"
    handles_inactive = True
    short_description = "Merges the branches of a CASE switch."
    description = "Outputs the active input. Choose what happens if more than one input is active."
    in_ports = [Port(TABLE, "Input 0"), Port(TABLE, "Input 1", optional=True), Port(TABLE, "Input 2", optional=True)]
    out_ports = [Port(TABLE, "Output of the active branch")]
    fields = [F("multiple", "enum", "If multiple inputs are active", "Fail", options=MULTIPLE)]

    def execute(self, inputs, s, ctx):
        active = [t for t in inputs if _active(t)]
        if not active:
            return [INACTIVE]
        if len(active) > 1:
            if s["multiple"] == "Fail":
                raise ValueError("More than one input is active")
            if s["multiple"] == "Concatenate":
                return [pl.concat(active, how="diagonal_relaxed")]
        return [active[0]]


class CaseSwitchVariableStart(NodeModel):
    factory = "verknode.flow.CaseSwitchVariableStart"
    closed_by = ["verknode.flow.CaseSwitchVariableEnd"]
    name = "CASE Switch Variable (Start)"
    category = SWITCHES
    node_type = "ScopeStart"
    icon = "CASE"
    short_description = "Activates one of three flow variable branches."
    description = "Like <i>CASE Switch Data (Start)</i>, for flow variable connections."
    in_ports = [Port(FLOWVARIABLE, "Variables")]
    out_ports = [Port(FLOWVARIABLE, "Port 0"), Port(FLOWVARIABLE, "Port 1"), Port(FLOWVARIABLE, "Port 2")]
    fields = [F("active", "enum", "Select active port", "0", options=["0", "1", "2"])]

    def execute(self, inputs, s, ctx):
        choice = str(s["active"]).strip()
        if choice not in ("0", "1", "2"):
            raise InvalidSettingsError(f"Invalid port index: {s['active']!r} (use 0, 1 or 2)")
        return [FLOWVAR if str(i) == choice else INACTIVE for i in range(3)]


class CaseSwitchVariableEnd(NodeModel):
    factory = "verknode.flow.CaseSwitchVariableEnd"
    name = "CASE Switch Variable (End)"
    category = SWITCHES
    node_type = "ScopeEnd"
    icon = "END"
    handles_inactive = True
    short_description = "Merges the flow variable branches of a CASE switch."
    description = "Outputs the flow variables of the active branch(es); inactive if no branch is active."
    in_ports = [Port(FLOWVARIABLE, "Input 0"), Port(FLOWVARIABLE, "Input 1", optional=True),
                Port(FLOWVARIABLE, "Input 2", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Variables of the active branch")]
    fields = []

    def execute(self, inputs, s, ctx):
        return [FLOWVAR if any(_active(t) for t in inputs) else INACTIVE]


class EmptyTableSwitch(NodeModel):
    factory = "verknode.flow.EmptyTableSwitch"
    name = "Empty Table Switch"
    category = SWITCHES
    node_type = "ScopeStart"
    icon = "∅?"
    short_description = "Activates the top branch for non-empty tables, the bottom one otherwise."
    description = (
        "If the input table has rows it is passed to the top port and the bottom port is inactive. "
        "If it is empty, the top port is inactive and the (empty) table goes to the bottom port. "
        "Close the branches with <i>End IF</i>."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Non-empty table"), Port(TABLE, "Empty table")]
    fields = []

    def configure(self, in_specs, s, ctx):
        # both branches are active while configuring, so downstream nodes can be set up
        return [in_specs[0], in_specs[0]]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        return [df, INACTIVE] if df.height > 0 else [INACTIVE, df]


# ----------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------
ERROR_HELP = (
    " Pushes the flow variables <code>FailingNode</code>, <code>FailingNodeMessage</code> and "
    "<code>FailingNodeStackTrace</code> when a node in the try scope failed."
)


def _error_fields():
    return [
        F("always_populate", "bool", "Always populate error variables", False,
          "Push the error variables with the default values below even if no node failed."),
        F("default_node", "string", "Default FailingNode", "none", show_if={"always_populate": True}),
        F("default_message", "string", "Default FailingNodeMessage", "none", show_if={"always_populate": True}),
        F("default_trace", "string", "Default FailingNodeStackTrace", "none", show_if={"always_populate": True}),
    ]


def _push_error_vars(ctx, s):
    err = ctx.caught_error
    if err:
        ctx.push_variable("FailingNode", err.get("node", ""), "STRING")
        ctx.push_variable("FailingNodeMessage", err.get("message", ""), "STRING")
        ctx.push_variable("FailingNodeStackTrace", err.get("trace", err.get("message", "")), "STRING")
        ctx.warn(f"Caught error in {err.get('node')}: {err.get('message')}")
    elif s["always_populate"]:
        ctx.push_variable("FailingNode", s["default_node"], "STRING")
        ctx.push_variable("FailingNodeMessage", s["default_message"], "STRING")
        ctx.push_variable("FailingNodeStackTrace", s["default_trace"], "STRING")


class TryDataPorts(NodeModel):
    factory = "verknode.flow.TryDataPorts"
    closed_by = ["verknode.flow.CatchErrorsDataPorts"]
    name = "Try (Data Ports)"
    category = ERRORS
    node_type = "ScopeStart"
    icon = "TRY"
    scope_role = "try"
    short_description = "Starts a try-catch scope."
    description = (
        "Marks the start of a try scope. If a node between this node and the matching "
        "<i>Catch Errors</i> node fails, the Catch node outputs its alternative input instead."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Input table (unchanged)")]
    fields = []

    def execute(self, inputs, s, ctx):
        return [inputs[0]]


class TryVariablePorts(NodeModel):
    factory = "verknode.flow.TryVariablePorts"
    closed_by = ["verknode.flow.CatchErrorsVarPorts"]
    name = "Try (Variable Ports)"
    category = ERRORS
    node_type = "ScopeStart"
    icon = "TRY"
    scope_role = "try"
    short_description = "Starts a try-catch scope (flow variable ports)."
    description = "Like <i>Try (Data Ports)</i>, for flow variable connections."
    in_ports = [Port(FLOWVARIABLE, "Variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Variables")]
    fields = []

    def execute(self, inputs, s, ctx):
        return [FLOWVAR]


class CatchErrorsDataPorts(NodeModel):
    factory = "verknode.flow.CatchErrorsDataPorts"
    name = "Catch Errors (Data Ports)"
    category = ERRORS
    node_type = "ScopeEnd"
    icon = "CTCH"
    scope_role = "catch"
    handles_inactive = True
    short_description = "Ends a try-catch scope; uses the alternative input if a node failed."
    description = (
        "Outputs the default input (from the try branch) if all nodes in the try scope executed. If a "
        "node failed, the alternative input (second port) is used instead and execution continues."
        + ERROR_HELP
    )
    in_ports = [Port(TABLE, "Default input"), Port(TABLE, "Alternative input", optional=True)]
    out_ports = [Port(TABLE, "Output")]
    fields = _error_fields()

    def execute(self, inputs, s, ctx):
        default, alternative = inputs
        _push_error_vars(ctx, s)
        if not ctx.caught_error and _active(default):
            return [default]
        if _active(alternative):
            return [alternative]
        if ctx.caught_error:
            ctx.warn(f"Caught error in {ctx.caught_error.get('node')} and no alternative input is connected")
            return [pl.DataFrame()]
        return [INACTIVE]


class CatchErrorsVarPorts(NodeModel):
    factory = "verknode.flow.CatchErrorsVarPorts"
    name = "Catch Errors (Var Ports)"
    category = ERRORS
    node_type = "ScopeEnd"
    icon = "CTCH"
    scope_role = "catch"
    handles_inactive = True
    short_description = "Ends a try-catch scope (flow variable ports)."
    description = "Like <i>Catch Errors (Data Ports)</i>, for flow variable connections." + ERROR_HELP
    in_ports = [Port(FLOWVARIABLE, "Default input"), Port(FLOWVARIABLE, "Alternative input", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Variables")]
    fields = _error_fields()

    def execute(self, inputs, s, ctx):
        _push_error_vars(ctx, s)
        if ctx.caught_error or any(_active(t) for t in inputs):
            return [FLOWVAR]
        return [INACTIVE]


class ActiveBranchInverter(NodeModel):
    factory = "verknode.flow.ActiveBranchInverter"
    name = "Active Branch Inverter"
    category = ERRORS
    node_type = "Other"
    icon = "↯"
    handles_inactive = True
    short_description = "Makes an active branch inactive and vice versa."
    description = (
        "If the input is inactive, an empty table is output on an active branch; if the input is "
        "active, the output becomes inactive."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Inverted branch")]
    fields = []

    def execute(self, inputs, s, ctx):
        return [pl.DataFrame() if inputs[0] is INACTIVE else INACTIVE]


BREAK_ON = ["Always", "Empty table", "Active branch", "Inactive branch", "Variable matches value"]


class Breakpoint(NodeModel):
    factory = "verknode.flow.Breakpoint"
    name = "Breakpoint"
    category = ERRORS
    node_type = "Other"
    icon = "⛔"
    handles_inactive = True
    short_description = "Stops execution when a condition is met."
    description = (
        "Passes the input through, but fails (and so stops the downstream execution) when the selected "
        "condition holds. Useful for debugging and, together with Try/Catch, for flow control."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Input table (unchanged)")]
    fields = [
        F("enabled", "bool", "Breakpoint enabled", True),
        F("condition", "enum", "Breakpoint condition", "Empty table", options=BREAK_ON),
        F("variable", "variable", "Flow variable", "", show_if={"condition": "Variable matches value"}),
        F("value", "string", "Value", "", show_if={"condition": "Variable matches value"}),
        F("message", "string", "Custom message (optional)", ""),
    ]

    def configure(self, in_specs, s, ctx):
        return [in_specs[0]]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if s["enabled"]:
            cond = s["condition"]
            if cond == "Variable matches value":
                var = ctx.variable(s["variable"])
                if var is None:
                    raise InvalidSettingsError(f'Flow variable "{s["variable"]}" not found')
                hit = str(var.value) == (s["value"] or "")
            else:
                hit = {
                    "Always": True,
                    "Empty table": _active(df) and df.height == 0,
                    "Active branch": _active(df),
                    "Inactive branch": df is INACTIVE,
                }[cond]
            if hit:
                raise RuntimeError(s["message"] or f"Breakpoint halted execution ({cond.lower()})")
        return [df]


# ----------------------------------------------------------------------
# Flow variables
# ----------------------------------------------------------------------
class TableColumnToVariable(NodeModel):
    factory = "verknode.flow.TableColumnToVariable"
    name = "Table Column to Variable"
    category = VARIABLES
    node_type = "Other"
    icon = "▥→V"
    short_description = "Converts a column into flow variables, one per row."
    description = (
        "Creates one flow variable per row: the name is taken from the name column, the value from "
        "the value column (type follows the column type)."
    )
    in_ports = [Port(TABLE, "Variables table")]
    out_ports = [Port(FLOWVARIABLE, "Variables connection")]
    fields = [
        F("name_column", "column", "Variable name column", None, kinds=["string"]),
        F("value_column", "column", "Value column", None),
        F("missing", "enum", "Missing values", "Skip", options=["Skip", "Fail", "Use default value"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        name_col = require_column(s, "name_column", df.schema, "name column")
        value_col = require_column(s, "value_column", df.schema, "value column")
        vtype = _var_type_for(df.schema[value_col])
        for name, value in df.select(name_col, value_col).iter_rows():
            if not name:
                continue
            if value is None:
                if s["missing"] == "Skip":
                    continue
                if s["missing"] == "Fail":
                    raise ValueError(f'Missing value for variable "{name}"')
                value = VAR_DEFAULTS[vtype]
            elif vtype == "STRING":
                value = str(value)
            ctx.push_variable(str(name), value, vtype)
        return [FLOWVAR]


class VariableToTableColumn(NodeModel):
    factory = "verknode.flow.VariableToTableColumn"
    name = "Variable to Table Column"
    category = VARIABLES
    node_type = "Other"
    icon = "V→▥"
    short_description = "Appends flow variables as constant columns."
    description = "Appends the selected flow variables to the input table, one constant column per variable."
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Table with variable columns")]
    fields = [F("variables", "varlist", "Variables", {"include": [], "exclude": [], "enforce": "inclusion"})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        names = _vars_selection(s["variables"], ctx.stack)
        cols = []
        for n in names:
            if n in df.columns:
                raise InvalidSettingsError(f'Column "{n}" already exists')
            v = ctx.stack[n]
            cols.append(pl.lit(v.value, dtype=VAR_DTYPES[v.type]).alias(n))
        return [df.with_columns(cols) if cols else df]


class RuleEngineVariable(NodeModel):
    factory = "verknode.flow.RuleEngineVariable"
    name = "Rule Engine Variable"
    category = VARIABLES
    node_type = "Other"
    icon = "⇒V"
    short_description = "Creates a flow variable from rules over flow variables."
    description = (
        "Evaluates Rule Engine rules that reference flow variables as <code>$${Sname}$$</code> "
        "(type letters S, I, D, B). The outcome of the first matching rule becomes a new flow variable."
    )
    in_ports = [Port(FLOWVARIABLE, "Variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Variables with result")]
    fields = [
        F("rules", "code", "Expression", '// $${Dvalue}$$ > 10 => "large"\nTRUE => "default"',
          language="rules", insertables=True),
        F("new_variable", "string", "New flow variable name", "prediction"),
    ]

    def execute(self, inputs, s, ctx):
        rules = compile_rules(s["rules"] or "", [], ctx.variables())
        value = pl.DataFrame({"_": pl.Series([0], dtype=pl.Int8)}).select(rules_expression(rules).alias("r"))["r"][0]
        name = (s["new_variable"] or "").strip()
        if not name:
            raise InvalidSettingsError("No variable name given")
        if value is None:
            ctx.warn("No rule matched; variable not created")
        else:
            ctx.push_variable(name, value, infer_type(value))
        return [FLOWVAR]


class ExtractContextProperties(NodeModel):
    factory = "verknode.flow.ExtractContextProperties"
    name = "Extract Context Properties"
    category = VARIABLES
    node_type = "Other"
    icon = "ctx"
    short_description = "Pushes workflow context information as flow variables."
    description = (
        "Pushes <code>context.workflow.path</code>, <code>context.workflow.absolute-path</code>, "
        "<code>context.workflow.user</code>, <code>context.workflow.temp.location</code>, "
        "<code>context.workflow.execution.time</code> and the host name as flow variables."
    )
    out_ports = [Port(FLOWVARIABLE, "Context properties")]
    fields = []

    def execute(self, inputs, s, ctx):
        base = os.path.abspath(ctx.base_dir)
        wf_file = ctx.workflow_path or ""
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = ""
        props = {
            "context.workflow.path": ctx.base_dir,
            "context.workflow.absolute-path": base,
            "context.workflow.name": os.path.splitext(os.path.basename(wf_file))[0] if wf_file else os.path.basename(base),
            "context.workflow.file": wf_file,
            "context.workflow.user": user,
            "context.workflow.temp.location": tempfile.gettempdir(),
            "context.workflow.execution.time": dt.datetime.now().isoformat(timespec="seconds"),
            "context.host.name": socket.gethostname(),
        }
        for k, v in props.items():
            ctx.push_variable(k, v, "STRING")
        return [FLOWVAR]


class ExtractSystemProperties(NodeModel):
    factory = "verknode.flow.ExtractSystemProperties"
    name = "Extract System Properties"
    category = VARIABLES
    node_type = "Other"
    icon = "sys"
    short_description = "Outputs system and runtime properties as a table."
    description = (
        "Creates a key/value table with Python, operating system, host and library information; "
        "optionally including all environment variables."
    )
    out_ports = [Port(TABLE, "System properties")]
    fields = [F("environment", "bool", "Include environment variables", False)]

    def execute(self, inputs, s, ctx):
        props = {
            "python.version": sys.version.split()[0],
            "python.executable": sys.executable,
            "os.name": platform.system(),
            "os.version": platform.release(),
            "os.arch": platform.machine(),
            "host.name": socket.gethostname(),
            "user.name": os.environ.get("USER") or os.environ.get("NB_USER", ""),
            "user.home": os.path.expanduser("~"),
            "user.dir": os.getcwd(),
            "cpu.count": str(os.cpu_count()),
            "polars.version": pl.__version__,
        }
        try:
            import rdkit

            props["rdkit.version"] = rdkit.__version__
        except ImportError:
            pass
        if s["environment"]:
            props.update({f"env.{k}": v for k, v in sorted(os.environ.items())})
        return [pl.DataFrame({
            "Key": pl.Series(list(props), dtype=pl.String),
            "Value": pl.Series(list(props.values()), dtype=pl.String),
        })]


# ----------------------------------------------------------------------
# Wait
# ----------------------------------------------------------------------
class Wait(NodeModel):
    factory = "verknode.flow.Wait"
    name = "Wait..."
    category = "Flow Control/Wait"
    node_type = "Other"
    icon = "⏱"
    short_description = "Waits for a duration, until a time of day, or for a file."
    description = (
        "Pauses the branch: for a number of seconds, until a time of day (HH:MM[:SS]), or until a file "
        "is created, modified or deleted. Execution can be canceled while waiting."
    )
    in_ports = [Port(FLOWVARIABLE, "Variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Variables")]
    fields = [
        F("mode", "enum", "Wait", "For time", options=["For time", "To time", "For file"]),
        F("seconds", "float", "Seconds", 5.0, show_if={"mode": "For time"}),
        F("time", "string", "Time of day (HH:MM[:SS])", "12:00:00", show_if={"mode": "To time"}),
        F("path", "string", "File", "", PATH_HELP, path=True, show_if={"mode": "For file"}),
        F("file_event", "enum", "Wait for file", "Creation", options=["Creation", "Modification", "Deletion"],
          show_if={"mode": "For file"}),
    ]

    def _target_time(self, s) -> float:
        try:
            t = dt.time.fromisoformat(s["time"].strip())
        except ValueError:
            raise InvalidSettingsError(f"Invalid time of day: {s['time']!r}") from None
        now = dt.datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        return target.timestamp()

    def validate(self, s, in_specs):
        if s["mode"] == "To time":
            self._target_time(s)
        if s["mode"] == "For time" and s["seconds"] < 0:
            raise InvalidSettingsError("Seconds must not be negative")

    def configure(self, in_specs, s, ctx):
        self.validate(s, in_specs)
        if s["mode"] == "For file":
            ctx.resolve_path(s["path"])
        return [FLOWVAR]

    def _sleep_until(self, ctx, done, end=None):
        start = time.time()
        while not done():
            ctx.check_canceled()
            if end is not None and end > start:
                ctx.set_progress((time.time() - start) / (end - start))
            time.sleep(0.1)

    def execute(self, inputs, s, ctx):
        mode = s["mode"]
        if mode == "For time":
            end = time.time() + s["seconds"]
            self._sleep_until(ctx, lambda: time.time() >= end, end)
        elif mode == "To time":
            end = self._target_time(s)
            self._sleep_until(ctx, lambda: time.time() >= end, end)
        else:
            path = ctx.resolve_path(s["path"])
            event = s["file_event"]
            if event == "Creation":
                self._sleep_until(ctx, lambda: os.path.exists(path))
            elif event == "Deletion":
                self._sleep_until(ctx, lambda: not os.path.exists(path))
            else:
                start = os.path.getmtime(path) if os.path.exists(path) else None

                def modified():
                    return os.path.exists(path) and os.path.getmtime(path) != start

                self._sleep_until(ctx, modified)
        return [FLOWVAR]


class Delay(NodeModel):
    """Passes its table on after a while: for trying out run times, estimates and ETAs."""

    factory = "verknode.flow.Delay"
    name = "Delay"
    category = "Flow Control/Wait"
    node_type = "Other"
    icon = "⌛"
    short_description = "Passes the table on after a delay (to try out run times and ETAs)."
    description = (
        "Waits, then passes the input table on unchanged (an empty table without input). The delay is a "
        "number of seconds plus some seconds per input row (so estimates scale with the rows), optionally "
        "varied at random on every run - a run slower than the last turns the node's time bar red. "
        "<i>Progress</i> is what the node reports while waiting: <i>Steady</i>, <i>Slow start</i> (behind "
        "at first: the bar turns amber) or <i>None</i> (the canvas compares the time with the last run)."
    )
    in_ports = [Port(TABLE, "Any table", optional=True)]
    out_ports = [Port(TABLE, "The table, unchanged")]
    fields = [
        F("seconds", "float", "Seconds", 2.0),
        F("per_row", "float", "Plus seconds per input row", 0.0),
        F("variation", "int", "Random variation (%)", 0, "Each run takes up to this much longer or shorter."),
        F("pace", "enum", "Progress", "Steady", options=["Steady", "Slow start", "None"]),
    ]

    def validate(self, s, in_specs):
        if s["seconds"] < 0 or s["per_row"] < 0:
            raise InvalidSettingsError("Delays must not be negative")
        if not 0 <= s["variation"] <= 100:
            raise InvalidSettingsError("The variation must be between 0 and 100 %")

    def configure(self, in_specs, s, ctx):
        self.validate(s, in_specs)
        return [in_specs[0]]

    def execute(self, inputs, s, ctx):
        import random

        df = inputs[0] if inputs[0] is not None else pl.DataFrame()
        total = s["seconds"] + s["per_row"] * df.height
        total *= 1 + random.uniform(-1, 1) * s["variation"] / 100
        start = time.time()
        while (done := time.time() - start) < total:
            ctx.check_canceled()
            f = done / total
            if s["pace"] == "Steady":
                ctx.set_progress(f)
            elif s["pace"] == "Slow start":
                ctx.set_progress(f ** 3)
            time.sleep(min(0.05, total - done))
        return [df]
