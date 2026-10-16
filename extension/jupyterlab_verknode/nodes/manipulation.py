"""Column and row manipulation nodes."""
from __future__ import annotations


import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, require_column
from verknode.node import compile_rules, rules_expression
from verknode.node import selected_columns, substitute_variables
from verknode.node import CELL_DTYPES, dtype_kind, is_numeric_kind

IN = [Port(TABLE, "Input table")]


# ----------------------------------------------------------------------
# Column
# ----------------------------------------------------------------------
class ColumnFilter(NodeModel):
    factory = "verknode.manipulation.ColumnFilter"
    name = "Column Filter"
    category = "Manipulation/Column/Filter"
    icon = "▥"
    short_description = "Removes columns from the input table."
    description = "Select the columns to include. With <i>Enforce exclusion</i>, new input columns are included automatically."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with included columns")]
    fields = [F("columns", "columns", "Columns", {"include": [], "exclude": [], "enforce": "exclusion"})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        return [df.select(selected_columns(s["columns"], df.schema))]


class ColumnRenamer(NodeModel):
    factory = "verknode.manipulation.ColumnRenamer"
    name = "Column Renamer"
    category = "Manipulation/Column/Convert & Replace"
    icon = "A→B"
    short_description = "Renames columns."
    description = "Renames the selected columns."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with renamed columns")]
    fields = [
        F("renames", "records", "Renamings", [], fields=[
            F("column", "column", "Column"),
            F("new_name", "string", "New name", ""),
        ]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        mapping = {}
        for r in s["renames"]:
            if not r.get("column"):
                continue
            if r["column"] not in df.columns:
                raise InvalidSettingsError(f'Column "{r["column"]}" not found')
            if r.get("new_name"):
                mapping[r["column"]] = r["new_name"]
        return [df.rename(mapping)]


def _convert_expr(name, src, target, fmt):
    e = pl.col(name)
    kind = dtype_kind(src)
    if target == "String":
        return e.cast(pl.String)
    if target in ("Integer", "Long", "Double"):
        if kind == "string":
            e = e.str.strip_chars()
            if target != "Double":
                e = e.cast(pl.Float64, strict=False)
        return e.cast(CELL_DTYPES[target], strict=False)
    if target == "Boolean":
        if kind == "string":
            return e.str.strip_chars().str.to_lowercase().replace_strict(
                {"true": True, "1": True, "yes": True, "y": True, "t": True,
                 "false": False, "0": False, "no": False, "n": False, "f": False},
                default=None, return_dtype=pl.Boolean)
        return e.cast(pl.Boolean, strict=False)
    if target == "Date":
        if kind == "string":
            return e.str.to_date(fmt or None, strict=False)
        return e.cast(pl.Date, strict=False)
    if target == "Date&Time":
        if kind == "string":
            return e.str.to_datetime(fmt or None, strict=False)
        return e.cast(pl.Datetime("us"), strict=False)
    if target == "Time":
        if kind == "string":
            return e.str.to_time(fmt or None, strict=False)
        return e.cast(pl.Time, strict=False)
    raise InvalidSettingsError(f"Unknown target type: {target}")


class ColumnTypeConverter(NodeModel):
    factory = "verknode.manipulation.ColumnTypeConverter"
    name = "Column Type Converter"
    category = "Manipulation/Column/Convert & Replace"
    icon = "S→D"
    short_description = "Converts columns to another type."
    description = (
        "Converts the selected columns to String, number, Boolean or date &amp; time types. "
        "Values that cannot be converted become missing. The optional format uses strftime "
        "syntax, e.g. %d.%m.%Y (empty: ISO 8601)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Converted table")]
    fields = [
        F("conversions", "records", "Conversions", [], fields=[
            F("column", "column", "Column"),
            F("type", "enum", "Target type", "Double", options=list(CELL_DTYPES)),
            F("format", "string", "Format", ""),
        ]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        exprs = []
        for r in s["conversions"]:
            if not r.get("column"):
                continue
            if r["column"] not in df.schema:
                raise InvalidSettingsError(f'Column "{r["column"]}" not found')
            exprs.append(_convert_expr(r["column"], df.schema[r["column"]], r["type"], r.get("format")).alias(r["column"]))
        return [df.with_columns(exprs) if exprs else df]


class ColumnExpression(NodeModel):
    factory = "verknode.manipulation.ColumnExpression"
    name = "Column Expression"
    category = "Manipulation/Column/Convert & Replace"
    icon = "f(x)"
    short_description = "Computes new columns from SQL expressions."
    description = (
        "Appends or replaces columns using SQL expressions evaluated by polars, e.g. "
        "<code>$price$ * $quantity$</code>, <code>upper($name$)</code>, "
        "<code>CASE WHEN $x$ &gt; 0 THEN 'pos' ELSE 'neg' END</code>. "
        "Reference columns as <code>$column$</code> and flow variables as <code>$${Sname}$$</code>. "
        "Expressions are evaluated in order and may use columns created by earlier expressions."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with computed columns")]
    fields = [
        F("expressions", "records", "Expressions", [{"column": "new column", "expression": "1"}], fields=[
            F("column", "string", "Output column", ""),
            F("expression", "string", "Expression", ""),
        ], insertables=True),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        variables = ctx.variables()
        for r in s["expressions"]:
            if not r.get("column") or not r.get("expression"):
                continue
            sql = substitute_variables(r["expression"], variables, sql=True)
            try:
                expr = pl.sql_expr(sql)
            except Exception as e:  # noqa: BLE001
                raise InvalidSettingsError(f'Invalid expression for "{r["column"]}": {e}') from None
            df = df.with_columns(expr.alias(r["column"]))
        return [df]


class RuleEngine(NodeModel):
    factory = "verknode.manipulation.RuleEngine"
    name = "Rule Engine"
    category = "Manipulation/Column/Convert & Replace"
    icon = "⇒"
    short_description = "Applies user-defined business rules to the input table."
    description = (
        "Each rule has the form <code>condition =&gt; outcome</code>; the first matching rule "
        "determines the value. Columns are referenced as <code>$column$</code>, flow variables as "
        "<code>$${Sname}$$</code>.<br/>Operators: <code>= &lt; &lt;= &gt; &gt;= !=</code>, "
        "<code>LIKE</code> (wildcards * ?), <code>MATCHES</code> (regex), <code>IN (a, b)</code>, "
        "<code>MISSING $col$</code>, <code>AND OR XOR NOT</code>, <code>TRUE</code>. "
        "Lines starting with // are comments. No match gives a missing value."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with rule outcome column")]
    fields = [
        F("rules", "code", "Expression", '// $column$ > 10 => "large"\nTRUE => "default"',
          language="rules", insertables=True),
        F("mode", "enum", "Output", "Append column", options=["Append column", "Replace column"]),
        F("new_column", "string", "Appended column name", "prediction",
          show_if={"mode": "Append column"}),
        F("replace_column", "column", "Replaced column", None, show_if={"mode": "Replace column"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        rules = compile_rules(s["rules"] or "", df.columns, ctx.variables())
        if s["mode"] == "Replace column":
            target = require_column(s, "replace_column", df.schema)
        else:
            target = s["new_column"] or "prediction"
            if target in df.columns:
                raise InvalidSettingsError(f'Column "{target}" already exists')
        return [df.with_columns(rules_expression(rules).alias(target))]


class MissingValue(NodeModel):
    factory = "verknode.manipulation.MissingValue"
    name = "Missing Value"
    category = "Manipulation/Column/Transform"
    icon = "?→"
    short_description = "Handles missing values per column type."
    description = "Replaces missing values in number, String and other columns, or removes rows containing them."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with replaced missing values")]
    NUM = ["Do nothing", "Remove row", "Fixed value", "Mean", "Median", "Minimum", "Maximum",
           "Previous value", "Next value"]
    STR = ["Do nothing", "Remove row", "Fixed value", "Most frequent value", "Previous value", "Next value"]
    OTHER = ["Do nothing", "Remove row", "Previous value", "Next value"]
    fields = [
        F("number", "enum", "Number columns", "Do nothing", options=NUM),
        F("number_value", "float", "Fixed number", 0.0, show_if={"number": "Fixed value"}),
        F("string", "enum", "String columns", "Do nothing", options=STR),
        F("string_value", "string", "Fixed string", "", show_if={"string": "Fixed value"}),
        F("other", "enum", "Other columns", "Do nothing", options=OTHER),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        exprs, drop = [], []
        for name, t in df.schema.items():
            kind = dtype_kind(t)
            strategy = s["number"] if is_numeric_kind(kind) else s["string"] if kind == "string" else s["other"]
            c = pl.col(name)
            if strategy == "Do nothing":
                continue
            if strategy == "Remove row":
                drop.append(name)
                continue
            e = {
                "Fixed value": lambda: c.fill_null(pl.lit(s["number_value"] if kind != "string" else s["string_value"]).cast(t)),
                "Mean": lambda: c.fill_null(c.mean()),
                "Median": lambda: c.fill_null(c.median()),
                "Minimum": lambda: c.fill_null(c.min()),
                "Maximum": lambda: c.fill_null(c.max()),
                "Most frequent value": lambda: c.fill_null(c.drop_nulls().mode().first()),
                "Previous value": lambda: c.fill_null(strategy="forward"),
                "Next value": lambda: c.fill_null(strategy="backward"),
            }[strategy]()
            exprs.append(e.cast(t, strict=False) if strategy in ("Mean", "Median") and kind != "double" else e)
        if exprs:
            df = df.with_columns(exprs)
        if drop:
            df = df.drop_nulls(drop)
        return [df]


class Joiner(NodeModel):
    factory = "verknode.manipulation.Joiner"
    name = "Joiner"
    category = "Manipulation/Column/Split & Combine"
    icon = "⋈"
    short_description = "Joins two tables on key columns."
    description = (
        "Joins the left (top) and right (bottom) table on matching key columns. "
        "Right-table columns with duplicate names get the suffix."
    )
    in_ports = [Port(TABLE, "Left table"), Port(TABLE, "Right table")]
    out_ports = [Port(TABLE, "Joined table")]
    MODES = {
        "Inner join": "inner",
        "Left outer join": "left",
        "Right outer join": "right",
        "Full outer join": "full",
        "Left semi join (matching rows)": "semi",
        "Left anti join (non-matching rows)": "anti",
    }
    fields = [
        F("mode", "enum", "Join mode", "Inner join", options=list(MODES)),
        F("keys", "records", "Joining columns", [{"left": None, "right": None}], fields=[
            F("left", "column", "Left table column", port=1),
            F("right", "column", "Right table column", port=2),
        ]),
        F("suffix", "string", "Duplicate column suffix", " (Right)"),
    ]

    def execute(self, inputs, s, ctx):
        left, right = inputs
        keys = [k for k in s["keys"] if k.get("left") and k.get("right")]
        if not keys:
            raise InvalidSettingsError("No joining columns selected")
        for k in keys:
            if k["left"] not in left.columns:
                raise InvalidSettingsError(f'Column "{k["left"]}" not in left table')
            if k["right"] not in right.columns:
                raise InvalidSettingsError(f'Column "{k["right"]}" not in right table')
        how = self.MODES[s["mode"]]
        kw = {} if how in ("semi", "anti") else {"suffix": s["suffix"] or " (Right)"}
        if how == "full":
            kw["coalesce"] = True
        return [left.join(right, how=how, left_on=[k["left"] for k in keys],
                          right_on=[k["right"] for k in keys], **kw)]


# ----------------------------------------------------------------------
# Row
# ----------------------------------------------------------------------
def _literal_for(value: str, dtype):
    kind = dtype_kind(dtype)
    try:
        if kind in ("integer", "long"):
            return int(float(value))
        if kind == "double":
            return float(value)
        if kind == "boolean":
            return value.strip().lower() in ("true", "1", "yes")
        if kind in ("date", "datetime", "time"):
            return pl.lit(value).str.strptime(dtype, strict=False) if kind != "time" else pl.lit(value).str.to_time()
    except ValueError:
        raise InvalidSettingsError(f"Value {value!r} does not match column type") from None
    return value


class RowFilter(NodeModel):
    factory = "verknode.manipulation.RowFilter"
    name = "Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "▤"
    short_description = "Filters rows by a condition on one column."
    description = "Includes or excludes rows whose value in the selected column matches the condition."
    in_ports = IN
    out_ports = [Port(TABLE, "Filtered table")]
    OPS = ["=", "!=", "<", "<=", ">", ">=", "contains", "starts with", "ends with",
           "matches regex", "is missing", "is not missing"]
    fields = [
        F("column", "column", "Column"),
        F("operator", "enum", "Operator", "=", options=OPS),
        F("value", "string", "Value", "", show_if={"operator": OPS[:10]}),
        F("case_sensitive", "bool", "Case sensitive", True,
          show_if={"operator": ["contains", "starts with", "ends with", "matches regex"]}),
        F("mode", "enum", "Matching rows", "Include", options=["Include", "Exclude"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        name = require_column(s, "column", df.schema)
        c, op, v = pl.col(name), s["operator"], s["value"] or ""
        if op == "is missing":
            cond = c.is_null()
        elif op == "is not missing":
            cond = c.is_not_null()
        elif op in ("contains", "starts with", "ends with", "matches regex"):
            text = c.cast(pl.String)
            if not s["case_sensitive"] and op != "matches regex":
                text, v = text.str.to_lowercase(), v.lower()
            cond = {
                "contains": lambda: text.str.contains(v, literal=True),
                "starts with": lambda: text.str.starts_with(v),
                "ends with": lambda: text.str.ends_with(v),
                "matches regex": lambda: text.str.contains(v if s["case_sensitive"] else f"(?i){v}"),
            }[op]()
        else:
            lit = _literal_for(v, df.schema[name])
            cond = {"=": c == lit, "!=": c != lit, "<": c < lit, "<=": c <= lit,
                    ">": c > lit, ">=": c >= lit}[op]
        cond = cond.fill_null(False)
        return [df.filter(cond if s["mode"] == "Include" else ~cond)]


class RuleBasedRowFilter(NodeModel):
    factory = "verknode.manipulation.RuleBasedRowFilter"
    name = "Rule-based Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "⇒▤"
    short_description = "Filters rows using Rule Engine rules."
    description = (
        "Rows are included (or excluded) when the first matching rule has outcome TRUE. "
        "Uses the Rule Engine syntax, e.g. <code>$age$ &gt; 30 AND $city$ = \"Berlin\" =&gt; TRUE</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("rules", "code", "Expression", "// $column$ > 10 => TRUE", language="rules", insertables=True),
        F("mode", "enum", "Matching rows (outcome TRUE)", "Include", options=["Include", "Exclude"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        rules = compile_rules(s["rules"] or "", df.columns, ctx.variables())
        cond = rules_expression(rules, default=False).cast(pl.Boolean).fill_null(False)
        return [df.filter(cond if s["mode"] == "Include" else ~cond)]


class DuplicateRowFilter(NodeModel):
    factory = "verknode.manipulation.DuplicateRowFilter"
    name = "Duplicate Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "⧉"
    short_description = "Removes duplicate rows."
    description = "Removes rows that are duplicates with respect to the selected columns (all columns when none are selected)."
    in_ports = IN
    out_ports = [Port(TABLE, "Table without duplicates")]
    fields = [
        F("columns", "columns", "Columns identifying duplicates", {"include": [], "exclude": [], "enforce": "exclusion"}),
        F("keep", "enum", "Row to keep", "First", options=["First", "Last"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema) or None
        return [df.unique(subset=cols, keep=s["keep"].lower(), maintain_order=True)]


class RowSampling(NodeModel):
    factory = "verknode.manipulation.RowSampling"
    name = "Row Sampling"
    category = "Manipulation/Row/Filter"
    icon = "⁘"
    short_description = "Extracts a sample of rows."
    description = "Takes the first rows or a random sample (absolute count or fraction)."
    in_ports = IN
    out_ports = [Port(TABLE, "Sampled table")]
    fields = [
        F("mode", "enum", "Sampling", "First rows", options=["First rows", "Random rows (count)", "Random rows (fraction)"]),
        F("count", "int", "Number of rows", 100, show_if={"mode": ["First rows", "Random rows (count)"]}),
        F("fraction", "float", "Fraction (0..1)", 0.1, show_if={"mode": "Random rows (fraction)"}),
        F("seed", "int", "Random seed (-1: none)", 0, show_if={"mode": ["Random rows (count)", "Random rows (fraction)"]}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        seed = None if s["seed"] == -1 else s["seed"]
        if s["mode"] == "First rows":
            return [df.head(max(0, s["count"]))]
        if s["mode"] == "Random rows (count)":
            return [df.sample(n=min(max(0, s["count"]), df.height), seed=seed)]
        return [df.sample(fraction=min(max(0.0, s["fraction"]), 1.0), seed=seed)]


class Sorter(NodeModel):
    factory = "verknode.manipulation.Sorter"
    name = "Sorter"
    category = "Manipulation/Row/Transform"
    icon = "↓A"
    short_description = "Sorts rows by one or more columns."
    description = "Sorts the table by the given columns in order of priority."
    in_ports = IN
    out_ports = [Port(TABLE, "Sorted table")]
    fields = [
        F("keys", "records", "Sort by", [{"column": None, "order": "Ascending"}], fields=[
            F("column", "column", "Column"),
            F("order", "enum", "Order", "Ascending", options=["Ascending", "Descending"]),
        ]),
        F("nulls_last", "bool", "Missing values last", True),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        keys = [k for k in s["keys"] if k.get("column")]
        if not keys:
            raise InvalidSettingsError("No sort column selected")
        for k in keys:
            if k["column"] not in df.columns:
                raise InvalidSettingsError(f'Column "{k["column"]}" not found')
        return [df.sort([k["column"] for k in keys], descending=[k["order"] == "Descending" for k in keys],
                        nulls_last=s["nulls_last"], maintain_order=True)]


class Concatenate(NodeModel):
    factory = "verknode.manipulation.Concatenate"
    name = "Concatenate"
    category = "Manipulation/Row/Transform"
    icon = "⊕"
    short_description = "Concatenates the rows of two tables."
    description = "Appends the rows of the second table to the first. Column types are unified where possible."
    in_ports = [Port(TABLE, "First table"), Port(TABLE, "Second table", optional=True)]
    out_ports = [Port(TABLE, "Concatenated table")]
    fields = [
        F("columns", "enum", "Column handling", "Union", options=["Union", "Intersection"]),
    ]

    def execute(self, inputs, s, ctx):
        tables = [t for t in inputs if t is not None]
        if s["columns"] == "Intersection":
            common = [c for c in tables[0].columns if all(c in t.columns for t in tables)]
            tables = [t.select(common) for t in tables]
        return [pl.concat(tables, how="diagonal_relaxed")]


AGGREGATIONS = {
    "Sum": lambda c: c.sum(),
    "Mean": lambda c: c.mean(),
    "Median": lambda c: c.median(),
    "Minimum": lambda c: c.min(),
    "Maximum": lambda c: c.max(),
    "Count": lambda c: c.count(),
    "Unique count": lambda c: c.n_unique(),
    "First": lambda c: c.first(),
    "Last": lambda c: c.last(),
    "Standard deviation": lambda c: c.std(),
    "Variance": lambda c: c.var(),
    "List": lambda c: c,
    "Concatenate": lambda c: c.cast(pl.String).str.join(", "),
}


class GroupBy(NodeModel):
    factory = "verknode.manipulation.GroupBy"
    name = "GroupBy"
    category = "Manipulation/Row/Transform"
    icon = "Σ"
    short_description = "Groups rows and computes aggregations."
    description = (
        "Groups the table by the selected group columns and aggregates other columns. "
        "Without group columns the whole table is aggregated into one row. "
        "Output columns are named <code>Method(column)</code> unless a name is given."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Group table")]
    fields = [
        F("groups", "columns", "Group columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("aggregations", "records", "Aggregations", [{"column": None, "method": "Sum", "name": ""}], fields=[
            F("column", "column", "Column"),
            F("method", "enum", "Aggregation", "Sum", options=list(AGGREGATIONS)),
            F("name", "string", "Output name", ""),
        ]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        groups = selected_columns(s["groups"], df.schema)
        aggs = []
        for a in s["aggregations"]:
            if not a.get("column"):
                continue
            if a["column"] not in df.columns:
                raise InvalidSettingsError(f'Column "{a["column"]}" not found')
            out = a.get("name") or f'{a["method"]}({a["column"]})'
            aggs.append(AGGREGATIONS[a["method"]](pl.col(a["column"])).alias(out))
        if not groups and not aggs:
            raise InvalidSettingsError("Select group columns or aggregations")
        if not groups:
            return [df.select(aggs)]
        return [df.group_by(groups, maintain_order=True).agg(aggs)]
