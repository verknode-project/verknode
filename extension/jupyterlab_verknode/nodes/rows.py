"""Row manipulation nodes: filters/splitters, sampling, pivoting, transforms and others."""
from __future__ import annotations

import numpy as np
import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, require_column
from verknode.node import compile_rules, rules_expression
from verknode.node import CELL_DTYPES, dtype_kind, is_numeric_kind
from .manipulation import AGGREGATIONS, RowFilter, selected_columns

IN = [Port(TABLE, "Input table")]
IDX = "__verknode_row__"
NUMERIC = ["integer", "long", "double"]
SIZE_FIELDS = [
    F("size_mode", "enum", "Size", "Relative [%]", options=["Absolute", "Relative [%]"]),
    F("absolute", "int", "Number of rows", 100, show_if={"size_mode": "Absolute"}),
    F("relative", "float", "Percentage of rows", 70.0, show_if={"size_mode": "Relative [%]"}),
]


def _rng(seed):
    return np.random.default_rng(None if seed is None or seed == -1 else seed)


def _count(s, h) -> int:
    if s["size_mode"] == "Absolute":
        return min(max(0, s["absolute"]), h)
    return min(max(0, round(h * s["relative"] / 100.0)), h)


def _split_by_index(df: pl.DataFrame, idx) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(rows at idx in that order, all other rows in input order)."""
    idx = pl.Series(IDX, sorted(set(int(i) for i in idx)) if idx is not None else [], dtype=pl.UInt32)
    numbered = df.with_row_index(IDX)
    first = numbered.filter(pl.col(IDX).is_in(idx.implode())).drop(IDX)
    rest = numbered.filter(~pl.col(IDX).is_in(idx.implode())).drop(IDX)
    return first, rest


def _sort_keys(df, keys):
    keys = [k for k in keys if k.get("column")]
    if not keys:
        raise InvalidSettingsError("No sort column selected")
    for k in keys:
        if k["column"] not in df.columns:
            raise InvalidSettingsError(f'Column "{k["column"]}" not found')
    return [k["column"] for k in keys], [k.get("order") == "Descending" for k in keys]


def _aggs(df, records):
    aggs = []
    for a in records or []:
        if not a.get("column"):
            continue
        if a["column"] not in df.columns:
            raise InvalidSettingsError(f'Column "{a["column"]}" not found')
        aggs.append((f'{a["method"]}({a["column"]})', AGGREGATIONS[a["method"]](pl.col(a["column"]))))
    return aggs


# ----------------------------------------------------------------------
# Filter
# ----------------------------------------------------------------------
class RowSplitter(NodeModel):
    factory = "verknode.manipulation.RowSplitter"
    name = "Row Splitter"
    category = "Manipulation/Row/Filter"
    icon = "▤⑂"
    short_description = "Splits rows by a condition on one column."
    description = (
        "Same criteria as the <i>Row Filter</i>; matching rows go to the first output, "
        "all other rows to the second."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Matching rows"), Port(TABLE, "Non-matching rows")]
    fields = [f for f in RowFilter.fields if f.key != "mode"]

    def execute(self, inputs, s, ctx):
        df = inputs[0].with_row_index(IDX)
        match = RowFilter().execute([df], dict(s, mode="Include"), ctx)[0]
        hit = pl.col(IDX).is_in(match[IDX].implode())
        return [df.filter(hit).drop(IDX), df.filter(~hit).drop(IDX)]


class RuleBasedRowSplitter(NodeModel):
    factory = "verknode.manipulation.RuleBasedRowSplitter"
    name = "Rule-based Row Splitter"
    category = "Manipulation/Row/Filter"
    icon = "⇒⑂"
    short_description = "Splits rows using Rule Engine rules."
    description = (
        "Rows whose first matching rule has outcome TRUE go to the first output, all others "
        "(including rows matching no rule) to the second. Uses the Rule Engine syntax."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "TRUE matches"), Port(TABLE, "FALSE matches")]
    fields = [F("rules", "code", "Expression", "// $column$ > 10 => TRUE", language="rules", insertables=True)]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        rules = compile_rules(s["rules"] or "", df.columns, ctx.variables())
        cond = rules_expression(rules, default=False).cast(pl.Boolean).fill_null(False)
        return [df.filter(cond), df.filter(~cond)]


class ReferenceRowFilter(NodeModel):
    factory = "verknode.manipulation.ReferenceRowFilter"
    name = "Reference Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "▤⇆"
    short_description = "Filters rows by the values of a reference table."
    description = (
        "Includes (or excludes) rows of the first table whose value in the data column occurs "
        "in the reference column of the second table."
    )
    in_ports = [Port(TABLE, "Table to be filtered"), Port(TABLE, "Reference table")]
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("data_column", "column", "Data table column", None, port=1),
        F("reference_column", "column", "Reference table column", None, port=2),
        F("mode", "enum", "Rows with matching values", "Include", options=["Include", "Exclude"]),
    ]

    def execute(self, inputs, s, ctx):
        df, ref = inputs
        col = require_column(s, "data_column", df.schema, "data column")
        rcol = require_column(s, "reference_column", ref.schema, "reference column")
        values = ref[rcol].drop_nulls().unique()
        if values.dtype != df.schema[col]:
            values = values.cast(pl.String)
            cond = pl.col(col).cast(pl.String).is_in(values.implode())
        else:
            cond = pl.col(col).is_in(values.implode())
        cond = cond.fill_null(False)
        return [df.filter(cond if s["mode"] == "Include" else ~cond)]


class NominalValueRowFilter(NodeModel):
    factory = "verknode.manipulation.NominalValueRowFilter"
    name = "Nominal Value Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "▤S"
    short_description = "Filters rows by a set of nominal values."
    description = (
        "Keeps rows whose value in the selected column is one of the given values "
        "(compared as text). Missing values can be included explicitly."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("column", "column", "Column"),
        F("values", "records", "Values", [{"value": ""}], fields=[F("value", "string", "Value", "")]),
        F("mode", "enum", "Matching rows", "Include", options=["Include", "Exclude"]),
        F("include_missing", "bool", "Treat missing values as matching", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        values = pl.Series("v", [r.get("value") or "" for r in s["values"] if r.get("value") not in (None, "")],
                           dtype=pl.String)
        cond = pl.col(col).cast(pl.String).is_in(values.implode())
        cond = cond.fill_null(bool(s["include_missing"]))
        return [df.filter(cond if s["mode"] == "Include" else ~cond)]


class Partitioning(NodeModel):
    factory = "verknode.manipulation.Partitioning"
    name = "Partitioning"
    category = "Manipulation/Row/Filter"
    icon = "▤|▤"
    short_description = "Splits the table into two partitions (e.g. train / test)."
    description = (
        "The first partition receives the given number or percentage of rows; all other rows go to "
        "the second. Rows are taken from the top, linearly spaced, drawn randomly, or stratified so "
        "that the class distribution of a column is preserved."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "First partition"), Port(TABLE, "Second partition")]
    fields = [
        *SIZE_FIELDS,
        F("method", "enum", "Sampling method", "Draw randomly",
          options=["Take from top", "Linear sampling", "Draw randomly", "Stratified sampling"]),
        F("class_column", "column", "Stratification column", None, show_if={"method": "Stratified sampling"}),
        F("seed", "int", "Random seed (-1: none)", 42, show_if={"method": ["Draw randomly", "Stratified sampling"]}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        h = df.height
        n = _count(s, h)
        method = s["method"]
        if method == "Take from top":
            idx = range(n)
        elif method == "Linear sampling":
            idx = [int(i * h / n) for i in range(n)] if n else []
        elif method == "Draw randomly":
            idx = _rng(s["seed"]).permutation(h)[:n] if h else []
        else:
            col = require_column(s, "class_column", df.schema, "stratification column")
            frac = n / h if h else 0.0
            rng = _rng(s["seed"])
            groups = df.with_row_index(IDX).group_by(col, maintain_order=True).agg(pl.col(IDX))
            idx = []
            for rows in groups[IDX].to_list():
                k = round(len(rows) * frac)
                idx.extend(rng.permutation(rows)[:k])
        return list(_split_by_index(df, idx))


class BootstrapSampling(NodeModel):
    factory = "verknode.manipulation.BootstrapSampling"
    name = "Bootstrap Sampling"
    category = "Manipulation/Row/Filter"
    icon = "⟲▤"
    short_description = "Draws a bootstrap sample (with replacement)."
    description = (
        "Draws rows with replacement. The second output holds the rows that were never drawn "
        "(out-of-bag / holdout)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Bootstrap sample"), Port(TABLE, "Holdout (not drawn)")]
    fields = [
        F("size_mode", "enum", "Sample size", "Relative [%]", options=["Absolute", "Relative [%]"]),
        F("absolute", "int", "Number of rows", 100, show_if={"size_mode": "Absolute"}),
        F("relative", "float", "Percentage of input size", 100.0, show_if={"size_mode": "Relative [%]"}),
        F("append_count", "bool", "Append count of occurrences", False),
        F("count_column", "string", "Count column name", "Occurrences", show_if={"append_count": True}),
        F("seed", "int", "Random seed (-1: none)", 42),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        h = df.height
        n = max(0, s["absolute"]) if s["size_mode"] == "Absolute" else max(0, round(h * s["relative"] / 100.0))
        draws = _rng(s["seed"]).integers(0, h, size=n) if h and n else np.array([], dtype=np.int64)
        numbered = df.with_row_index(IDX)
        sample = numbered[pl.Series(draws, dtype=pl.UInt32)] if len(draws) else numbered.clear()
        if s["append_count"]:
            counts = pl.Series(IDX, draws, dtype=pl.UInt32).value_counts(name="__n__")
            sample = sample.join(counts, on=IDX, how="left").rename({"__n__": s["count_column"] or "Occurrences"})
            sample = sample.with_columns(pl.col(s["count_column"] or "Occurrences").cast(pl.Int32))
        holdout = numbered.filter(~pl.col(IDX).is_in(pl.Series(draws, dtype=pl.UInt32).implode()))
        return [sample.drop(IDX), holdout.drop(IDX)]


class EqualSizeSampling(NodeModel):
    factory = "verknode.manipulation.EqualSizeSampling"
    name = "Equal Size Sampling"
    category = "Manipulation/Row/Filter"
    icon = "⚖▤"
    short_description = "Down-samples so that all classes have equal size."
    description = (
        "Removes rows of the more frequent classes so every value of the class column occurs equally "
        "often. <i>Exact</i> draws exactly the minority count per class; <i>Approximate</i> keeps each "
        "row with a class-dependent probability."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Sampled table")]
    fields = [
        F("class_column", "column", "Nominal column"),
        F("method", "enum", "Sampling method", "Exact", options=["Exact", "Approximate"]),
        F("seed", "int", "Random seed (-1: none)", 42),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "class_column", df.schema, "class column")
        groups = df.with_row_index(IDX).group_by(col, maintain_order=True).agg(pl.col(IDX))[IDX].to_list()
        if not groups:
            return [df]
        m = min(len(g) for g in groups)
        rng = _rng(s["seed"])
        idx = []
        for rows in groups:
            if s["method"] == "Exact":
                idx.extend(rng.permutation(rows)[:m])
            else:
                keep = rng.random(len(rows)) < (m / len(rows))
                idx.extend(np.asarray(rows)[keep])
        return [_split_by_index(df, idx)[0]]


class TopKRowFilter(NodeModel):
    factory = "verknode.manipulation.TopKRowFilter"
    name = "Top k Row Filter"
    category = "Manipulation/Row/Filter"
    icon = "↑k"
    short_description = "Keeps the k top rows by sort criteria."
    description = (
        "Selects the first k rows according to the sort criteria. With <i>Unique values</i> all rows "
        "belonging to the k best distinct value combinations are kept (ties included)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Top k rows")]
    fields = [
        F("keys", "records", "Sort by", [{"column": None, "order": "Descending"}], fields=[
            F("column", "column", "Column"),
            F("order", "enum", "Order", "Descending", options=["Ascending", "Descending"]),
        ]),
        F("k", "int", "Number of rows / unique values (k)", 5),
        F("mode", "enum", "Filter mode", "Rows", options=["Rows", "Unique values"]),
        F("missings_last", "bool", "Missing values last", True),
        F("output_order", "enum", "Output order", "Sorted", options=["Sorted", "Keep input order"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols, desc = _sort_keys(df, s["keys"])
        k = max(0, s["k"])
        numbered = df.with_row_index(IDX)
        ordered = numbered.sort(cols, descending=desc, nulls_last=s["missings_last"], maintain_order=True)
        if s["mode"] == "Rows":
            top = ordered.head(k)
        else:
            keys = ordered.select(cols).unique(maintain_order=True).head(k)
            top = ordered.join(keys, on=cols, how="semi", nulls_equal=True)
        if s["output_order"] == "Keep input order":
            top = top.sort(IDX)
        return [top.drop(IDX)]


# ----------------------------------------------------------------------
# Transform
# ----------------------------------------------------------------------
class Pivot(NodeModel):
    factory = "verknode.manipulation.Pivot"
    name = "Pivot"
    category = "Manipulation/Row/Transform"
    icon = "⊞"
    short_description = "Pivots and groups the table by group and pivot columns."
    description = (
        "Creates one row per group and one column per combination of pivot value and aggregation. "
        "Second output: aggregations per group (group totals). Third output: aggregations per pivot "
        "value plus overall totals (pivot totals). Multiple pivot values are joined with '_'."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Pivot table"), Port(TABLE, "Group totals"), Port(TABLE, "Pivot totals")]
    fields = [
        F("groups", "columns", "Groups", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("pivots", "columns", "Pivots", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("aggregations", "records", "Manual aggregation", [{"column": None, "method": "Count"}], fields=[
            F("column", "column", "Column"),
            F("method", "enum", "Aggregation", "Count", options=list(AGGREGATIONS)),
        ]),
        F("naming", "enum", "Column name", "Pivot name+Aggregation name",
          options=["Pivot name+Aggregation name", "Aggregation name+Pivot name", "Pivot name"]),
    ]

    def _parts(self, df, s):
        groups = selected_columns(s["groups"], df.schema)
        pivots = selected_columns(s["pivots"], df.schema)
        if not pivots:
            raise InvalidSettingsError("No pivot columns selected")
        if set(groups) & set(pivots):
            raise InvalidSettingsError("A column cannot be both group and pivot column")
        aggs = _aggs(df, s["aggregations"])
        if not aggs:
            raise InvalidSettingsError("No aggregation selected")
        return groups, pivots, aggs

    def _totals(self, df, groups, aggs):
        exprs = [e.alias(n) for n, e in aggs]
        return df.group_by(groups, maintain_order=True).agg(exprs) if groups else df.select(exprs)

    def configure(self, in_specs, s, ctx):
        empty = pl.DataFrame(schema=in_specs[0])
        groups, pivots, aggs = self._parts(empty, s)
        # pivot columns depend on the data: only the group totals are known in advance
        return [None, self._totals(empty, groups, aggs).schema, None]

    def _name(self, s, pivot, agg):
        if s["naming"] == "Pivot name":
            return pivot
        if s["naming"] == "Aggregation name+Pivot name":
            return f"{agg}+{pivot}"
        return f"{pivot}+{agg}"

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        groups, pivots, aggs = self._parts(df, s)
        if s["naming"] == "Pivot name" and len(aggs) > 1:
            raise InvalidSettingsError('Naming "Pivot name" requires exactly one aggregation')
        label = pl.concat_str([pl.col(p).cast(pl.String).fill_null("?") for p in pivots], separator="_")
        data = df.with_columns(label.alias("__pivot__"))
        long = self._totals(data, groups + ["__pivot__"], aggs)
        values = data["__pivot__"].unique(maintain_order=True).sort().to_list()
        base = (data.select(groups).unique(maintain_order=True) if groups
                else pl.DataFrame({"__one__": [1]}))
        if not groups:
            long = long.with_columns(pl.lit(1).alias("__one__"))
        keys = groups or ["__one__"]
        pivot_table = base
        for v in values:
            ctx.check_canceled()
            part = long.filter(pl.col("__pivot__") == v).select(
                keys + [pl.col(n).alias(self._name(s, v, n)) for n, _ in aggs])
            pivot_table = pivot_table.join(part, on=keys, how="left", nulls_equal=True)
        if not groups:
            pivot_table = pivot_table.drop("__one__")
        group_totals = self._totals(df, groups, aggs)
        per_pivot = self._totals(data, ["__pivot__"], aggs)
        totals_cols = {}
        for v in values:
            row = per_pivot.filter(pl.col("__pivot__") == v)
            for n, _ in aggs:
                totals_cols[self._name(s, v, n)] = row[n]
        overall = df.select([e.alias(n) for n, e in aggs])
        pivot_totals = pl.DataFrame(totals_cols) if totals_cols else pl.DataFrame()
        pivot_totals = pl.concat([pivot_totals, overall], how="horizontal") if pivot_totals.width else overall
        return [pivot_table, group_totals, pivot_totals]


class Unpivot(NodeModel):
    factory = "verknode.manipulation.Unpivot"
    name = "Unpivot"
    category = "Manipulation/Row/Transform"
    icon = "⊟"
    short_description = "Rotates selected columns into rows."
    description = (
        "For each input row and each value column, creates a row with the column name "
        "(<code>ColumnNames</code>), its value (<code>ColumnValues</code>), the original row "
        "(<code>RowIDs</code>) and the retained columns. Mixed value types are converted to String."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Unpivoted table")]
    fields = [
        F("values", "columns", "Value columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("retained", "columns", "Retained columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("skip_missing", "bool", "Skip rows containing missing cells", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        values = selected_columns(s["values"], df.schema)
        retained = [c for c in selected_columns(s["retained"], df.schema) if c not in values]
        if not values:
            raise InvalidSettingsError("No value columns selected")
        dtypes = {df.schema[c] for c in values}
        data = df if len(dtypes) == 1 or all(t.is_numeric() for t in dtypes) else df.with_columns(
            [pl.col(c).cast(pl.String) for c in values])
        data = data.with_row_index(IDX).with_columns(
            pl.format("Row{}", pl.col(IDX)).alias("RowIDs"))
        out = data.unpivot(on=values, index=[IDX, "RowIDs"] + retained,
                           variable_name="ColumnNames", value_name="ColumnValues")
        order = {c: i for i, c in enumerate(values)}
        out = out.with_columns(pl.col("ColumnNames").replace_strict(order, return_dtype=pl.Int32).alias("__c__"))
        out = out.sort([IDX, "__c__"]).drop(IDX, "__c__")
        if s["skip_missing"]:
            out = out.filter(pl.col("ColumnValues").is_not_null())
        return [out.select(["RowIDs", "ColumnNames", "ColumnValues"] + retained)]


class Ungroup(NodeModel):
    factory = "verknode.manipulation.Ungroup"
    name = "Ungroup"
    category = "Manipulation/Row/Transform"
    icon = "[ ]→"
    short_description = "Creates one row per element of collection columns."
    description = (
        "Expands collection (list) columns: each element becomes its own row; other columns are "
        "repeated. Multiple collection columns must have equal lengths per row."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Ungrouped table")]
    fields = [
        F("columns", "columns", "Collection columns", {"include": [], "exclude": [], "enforce": "exclusion"},
          kinds=["list"]),
        F("skip_missing", "bool", "Skip missing values", False),
        F("skip_empty", "bool", "Skip empty collections", True,
          description="If unchecked, empty collections produce one row with missing values."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c in selected_columns(s["columns"], df.schema) if dtype_kind(df.schema[c]) == "list"]
        if not cols:
            raise InvalidSettingsError("No collection columns selected")
        if not s["skip_empty"]:
            df = df.with_columns([
                pl.when(pl.col(c).list.len() == 0).then(pl.lit(None).cast(df.schema[c])).otherwise(pl.col(c)).alias(c)
                for c in cols])
        out = df.explode(cols)
        if s["skip_missing"]:
            out = out.drop_nulls(cols)
        return [out]


class Transpose(NodeModel):
    factory = "verknode.manipulation.Transpose"
    name = "Transpose"
    category = "Manipulation/Row/Transform"
    icon = "⤡"
    short_description = "Swaps rows and columns."
    description = (
        "Columns become rows and rows become columns. The original column names are stored in the "
        "<code>RowID</code> column, new columns are named Row0, Row1, ... Mixed types are converted to String."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Transposed table")]
    fields = []

    def configure(self, in_specs, s, ctx):
        return [None]  # columns depend on the number of rows

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if len(set(df.schema.values())) > 1 and not all(t.is_numeric() for t in df.schema.values()):
            df = df.with_columns(pl.all().cast(pl.String))
        if df.width == 0:
            return [pl.DataFrame({"RowID": pl.Series([], dtype=pl.String)})]
        return [df.transpose(include_header=True, header_name="RowID",
                             column_names=[f"Row{i}" for i in range(df.height)])]


class CounterGeneration(NodeModel):
    factory = "verknode.manipulation.CounterGeneration"
    name = "Counter Generation"
    category = "Manipulation/Row/Transform"
    icon = "#+"
    short_description = "Appends a counter column."
    description = "Appends an integer column counting the rows, starting at the start value."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with counter")]
    fields = [
        F("column", "string", "Column name", "counter"),
        F("start", "int", "Start value", 1),
        F("increment", "int", "Increment", 1),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        name = s["column"] or "counter"
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        return [df.with_columns((pl.int_range(0, pl.len(), dtype=pl.Int64) * s["increment"] + s["start"]).alias(name))]


class RowAggregator(NodeModel):
    factory = "verknode.manipulation.RowAggregator"
    name = "Row Aggregator"
    category = "Manipulation/Row/Transform"
    icon = "Σ▤"
    short_description = "Aggregates rows per category (count, sum, average, min, max)."
    description = (
        "Aggregates the selected numeric columns, per category value if a category column is selected. "
        "Sum and average can be weighted by a weight column. The second output holds the grand totals."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Aggregated"), Port(TABLE, "Grand totals")]
    METHODS = ["Occurrence count", "Sum", "Average", "Minimum", "Maximum"]
    fields = [
        F("category", "column", "Category column (optional)", None),
        F("method", "enum", "Aggregation", "Sum", options=METHODS),
        F("columns", "columns", "Aggregated columns", {"include": [], "exclude": [], "enforce": "exclusion"},
          kinds=NUMERIC, show_if={"method": METHODS[1:]}),
        F("weight", "column", "Weight column (optional)", None, kinds=NUMERIC,
          show_if={"method": ["Sum", "Average"]}),
    ]

    def _exprs(self, df, s):
        method = s["method"]
        if method == "Occurrence count":
            return [pl.len().cast(pl.Int64).alias("OCCURRENCE_COUNT")]
        cols = [c for c in selected_columns(s["columns"], df.schema)
                if is_numeric_kind(dtype_kind(df.schema[c])) and c not in (s.get("category"), s.get("weight"))]
        if not cols:
            raise InvalidSettingsError("No numeric columns selected")
        w = s.get("weight") if method in ("Sum", "Average") else None
        if w and w not in df.columns:
            raise InvalidSettingsError(f'Column "{w}" not found')
        out = []
        for c in cols:
            x = pl.col(c)
            if method == "Sum":
                e = (x * pl.col(w)).sum() if w else x.sum()
            elif method == "Average":
                e = ((x * pl.col(w)).sum() / pl.col(w).filter(x.is_not_null()).sum()) if w else x.mean()
            elif method == "Minimum":
                e = x.min()
            else:
                e = x.max()
            out.append(e.alias(c))
        return out

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        exprs = self._exprs(df, s)
        cat = s.get("category")
        if cat:
            require_column(s, "category", df.schema, "category column")
            agg = df.group_by(cat, maintain_order=True).agg(exprs)
        else:
            agg = df.select(exprs)
        return [agg, df.select(exprs)]


class Shuffle(NodeModel):
    factory = "verknode.manipulation.Shuffle"
    name = "Shuffle"
    category = "Manipulation/Row/Transform"
    icon = "⤨"
    short_description = "Shuffles the rows randomly."
    description = "Puts the rows in random order. A seed makes the order reproducible."
    in_ports = IN
    out_ports = [Port(TABLE, "Shuffled table")]
    fields = [F("seed", "int", "Random seed (-1: none)", -1)]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        seed = None if s["seed"] == -1 else s["seed"]
        return [df.sample(fraction=1.0, shuffle=True, seed=seed) if df.height else df]


class TableDifferenceFinder(NodeModel):
    factory = "verknode.manipulation.TableDifferenceFinder"
    name = "Table Difference Finder"
    category = "Manipulation/Row/Transform"
    icon = "≠▤"
    short_description = "Finds differences between two tables."
    description = (
        "Compares the table to compare (top) with the reference table (bottom) row by row. The first "
        "output lists every differing cell, the second the differences of the table specs."
    )
    in_ports = [Port(TABLE, "Table to compare"), Port(TABLE, "Reference table")]
    out_ports = [Port(TABLE, "Value differences"), Port(TABLE, "Spec differences")]
    fields = [F("fail", "bool", "Fail execution if tables differ", False)]

    def execute(self, inputs, s, ctx):
        cmp, ref = inputs
        spec_rows = []
        for i, c in enumerate(dict.fromkeys(list(cmp.columns) + list(ref.columns))):
            tc, tr = cmp.schema.get(c), ref.schema.get(c)
            pc = cmp.columns.index(c) if c in cmp.columns else None
            pr = ref.columns.index(c) if c in ref.columns else None
            if tc != tr or pc != pr:
                spec_rows.append((c, None if tc is None else str(tc), None if tr is None else str(tr), pc, pr))
        common = [c for c in cmp.columns if c in ref.columns]
        n = max(cmp.height, ref.height)
        value_rows = []
        for c in common:
            ctx.check_canceled()
            a = cmp[c].cast(pl.String).to_list() + [None] * (n - cmp.height)
            b = ref[c].cast(pl.String).to_list() + [None] * (n - ref.height)
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y or (i >= cmp.height) != (i >= ref.height):
                    value_rows.append((f"Row{i}", c, x, y))
        values = pl.DataFrame(value_rows, schema={"RowID": pl.String, "Column": pl.String,
                                                  "Value in compared table": pl.String,
                                                  "Value in reference table": pl.String}, orient="row")
        specs = pl.DataFrame(spec_rows, schema={"Column": pl.String, "Type in compared table": pl.String,
                                                "Type in reference table": pl.String,
                                                "Position in compared table": pl.Int32,
                                                "Position in reference table": pl.Int32}, orient="row")
        if s["fail"] and (values.height or specs.height):
            raise ValueError(f"Tables differ: {values.height} cell(s), {specs.height} column spec(s)")
        return [values, specs]


class RowID(NodeModel):
    factory = "verknode.manipulation.RowID"
    name = "RowID"
    category = "Manipulation/Row/Transform"
    icon = "ID"
    short_description = "Creates a RowID column from row numbers or column values."
    description = (
        "Adds a String column <code>RowID</code> as first column: either generated (prefix + row "
        "number, e.g. Row0) or taken from a column's values, optionally made unique by appending "
        "a counter to duplicates (_1, _2, ...)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with RowID column")]
    fields = [
        F("mode", "enum", "RowID", "Generate", options=["Generate", "From column"]),
        F("prefix", "string", "Prefix", "Row", show_if={"mode": "Generate"}),
        F("column", "column", "Column with new RowIDs", None, show_if={"mode": "From column"}),
        F("unique", "bool", "Ensure uniqueness", True, show_if={"mode": "From column"}),
        F("missing", "string", "Replacement for missing values", "?", show_if={"mode": "From column"}),
        F("remove_column", "bool", "Remove selected column", False, show_if={"mode": "From column"}),
        F("name", "string", "RowID column name", "RowID"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        name = s["name"] or "RowID"
        if s["mode"] == "Generate":
            ids = pl.format(f"{s['prefix'] or ''}{{}}", pl.int_range(0, pl.len()))
            src = None
        else:
            src = require_column(s, "column", df.schema)
            ids = pl.col(src).cast(pl.String).fill_null(s["missing"] or "?")
            if s["unique"]:
                n = pl.int_range(0, pl.len()).over(ids)
                ids = pl.when(n == 0).then(ids).otherwise(pl.format("{}_{}", ids, n))
        if s.get("remove_column") and src:
            df = df.drop(src)
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        return [pl.concat([inputs[0].select(ids.alias(name)), df], how="horizontal")]


# ----------------------------------------------------------------------
# Other
# ----------------------------------------------------------------------
def _filler_rows(schema, n, s) -> pl.DataFrame:
    cols = {}
    for c, t in schema.items():
        kind = dtype_kind(t)
        value = None
        if kind == "double" and not s.get("double_missing", True):
            value = s.get("double_value", 0.0)
        elif kind in ("integer", "long") and not s.get("int_missing", True):
            value = s.get("int_value", 0)
        elif kind == "string" and not s.get("string_missing", True):
            value = s.get("string_value", "")
        cols[c] = pl.Series(c, [value] * n, dtype=t)
    return pl.DataFrame(cols, schema=schema)


FILL_FIELDS = [
    F("double_missing", "bool", "Double: missing value", True),
    F("double_value", "float", "Double fill value", 0.0, show_if={"double_missing": False}),
    F("int_missing", "bool", "Integer: missing value", True),
    F("int_value", "int", "Integer fill value", 0, show_if={"int_missing": False}),
    F("string_missing", "bool", "String: missing value", True),
    F("string_value", "string", "String fill value", "", show_if={"string_missing": False}),
]


class AddEmptyRows(NodeModel):
    factory = "verknode.manipulation.AddEmptyRows"
    name = "Add Empty Rows"
    category = "Manipulation/Row/Other"
    icon = "+▤"
    short_description = "Appends rows filled with missing or constant values."
    description = (
        "Appends a number of rows (or fills up to at least that many rows). New cells are missing "
        "values or the given constant per column type; other types are always missing."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with additional rows")]
    fields = [
        F("count", "int", "Number of rows", 15),
        F("mode", "enum", "Mode", "Additional rows", options=["Additional rows", "At least (fill up to)"]),
        *FILL_FIELDS,
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        n = max(0, s["count"]) if s["mode"] == "Additional rows" else max(0, s["count"] - df.height)
        return [pl.concat([df, _filler_rows(df.schema, n, s)], how="vertical") if n else df]


class EmptyTableReplacer(NodeModel):
    factory = "verknode.manipulation.EmptyTableReplacer"
    name = "Empty Table Replacer"
    category = "Manipulation/Row/Other"
    icon = "∅→"
    short_description = "Replaces an empty table by a table with one row."
    description = "If the input table has no rows, outputs one row with missing values or constants; otherwise passes the table."
    in_ports = IN
    out_ports = [Port(TABLE, "Non-empty table")]
    fields = FILL_FIELDS

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        return [df if df.height else _filler_rows(df.schema, 1, s)]


class ExtractTableDimension(NodeModel):
    factory = "verknode.manipulation.ExtractTableDimension"
    name = "Extract Table Dimension"
    category = "Manipulation/Row/Other"
    icon = "n×m"
    short_description = "Outputs the number of rows and columns."
    description = (
        "Outputs a table with the number of rows and columns and pushes the flow variables "
        "<code>number of rows</code> and <code>number of columns</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table dimensions")]
    fields = []

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        ctx.push_variable("number of rows", df.height, "INTEGER")
        ctx.push_variable("number of columns", df.width, "INTEGER")
        return [pl.DataFrame({
            "Dimension": pl.Series(["Number of Rows", "Number of Columns"], dtype=pl.String),
            "Value": pl.Series([df.height, df.width], dtype=pl.Int64),
        })]


class ExtractColumnHeader(NodeModel):
    factory = "verknode.manipulation.ExtractColumnHeader"
    name = "Extract Column Header"
    category = "Manipulation/Row/Other"
    icon = "H↑"
    short_description = "Extracts the column names as a one-row table."
    description = (
        "The first output holds a single row with the column names, under generic names "
        "(Column0, Column1, ...). The second output is the input with these generic column names."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Column header"), Port(TABLE, "Input with generic column names")]
    fields = [
        F("prefix", "string", "Prefix of new column names", "Column"),
        F("types", "enum", "Columns", "All columns", options=["All columns", "String columns", "Numeric columns"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c, t in df.schema.items()
                if s["types"] == "All columns"
                or (s["types"] == "String columns" and dtype_kind(t) == "string")
                or (s["types"] == "Numeric columns" and is_numeric_kind(dtype_kind(t)))]
        new = {c: f"{s['prefix'] or 'Column'}{i}" for i, c in enumerate(cols)}
        clash = set(new.values()) & (set(df.columns) - set(cols))
        if clash:
            raise InvalidSettingsError(f"Generated names clash with existing columns: {', '.join(sorted(clash))}")
        header = pl.DataFrame({v: pl.Series([k], dtype=pl.String) for k, v in new.items()})
        return [header, df.rename(new)]


class InsertColumnHeader(NodeModel):
    factory = "verknode.manipulation.InsertColumnHeader"
    name = "Insert Column Header"
    category = "Manipulation/Row/Other"
    icon = "H↓"
    short_description = "Renames columns using a dictionary table."
    description = (
        "Renames the columns of the data table using a dictionary table (second input) with a lookup "
        "column (current names) and a value column (new names)."
    )
    in_ports = [Port(TABLE, "Data table"), Port(TABLE, "Dictionary table")]
    out_ports = [Port(TABLE, "Table with new column names")]
    fields = [
        F("lookup", "column", "Lookup column (current names)", None, port=2),
        F("value", "column", "Value column (new names)", None, port=2),
        F("fail", "bool", "Fail if a column has no new name", False),
    ]

    def configure(self, in_specs, s, ctx):
        require_column(s, "lookup", in_specs[1], "lookup column")
        require_column(s, "value", in_specs[1], "value column")
        return [None]  # new names come from the dictionary data

    def execute(self, inputs, s, ctx):
        df, dic = inputs
        lookup = require_column(s, "lookup", dic.schema, "lookup column")
        value = require_column(s, "value", dic.schema, "value column")
        mapping = {}
        for k, v in dic.select(pl.col(lookup).cast(pl.String), pl.col(value).cast(pl.String)).iter_rows():
            if k in df.columns and v:
                mapping[k] = v
        if s["fail"]:
            missing = [c for c in df.columns if c not in mapping]
            if missing:
                raise ValueError(f"No new name for column(s): {', '.join(missing)}")
        return [df.rename(mapping)]


class ValueCounter(NodeModel):
    factory = "verknode.manipulation.ValueCounter"
    name = "Value Counter"
    category = "Manipulation/Row/Other"
    icon = "#v"
    short_description = "Counts the occurrences of each value in a column."
    description = "Outputs one row per distinct value of the column with its count, most frequent first."
    in_ports = IN
    out_ports = [Port(TABLE, "Occurrences")]
    fields = [
        F("column", "column", "Column with values to count"),
        F("ignore_missing", "bool", "Ignore missing values", True),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        data = df.select(col)
        if s["ignore_missing"]:
            data = data.drop_nulls()
        out = data.group_by(col, maintain_order=True).agg(pl.len().cast(pl.Int64).alias("count"))
        return [out.sort(["count", col], descending=[True, False], nulls_last=True, maintain_order=True)]


class Rank(NodeModel):
    factory = "verknode.manipulation.Rank"
    name = "Rank"
    category = "Manipulation/Row/Other"
    icon = "1²³"
    short_description = "Ranks rows by one or more columns, optionally within groups."
    description = (
        "Appends a rank column. <i>Standard</i>: ties share a rank, next rank skips (1,1,3); "
        "<i>Dense</i>: ties share, no gaps (1,1,2); <i>Ordinal</i>: consecutive (1,2,3)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Ranked table")]
    fields = [
        F("keys", "records", "Ranking attributes", [{"column": None, "order": "Ascending"}], fields=[
            F("column", "column", "Column"),
            F("order", "enum", "Order", "Ascending", options=["Ascending", "Descending"]),
        ]),
        F("groups", "columns", "Grouping attributes", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("mode", "enum", "Rank mode", "Standard", options=["Standard", "Dense", "Ordinal"]),
        F("column", "string", "Rank column name", "rank"),
        F("retain_order", "bool", "Retain row order", True),
        F("as_long", "bool", "Rank as Long", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        keys, desc = _sort_keys(df, s["keys"])
        groups = [g for g in selected_columns(s["groups"], df.schema) if g not in keys]
        name = s["column"] or "rank"
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        data = df.with_row_index(IDX).sort(groups + keys, descending=[False] * len(groups) + desc,
                                            nulls_last=True, maintain_order=True)

        def over(e):
            return e.over(groups) if groups else e

        ordinal = over(pl.int_range(1, pl.len() + 1, dtype=pl.Int64))
        change = pl.any_horizontal([pl.col(k).ne_missing(over(pl.col(k).shift(1))) for k in keys]) | (ordinal == 1)
        data = data.with_columns(ordinal.alias("__ord__"), change.alias("__chg__"))
        if s["mode"] == "Ordinal":
            rank = pl.col("__ord__")
        elif s["mode"] == "Dense":
            rank = over(pl.col("__chg__").cast(pl.Int64).cum_sum())
        else:
            rank = over(pl.when(pl.col("__chg__")).then(pl.col("__ord__")).otherwise(None).forward_fill())
        data = data.with_columns(rank.cast(pl.Int64 if s["as_long"] else pl.Int32).alias(name))
        if s["retain_order"]:
            data = data.sort(IDX)
        return [data.drop(IDX, "__ord__", "__chg__")]


VALIDATOR_TYPES = ["Any", *CELL_DTYPES]


class TableValidator(NodeModel):
    factory = "verknode.manipulation.TableValidator"
    name = "Table Validator"
    category = "Manipulation/Row/Other"
    icon = "✓▤"
    short_description = "Validates the table against a reference column specification."
    description = (
        "Checks that the specified columns exist, have the expected type and (optionally) contain no "
        "missing values. Unknown columns can be removed. Conflicts are listed in the second output; "
        "with <i>Fail on conflict</i> execution fails instead."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Validated table"), Port(TABLE, "Conflicts")]
    fields = [
        F("columns", "records", "Reference columns", [{"name": "", "type": "Any", "required": True, "missing": True}],
          fields=[
              F("name", "column", "Column"),
              F("type", "enum", "Type", "Any", options=VALIDATOR_TYPES),
              F("required", "bool", "Required", True),
              F("missing", "bool", "Allow missing values", True),
          ]),
        F("convert", "bool", "Convert to expected type", False,
          description="Try to convert columns of another type; values that cannot be converted become missing."),
        F("remove_unknown", "bool", "Remove unknown columns", False),
        F("on_conflict", "enum", "On conflict", "Fail", options=["Fail", "Output conflicts"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        conflicts = []
        known = []
        for r in s["columns"]:
            name = r.get("name")
            if not name:
                continue
            known.append(name)
            if name not in df.columns:
                if r.get("required", True):
                    conflicts.append((name, "Column missing"))
                continue
            expected = r.get("type", "Any")
            if expected != "Any" and df.schema[name] != CELL_DTYPES[expected]:
                if s["convert"]:
                    df = df.with_columns(pl.col(name).cast(CELL_DTYPES[expected], strict=False))
                else:
                    conflicts.append((name, f"Type {df.schema[name]} instead of {expected}"))
            if not r.get("missing", True):
                n = df[name].null_count()
                if n:
                    conflicts.append((name, f"{n} missing value(s)"))
        if s["remove_unknown"]:
            df = df.select([c for c in df.columns if c in known])
        table = pl.DataFrame(conflicts, schema={"Column": pl.String, "Conflict": pl.String}, orient="row")
        if conflicts and s["on_conflict"] == "Fail":
            raise ValueError("Validation failed: " + "; ".join(f"{c}: {t}" for c, t in conflicts))
        return [df, table]
