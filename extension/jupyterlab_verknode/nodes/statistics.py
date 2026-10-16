"""Analytics nodes: statistics, hypothesis tests, scoring, feature filters, cross validation."""
from __future__ import annotations

import math
import warnings

import numpy as np
import polars as pl

from verknode.node import views
from verknode.node import (
    F,
    IMAGE,
    ImagePortObject,
    InvalidSettingsError,
    NodeModel,
    Port,
    TABLE,
    require_column,
)
from verknode.node import dtype_kind, is_numeric_kind
from .manipulation import selected_columns

STATS = "Analytics/Statistics"
TESTS = "Analytics/Statistics/Hypothesis Testing"
SCORING = "Analytics/Mining/Scoring"

NOMINAL_KINDS = ["string", "boolean"]
NUMERIC_KINDS = ["integer", "long", "double"]
ALL_COLUMNS = {"include": [], "exclude": [], "enforce": "exclusion"}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _fl(x):
    """Python float or None for missing / NaN / infinite values."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _frame(schema: dict, rows: list[dict]) -> pl.DataFrame:
    """DataFrame with a fixed schema from a list of row dicts (works for 0 rows)."""
    return pl.DataFrame({k: pl.Series(k, [r.get(k) for r in rows], dtype=t) for k, t in schema.items()})


def _values(df: pl.DataFrame, col: str) -> np.ndarray:
    """Float values of a column with missing values as NaN."""
    return df[col].cast(pl.Float64).to_numpy().astype(float)


def _finite(x: np.ndarray) -> np.ndarray:
    return x[np.isfinite(x)]


def _numeric(df: pl.DataFrame, sel) -> list[str]:
    return [c for c in selected_columns(sel, df.schema) if is_numeric_kind(dtype_kind(df.schema[c]))]


def _quiet(fn, *args, **kw):
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        return fn(*args, **kw)


def _check_confidence(c):
    if not 0 < c < 1:
        raise InvalidSettingsError("Confidence level must be between 0 and 1")


def _describe(x: np.ndarray) -> dict:
    v = x[~np.isnan(x)]
    n = len(v)
    sd = _quiet(np.std, v, ddof=1) if n > 1 else None
    return {
        "N": n,
        "Missing Count": int(np.isnan(x).sum()),
        "Mean": _fl(v.mean()) if n else None,
        "Standard Deviation": _fl(sd),
        "Standard Error Mean": _fl(sd / math.sqrt(n)) if sd is not None and n else None,
    }


DESCRIPTIVE = {"Column": pl.String, "N": pl.Int64, "Missing Count": pl.Int64, "Mean": pl.Float64,
               "Standard Deviation": pl.Float64, "Standard Error Mean": pl.Float64}


def _view_tables(ctx, title, *sections):
    """sections: (heading, DataFrame | html string)."""
    if ctx.dry:
        return
    parts = []
    for heading, content in sections:
        parts.append(f"<h2>{views.escape(heading)}</h2>")
        parts.append(views.table(content) if isinstance(content, pl.DataFrame) else content)
    ctx.set_view(views.page(title, *parts))


def _heatmap(names: list[str], matrix: np.ndarray, title: str) -> str:
    n = len(names)
    size = max(4.0, min(12.0, 0.6 * n + 2))
    fig, ax = views.figure(size + 1.5, size)
    im = ax.imshow(matrix, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(n), names, rotation=45, ha="right")
    ax.set_yticks(range(n), names)
    if n <= 15:
        for i in range(n):
            for j in range(n):
                v = matrix[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8,
                            color="white" if abs(v) > 0.6 else "black")
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title(title)
    return views.svg(fig)


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------
NUM_STATS = {
    "Column": pl.String, "Min": pl.Float64, "Max": pl.Float64, "Mean": pl.Float64,
    "Std. deviation": pl.Float64, "Variance": pl.Float64, "Skewness": pl.Float64, "Kurtosis": pl.Float64,
    "Overall sum": pl.Float64, "No. zeros": pl.Int64, "No. missings": pl.Int64, "No. NaNs": pl.Int64,
    "No. +∞s": pl.Int64, "No. -∞s": pl.Int64, "Median": pl.Float64, "Row count": pl.Int64,
}


class Statistics(NodeModel):
    factory = "verknode.analytics.Statistics"
    name = "Statistics"
    category = STATS
    node_type = "Other"
    icon = "μσ"
    has_view = True
    short_description = "Calculates statistical moments and value counts."
    description = (
        "Calculates statistical moments (minimum, maximum, mean, standard deviation, variance, skewness, "
        "kurtosis, sum, median) and counts of zeros, missing values, NaN and infinite values for numeric "
        "columns, and value counts for nominal (String / Boolean) columns. The view shows the statistics "
        "and histograms."
    )
    in_ports = [Port(TABLE, "Table to compute statistics for")]
    out_ports = [
        Port(TABLE, "Statistics Table", "Statistics of the numeric columns, one row per column"),
        Port(TABLE, "Nominal Histogram Table", "Most frequent values with counts, one column per nominal column"),
        Port(TABLE, "Occurrences Table", "Value counts of all nominal columns"),
    ]
    fields = [
        F("columns", "columns", "Columns", dict(ALL_COLUMNS)),
        F("median", "bool", "Calculate median values", True),
        F("max_nominal", "int", "Max no. of most frequent and infrequent values", 20,
          "Number of values listed per nominal column in the Nominal Histogram Table."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        numeric = [c for c in cols if is_numeric_kind(dtype_kind(df.schema[c]))]
        nominal = [c for c in cols if dtype_kind(df.schema[c]) in NOMINAL_KINDS]
        rows = []
        for c in numeric:
            ctx.check_canceled()
            x = _values(df, c)
            null = int(df[c].null_count())
            nan = int(df[c].is_nan().sum()) if df.schema[c].is_float() else 0
            finite = _finite(x)
            n = len(finite)
            from scipy import stats

            rows.append({
                "Column": c,
                "Min": _fl(finite.min()) if n else None,
                "Max": _fl(finite.max()) if n else None,
                "Mean": _fl(finite.mean()) if n else None,
                "Std. deviation": _fl(_quiet(np.std, finite, ddof=1)) if n > 1 else None,
                "Variance": _fl(_quiet(np.var, finite, ddof=1)) if n > 1 else None,
                "Skewness": _fl(_quiet(stats.skew, finite, bias=False)) if n > 2 else None,
                "Kurtosis": _fl(_quiet(stats.kurtosis, finite, bias=False)) if n > 3 else None,
                "Overall sum": _fl(finite.sum()),
                "No. zeros": int((finite == 0).sum()),
                "No. missings": null,
                "No. NaNs": nan,
                "No. +∞s": int(np.isposinf(x).sum()),
                "No. -∞s": int(np.isneginf(x).sum()),
                "Median": _fl(np.median(finite)) if s["median"] and n else None,
                "Row count": df.height,
            })
        stats_table = _frame(NUM_STATS, rows)

        occ_rows, hist = [], {}
        limit = max(1, s["max_nominal"])
        for c in nominal:
            counts = (df.select(pl.col(c).cast(pl.String).alias("v"))
                      .group_by("v").agg(pl.len().alias("n"))
                      .sort(["n", "v"], descending=[True, False], nulls_last=True))
            labels = []
            for v, n in counts.iter_rows():
                occ_rows.append({"Column": c, "Value": v, "Count": n,
                                 "Percent": 100.0 * n / df.height if df.height else None})
                if len(labels) < limit:
                    labels.append(f"{'?' if v is None else v} ({n})")
            hist[c] = labels
        height = max([len(v) for v in hist.values()] or [0])
        nominal_table = pl.DataFrame(
            [pl.Series(c, v + [None] * (height - len(v)), dtype=pl.String) for c, v in hist.items()])
        occurrences = _frame({"Column": pl.String, "Value": pl.String, "Count": pl.Int64, "Percent": pl.Float64},
                             occ_rows)

        if not ctx.dry:
            parts = ["<h2>Numeric columns</h2>", views.table(stats_table)]
            if numeric:
                parts.append(views.chart(self._histograms(df, numeric[:24])))
            if nominal:
                parts += ["<h2>Nominal columns</h2>", views.table(nominal_table)]
            ctx.set_view(views.page("Statistics", *parts))
        return [stats_table, nominal_table, occurrences]

    @staticmethod
    def _histograms(df, cols):
        from matplotlib.figure import Figure

        ncols = min(4, len(cols))
        nrows = math.ceil(len(cols) / ncols)
        fig = Figure(figsize=(3.2 * ncols, 2.4 * nrows), layout="constrained")
        axes = np.atleast_1d(fig.subplots(nrows, ncols)).ravel()
        for ax, c in zip(axes, cols):
            x = _finite(_values(df, c))
            if len(x):
                ax.hist(x, bins=min(20, max(1, len(np.unique(x)))), color=views.PALETTE[0], edgecolor="white")
            ax.set_title(c, fontsize=9)
            ax.tick_params(labelsize=7)
        for ax in axes[len(cols):]:
            ax.set_visible(False)
        return views.svg(fig)


# ----------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------
MEASURES = {"First column name": pl.String, "Second column name": pl.String, "Correlation value": pl.Float64,
            "p value": pl.Float64, "Degrees of freedom": pl.Int64}


def _matrix_table(names, matrix) -> pl.DataFrame:
    cols = [pl.Series("Column", names, dtype=pl.String)]
    for j, n in enumerate(names):
        cols.append(pl.Series(n, [_fl(matrix[i, j]) for i in range(len(names))], dtype=pl.Float64))
    return pl.DataFrame(cols)


def _codes(series: pl.Series) -> np.ndarray:
    """Integer codes of a nominal column (missing = -1)."""
    values = series.cast(pl.String).to_list()
    uniq = {v: i for i, v in enumerate(sorted({v for v in values if v is not None}))}
    return np.array([-1 if v is None else uniq[v] for v in values], dtype=int)


def _cramers_v(a: np.ndarray, b: np.ndarray, max_values: int):
    mask = (a >= 0) & (b >= 0)
    a, b = a[mask], b[mask]
    if not len(a):
        return None, None, None
    ra, rb = np.unique(a, return_inverse=True), np.unique(b, return_inverse=True)
    r, c = len(ra[0]), len(rb[0])
    if r < 2 or c < 2 or r > max_values or c > max_values:
        return None, None, None
    table = np.zeros((r, c))
    np.add.at(table, (ra[1], rb[1]), 1)
    from scipy import stats

    chi2, p, dof, _ = _quiet(stats.chi2_contingency, table, correction=False)
    v = math.sqrt(chi2 / (table.sum() * (min(r, c) - 1)))
    return v, p, int(dof)


class LinearCorrelation(NodeModel):
    factory = "verknode.analytics.LinearCorrelation"
    name = "Linear Correlation"
    category = STATS
    node_type = "Other"
    icon = "ρ"
    has_view = True
    short_description = "Computes correlation coefficients for pairs of columns."
    description = (
        "Calculates for each pair of selected columns a correlation coefficient: Pearson's product-moment "
        "coefficient for two numeric columns and Cramér's V for two nominal (String / Boolean) columns. "
        "Pairs of a numeric and a nominal column get a missing value. Missing values are ignored pairwise. "
        "The view shows the correlation matrix as heat map."
    )
    in_ports = [Port(TABLE, "Numeric input data")]
    out_ports = [
        Port(TABLE, "Correlation measure", "One row per column pair with correlation value, p value and degrees of freedom"),
        Port(TABLE, "Correlation matrix", "Correlation values as square matrix"),
    ]
    fields = [
        F("columns", "columns", "Columns", dict(ALL_COLUMNS)),
        F("alternative", "enum", "p-value", "two-sided", options=["two-sided", "greater", "less"],
          description="Alternative hypothesis for Pearson's test (greater: positive correlation)."),
        F("max_values", "int", "Possible values count (nominal)", 50,
          "Nominal columns with more distinct values are not used for Cramér's V."),
    ]

    def execute(self, inputs, s, ctx):
        from scipy import stats

        df = inputs[0]
        names = [c for c in selected_columns(s["columns"], df.schema)
                 if is_numeric_kind(dtype_kind(df.schema[c])) or dtype_kind(df.schema[c]) in NOMINAL_KINDS]
        numeric = {c: is_numeric_kind(dtype_kind(df.schema[c])) for c in names}
        data = {c: (_values(df, c) if numeric[c] else _codes(df[c])) for c in names}
        n = len(names)
        matrix = np.full((n, n), np.nan)
        rows = []
        for i in range(n):
            matrix[i, i] = 1.0
            for j in range(i + 1, n):
                ctx.check_canceled()
                a, b = names[i], names[j]
                r = p = dof = None
                if numeric[a] and numeric[b]:
                    x, y = data[a], data[b]
                    m = np.isfinite(x) & np.isfinite(y)
                    if m.sum() > 2 and np.std(x[m]) > 0 and np.std(y[m]) > 0:
                        res = _quiet(stats.pearsonr, x[m], y[m], alternative=s["alternative"])
                        r, p, dof = float(res.statistic), float(res.pvalue), int(m.sum()) - 2
                elif not numeric[a] and not numeric[b]:
                    r, p, dof = _cramers_v(data[a], data[b], s["max_values"])
                matrix[i, j] = matrix[j, i] = np.nan if r is None else r
                rows.append({"First column name": a, "Second column name": b, "Correlation value": _fl(r),
                             "p value": _fl(p), "Degrees of freedom": dof})
        measures = _frame(MEASURES, rows)
        table = _matrix_table(names, matrix)
        if not ctx.dry and names:
            _view_tables(ctx, "Linear Correlation",
                         ("Correlation matrix", views.chart(_heatmap(names, matrix, "Correlation"))),
                         ("Correlation measure", measures))
        return [measures, table]


RANK_METHODS = ["Spearman's rho", "Kendall's Tau A", "Kendall's Tau B", "Goodman and Kruskal's Gamma"]


def _concordance(x: np.ndarray, y: np.ndarray):
    """Concordant and discordant pair counts (ties excluded)."""
    conc = disc = 0
    for i in range(len(x) - 1):
        sx = np.sign(x[i + 1:] - x[i])
        sy = np.sign(y[i + 1:] - y[i])
        prod = sx * sy
        conc += int((prod > 0).sum())
        disc += int((prod < 0).sum())
    return conc, disc


class RankCorrelation(NodeModel):
    factory = "verknode.analytics.RankCorrelation"
    name = "Rank Correlation"
    category = STATS
    node_type = "Other"
    icon = "ρₛ"
    has_view = True
    short_description = "Computes rank correlation coefficients for pairs of columns."
    description = (
        "Calculates Spearman's rank correlation, Kendall's Tau (A or B) or Goodman and Kruskal's Gamma for "
        "each pair of selected numeric or ordinal (String, ranked lexicographically) columns. p values are "
        "given for Spearman's rho and Kendall's Tau B."
    )
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Correlation measure"), Port(TABLE, "Correlation matrix")]
    fields = [
        F("columns", "columns", "Columns", dict(ALL_COLUMNS)),
        F("method", "enum", "Correlation Type", RANK_METHODS[0], options=RANK_METHODS),
    ]

    def execute(self, inputs, s, ctx):
        from scipy import stats

        df = inputs[0]
        names = [c for c in selected_columns(s["columns"], df.schema)
                 if is_numeric_kind(dtype_kind(df.schema[c])) or dtype_kind(df.schema[c]) in NOMINAL_KINDS]
        data = {}
        for c in names:
            if is_numeric_kind(dtype_kind(df.schema[c])):
                data[c] = _values(df, c)
            else:
                codes = _codes(df[c]).astype(float)
                codes[codes < 0] = np.nan
                data[c] = codes
        n = len(names)
        matrix = np.full((n, n), np.nan)
        rows = []
        method = s["method"]
        for i in range(n):
            matrix[i, i] = 1.0
            for j in range(i + 1, n):
                ctx.check_canceled()
                x, y = data[names[i]], data[names[j]]
                m = np.isfinite(x) & np.isfinite(y)
                x, y = x[m], y[m]
                r = p = dof = None
                if len(x) > 2:
                    if method == "Spearman's rho":
                        res = _quiet(stats.spearmanr, x, y)
                        r, p, dof = res.statistic, res.pvalue, len(x) - 2
                    elif method == "Kendall's Tau B":
                        res = _quiet(stats.kendalltau, x, y, variant="b")
                        r, p = res.statistic, res.pvalue
                    else:
                        conc, disc = _concordance(x, y)
                        if method == "Kendall's Tau A":
                            r = (conc - disc) / (len(x) * (len(x) - 1) / 2)
                        elif conc + disc:
                            r = (conc - disc) / (conc + disc)
                matrix[i, j] = matrix[j, i] = np.nan if _fl(r) is None else r
                rows.append({"First column name": names[i], "Second column name": names[j],
                             "Correlation value": _fl(r), "p value": _fl(p), "Degrees of freedom": dof})
        measures = _frame(MEASURES, rows)
        if not ctx.dry and names:
            _view_tables(ctx, "Rank Correlation",
                         (method, views.chart(_heatmap(names, matrix, method))),
                         ("Correlation measure", measures))
        return [measures, _matrix_table(names, matrix)]


class CorrelationFilter(NodeModel):
    factory = "verknode.analytics.CorrelationFilter"
    name = "Correlation Filter"
    category = STATS
    node_type = "Manipulator"
    icon = "ρ▥"
    short_description = "Removes columns that are highly correlated with other columns."
    description = (
        "Uses the correlation measure table of a <i>Linear Correlation</i> (or <i>Rank Correlation</i>) node "
        "on the second input to filter redundant columns of the first input: the column with the most "
        "correlations above the threshold (absolute value) is kept and all columns correlated with it are "
        "removed; this repeats until no pair above the threshold remains. Columns not in the correlation "
        "table are kept."
    )
    in_ports = [Port(TABLE, "Data table"), Port(TABLE, "Correlation measure", "Output of Linear Correlation")]
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [F("threshold", "float", "Correlation threshold", 0.8,
                "Columns pairs with |correlation| >= threshold are considered redundant.")]
    REQUIRED = ("First column name", "Second column name", "Correlation value")

    def validate(self, s, in_specs):
        spec = in_specs[1] if len(in_specs) > 1 else None
        if hasattr(spec, "keys"):
            missing = [c for c in self.REQUIRED if c not in spec]
            if missing:
                raise InvalidSettingsError(f"Second input is not a correlation measure table (missing {', '.join(missing)})")
        if not 0 <= s["threshold"] <= 1:
            raise InvalidSettingsError("Threshold must be between 0 and 1")

    def configure(self, in_specs, s, ctx):
        self.validate(s, in_specs)
        return [None]  # remaining columns depend on the data

    def execute(self, inputs, s, ctx):
        df, corr = inputs
        order = {c: i for i, c in enumerate(df.columns)}
        edges: dict[str, set] = {}
        for a, b, r in corr.select(self.REQUIRED).iter_rows():
            if a in order and b in order and a != b and _fl(r) is not None and abs(r) >= s["threshold"]:
                edges.setdefault(a, set()).add(b)
                edges.setdefault(b, set()).add(a)
        removed = set()
        while any(edges.values()):
            best = max(edges, key=lambda c: (len(edges[c]), -order[c]))
            for other in edges.pop(best):
                removed.add(other)
                for nb in edges.pop(other, set()):
                    if nb in edges:
                        edges[nb].discard(other)
            for nb in edges.values():
                nb.discard(best)
        if removed:
            ctx.log(f"Removed {len(removed)} correlated column(s): {', '.join(sorted(removed, key=order.get))}")
        return [df.drop([c for c in df.columns if c in removed])]


class LowVarianceFilter(NodeModel):
    factory = "verknode.analytics.LowVarianceFilter"
    name = "Low Variance Filter"
    category = "Manipulation/Column/Filter"
    node_type = "Manipulator"
    icon = "σ²▥"
    short_description = "Removes numeric columns with low variance."
    description = (
        "Removes selected numeric columns whose variance is below or equal to the upper bound. "
        "Non-numeric and unselected columns are kept."
    )
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("threshold", "float", "Variance upper bound", 0.0),
        F("columns", "columns", "Columns", dict(ALL_COLUMNS)),
    ]

    def configure(self, in_specs, s, ctx):
        if s["threshold"] < 0:
            raise InvalidSettingsError("Variance upper bound must not be negative")
        return [None]  # remaining columns depend on the data

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        removed = []
        for c in _numeric(df, s["columns"]):
            x = _finite(_values(df, c))
            var = _fl(np.var(x, ddof=1)) if len(x) > 1 else None
            if var is not None and var <= s["threshold"]:
                removed.append(c)
        if removed:
            ctx.log(f"Removed low variance column(s): {', '.join(removed)}")
        return [df.drop(removed)]


# ----------------------------------------------------------------------
# Crosstab
# ----------------------------------------------------------------------
CROSSTAB_STATS = {"Chi-squared": pl.Float64, "Chi-squared df": pl.Int64, "Chi-squared p-value": pl.Float64,
                  "Fisher test p-value": pl.Float64, "Cramér's V": pl.Float64, "Number of rows": pl.Int64}


class Crosstab(NodeModel):
    factory = "verknode.analytics.Crosstab"
    name = "Crosstab"
    category = STATS
    node_type = "Other"
    icon = "▦χ²"
    has_view = True
    short_description = "Creates a contingency table of two nominal columns with statistics."
    description = (
        "Counts the co-occurrences of the values of the row and column variable (optionally weighted) and "
        "computes expected frequencies, deviations, percentages and cell chi-square values. The statistics "
        "table contains the chi-squared test, Fisher's exact test (2×2 tables) and Cramér's V. Rows with "
        "missing values are ignored."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Crosstab", "One row per cell of the contingency table"),
                 Port(TABLE, "Statistics", "Test statistics")]
    fields = [
        F("row", "column", "Row variable"),
        F("column", "column", "Column variable"),
        F("weight", "column", "Weight column (optional)", None, kinds=NUMERIC_KINDS),
    ]

    def validate(self, s, in_specs):
        if s.get("row") and s["row"] == s.get("column"):
            raise InvalidSettingsError("Row and column variable must be different")

    def execute(self, inputs, s, ctx):
        from scipy import stats

        df = inputs[0]
        rcol = require_column(s, "row", df.schema, "row variable")
        ccol = require_column(s, "column", df.schema, "column variable")
        w = s.get("weight")
        if w and w not in df.columns:
            raise InvalidSettingsError(f'Column "{w}" not found')
        sub = df.select(pl.col(rcol).cast(pl.String).alias("r"), pl.col(ccol).cast(pl.String).alias("c"),
                        (pl.col(w).cast(pl.Float64) if w else pl.lit(1.0)).alias("w"))
        clean = sub.drop_nulls()
        if clean.height < sub.height:
            ctx.warn(f"{sub.height - clean.height} row(s) with missing values ignored")
        if (clean["w"] < 0).any():
            raise ValueError(f'Weight column "{w}" contains negative values')
        rv = sorted(set(clean["r"].to_list()))
        cv = sorted(set(clean["c"].to_list()))
        ri, ci = {v: i for i, v in enumerate(rv)}, {v: i for i, v in enumerate(cv)}
        table = np.zeros((len(rv), len(cv)))
        for r, c, wt in clean.iter_rows():
            table[ri[r], ci[c]] += wt
        total = table.sum()
        rs, cs = table.sum(axis=1), table.sum(axis=0)
        expected = np.outer(rs, cs) / total if total else np.zeros_like(table)
        rows = []
        for i, r in enumerate(rv):
            for j, c in enumerate(cv):
                o, e = table[i, j], expected[i, j]
                rows.append({rcol: r, ccol: c, "Frequency": o, "Expected": e, "Deviation": o - e,
                             "Percent": 100 * o / total if total else None,
                             "Row Percent": 100 * o / rs[i] if rs[i] else None,
                             "Column Percent": 100 * o / cs[j] if cs[j] else None,
                             "Cell Chi-Square": (o - e) ** 2 / e if e else None})
        cross = _frame({rcol: pl.String, ccol: pl.String, "Frequency": pl.Float64, "Expected": pl.Float64,
                        "Deviation": pl.Float64, "Percent": pl.Float64, "Row Percent": pl.Float64,
                        "Column Percent": pl.Float64, "Cell Chi-Square": pl.Float64}, rows)
        st = {"Number of rows": clean.height}
        if len(rv) > 1 and len(cv) > 1 and total:
            mask = expected > 0
            chi2 = float((((table - expected) ** 2)[mask] / expected[mask]).sum())
            dof = (len(rv) - 1) * (len(cv) - 1)
            st.update({"Chi-squared": chi2, "Chi-squared df": dof,
                       "Chi-squared p-value": float(stats.chi2.sf(chi2, dof)),
                       "Cramér's V": math.sqrt(chi2 / (total * (min(len(rv), len(cv)) - 1)))})
            if table.shape == (2, 2):
                st["Fisher test p-value"] = float(stats.fisher_exact(np.rint(table).astype(int))[1])
        statistics = _frame(CROSSTAB_STATS, [st])
        if not ctx.dry:
            head = "".join(f"<th>{views.escape(c)}</th>" for c in cv)
            body = "".join(
                f"<tr><th>{views.escape(r)}</th>"
                + "".join(f'<td class="num">{table[i, j]:g}</td>' for j in range(len(cv)))
                + f'<td class="num"><b>{rs[i]:g}</b></td></tr>' for i, r in enumerate(rv))
            foot = "".join(f'<td class="num"><b>{v:g}</b></td>' for v in cs)
            matrix = (f"<table><thead><tr><th>{views.escape(rcol)} \\ {views.escape(ccol)}</th>{head}"
                      f"<th>Total</th></tr></thead><tbody>{body}<tr><th>Total</th>{foot}"
                      f'<td class="num"><b>{total:g}</b></td></tr></tbody></table>')
            _view_tables(ctx, "Crosstab", ("Frequencies", matrix), ("Statistics", statistics))
        return [cross, statistics]


# ----------------------------------------------------------------------
# Hypothesis tests
# ----------------------------------------------------------------------
def _t_ci(mean_diff, se, dof, conf):
    from scipy import stats

    if se is None or dof is None or not dof > 0 or not math.isfinite(se):
        return None, None
    q = float(stats.t.ppf(0.5 + conf / 2, dof))
    return _fl(mean_diff - q * se), _fl(mean_diff + q * se)


def _t_p(t, dof):
    from scipy import stats

    if t is None or dof is None or not dof > 0:
        return None
    return _fl(2 * stats.t.sf(abs(t), dof))


class SingleSampleTTest(NodeModel):
    factory = "verknode.analytics.SingleSampleTTest"
    name = "Single sample t-test"
    category = TESTS
    node_type = "Other"
    icon = "t₁"
    has_view = True
    short_description = "Tests whether the mean of columns differs from a test value."
    description = "Computes a one-sample Student's t-test for each selected numeric column against the test value."
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Single sample t-test"), Port(TABLE, "Descriptive Statistics")]
    fields = [
        F("columns", "columns", "Test columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("test_value", "float", "Test value", 0.0),
        F("confidence", "float", "Confidence Interval (in %/100)", 0.95),
    ]
    SCHEMA = {"Column": pl.String, "Test value": pl.Float64, "t": pl.Float64, "df": pl.Int64,
              "p-value (2-tailed)": pl.Float64, "Mean Difference": pl.Float64,
              "Confidence Interval (Lower Bound)": pl.Float64, "Confidence Interval (Upper Bound)": pl.Float64}

    def execute(self, inputs, s, ctx):
        _check_confidence(s["confidence"])
        df = inputs[0]
        rows, desc = [], []
        for c in _numeric(df, s["columns"]):
            x = _values(df, c)
            d = _describe(x)
            desc.append({"Column": c, **d})
            diff = d["Mean"] - s["test_value"] if d["Mean"] is not None else None
            se, dof = d["Standard Error Mean"], d["N"] - 1
            t = _fl(diff / se) if diff is not None and se else None
            lo, hi = _t_ci(diff, se, dof, s["confidence"]) if diff is not None else (None, None)
            rows.append({"Column": c, "Test value": s["test_value"], "t": t, "df": dof if dof > 0 else None,
                         "p-value (2-tailed)": _t_p(t, dof), "Mean Difference": _fl(diff),
                         "Confidence Interval (Lower Bound)": lo, "Confidence Interval (Upper Bound)": hi})
        result, descriptive = _frame(self.SCHEMA, rows), _frame(DESCRIPTIVE, desc)
        _view_tables(ctx, "Single sample t-test", ("Single sample t-test", result), ("Descriptive Statistics", descriptive))
        return [result, descriptive]


LEVENE = {"Column": pl.String, "Levene F": pl.Float64, "df 1": pl.Int64, "df 2": pl.Int64, "p-value": pl.Float64}


def _levene(groups):
    from scipy import stats

    groups = [g for g in groups if len(g) > 0]
    n = sum(len(g) for g in groups)
    if len(groups) < 2 or n <= len(groups) or all(len(g) < 2 for g in groups):
        return {"Levene F": None, "df 1": None, "df 2": None, "p-value": None}
    res = _quiet(stats.levene, *groups, center="mean")
    return {"Levene F": _fl(res.statistic), "df 1": len(groups) - 1, "df 2": n - len(groups),
            "p-value": _fl(res.pvalue)}


class IndependentGroupsTTest(NodeModel):
    factory = "verknode.analytics.IndependentGroupsTTest"
    name = "Independent groups t-test"
    category = TESTS
    node_type = "Other"
    icon = "t₂"
    has_view = True
    short_description = "Tests whether the means of two independent groups differ."
    description = (
        "Compares the means of two groups (rows whose grouping column equals group one / group two) for each "
        "selected numeric column, assuming equal variances (Student) and not assuming them (Welch). "
        "Levene's test checks the equality of variances."
    )
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Independent groups t-test"), Port(TABLE, "Levene-Test"),
                 Port(TABLE, "Descriptive Statistics")]
    fields = [
        F("group_column", "column", "Grouping column"),
        F("group_one", "string", "Group one", "", "Value of the grouping column for the first group."),
        F("group_two", "string", "Group two", "", "Value of the grouping column for the second group."),
        F("columns", "columns", "Test columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("confidence", "float", "Confidence Interval (in %/100)", 0.95),
    ]
    SCHEMA = {"Column": pl.String, "Variance Assumption": pl.String, "t": pl.Float64, "df": pl.Float64,
              "p-value (2-tailed)": pl.Float64, "Mean Difference": pl.Float64,
              "Standard Error Difference": pl.Float64, "Confidence Interval (Lower Bound)": pl.Float64,
              "Confidence Interval (Upper Bound)": pl.Float64}

    def validate(self, s, in_specs):
        if not s["group_one"] or not s["group_two"]:
            raise InvalidSettingsError("Specify the values of group one and group two")
        if s["group_one"] == s["group_two"]:
            raise InvalidSettingsError("Group one and group two must be different")
        _check_confidence(s["confidence"])

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        g = require_column(s, "group_column", df.schema, "grouping column")
        labels = df[g].cast(pl.String)
        masks = [(labels == s["group_one"]).fill_null(False), (labels == s["group_two"]).fill_null(False)]
        rows, lev, desc = [], [], []
        for c in _numeric(df, s["columns"]):
            if c == g:
                continue
            xs = [_values(df.filter(m), c) for m in masks]
            ds = [_describe(x) for x in xs]
            for name, x, d in zip((s["group_one"], s["group_two"]), xs, ds):
                desc.append({"Column": c, "Group": name, **d})
            v = [x[~np.isnan(x)] for x in xs]
            lev.append({"Column": c, **_levene(v)})
            n1, n2 = len(v[0]), len(v[1])
            if n1 < 2 or n2 < 2:
                for assumption in ("Equal variances assumed", "Equal variances not assumed"):
                    rows.append({"Column": c, "Variance Assumption": assumption})
                continue
            m1, m2 = v[0].mean(), v[1].mean()
            s1, s2 = np.var(v[0], ddof=1), np.var(v[1], ddof=1)
            diff = float(m1 - m2)
            sp = ((n1 - 1) * s1 + (n2 - 1) * s2) / (n1 + n2 - 2)
            variants = [("Equal variances assumed", math.sqrt(sp * (1 / n1 + 1 / n2)), n1 + n2 - 2)]
            se_w = math.sqrt(s1 / n1 + s2 / n2)
            denom = (s1 / n1) ** 2 / (n1 - 1) + (s2 / n2) ** 2 / (n2 - 1)
            variants.append(("Equal variances not assumed", se_w, (s1 / n1 + s2 / n2) ** 2 / denom if denom else None))
            for assumption, se, dof in variants:
                t = _fl(diff / se) if se else None
                lo, hi = _t_ci(diff, se, dof, s["confidence"])
                rows.append({"Column": c, "Variance Assumption": assumption, "t": t, "df": _fl(dof),
                             "p-value (2-tailed)": _t_p(t, dof), "Mean Difference": diff,
                             "Standard Error Difference": _fl(se),
                             "Confidence Interval (Lower Bound)": lo, "Confidence Interval (Upper Bound)": hi})
        result = _frame(self.SCHEMA, rows)
        levene = _frame(LEVENE, lev)
        descriptive = _frame({"Column": pl.String, "Group": pl.String, **{k: v for k, v in DESCRIPTIVE.items()
                                                                         if k != "Column"}}, desc)
        _view_tables(ctx, "Independent groups t-test", ("Independent groups t-test", result),
                     ("Levene-Test", levene), ("Descriptive Statistics", descriptive))
        return [result, levene, descriptive]


class PairedTTest(NodeModel):
    factory = "verknode.analytics.PairedTTest"
    name = "Paired t-test"
    category = TESTS
    node_type = "Other"
    icon = "tₚ"
    has_view = True
    short_description = "Tests whether the mean difference of paired columns is zero."
    description = "Computes a paired Student's t-test for each pair of numeric columns (rows with a missing value in the pair are ignored)."
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Paired t-test"), Port(TABLE, "Descriptive Statistics")]
    fields = [
        F("pairs", "records", "Column pairs", [{"left": None, "right": None}], fields=[
            F("left", "column", "Left column", kinds=NUMERIC_KINDS),
            F("right", "column", "Right column", kinds=NUMERIC_KINDS),
        ]),
        F("confidence", "float", "Confidence Interval (in %/100)", 0.95),
    ]
    SCHEMA = {"Left Column": pl.String, "Right Column": pl.String, "Mean": pl.Float64,
              "Standard Deviation": pl.Float64, "Standard Error Mean": pl.Float64,
              "Confidence Interval (Lower Bound)": pl.Float64, "Confidence Interval (Upper Bound)": pl.Float64,
              "t": pl.Float64, "df": pl.Int64, "p-value (2-tailed)": pl.Float64}

    def execute(self, inputs, s, ctx):
        _check_confidence(s["confidence"])
        df = inputs[0]
        pairs = [p for p in s["pairs"] if p.get("left") and p.get("right")]
        if not pairs:
            raise InvalidSettingsError("No column pairs selected")
        rows, desc, seen = [], [], set()
        for p in pairs:
            for c in (p["left"], p["right"]):
                if c not in df.columns:
                    raise InvalidSettingsError(f'Column "{c}" not found')
                if c not in seen:
                    seen.add(c)
                    desc.append({"Column": c, **_describe(_values(df, c))})
            x, y = _values(df, p["left"]), _values(df, p["right"])
            m = ~np.isnan(x) & ~np.isnan(y)
            d = x[m] - y[m]
            st = _describe(d)
            dof = st["N"] - 1
            se = st["Standard Error Mean"]
            t = _fl(st["Mean"] / se) if st["Mean"] is not None and se else None
            lo, hi = _t_ci(st["Mean"], se, dof, s["confidence"]) if st["Mean"] is not None else (None, None)
            rows.append({"Left Column": p["left"], "Right Column": p["right"], "Mean": st["Mean"],
                         "Standard Deviation": st["Standard Deviation"], "Standard Error Mean": se,
                         "Confidence Interval (Lower Bound)": lo, "Confidence Interval (Upper Bound)": hi,
                         "t": t, "df": dof if dof > 0 else None, "p-value (2-tailed)": _t_p(t, dof)})
        result, descriptive = _frame(self.SCHEMA, rows), _frame(DESCRIPTIVE, desc)
        _view_tables(ctx, "Paired t-test", ("Paired t-test", result), ("Descriptive Statistics", descriptive))
        return [result, descriptive]


def _groups(df, g):
    labels = df[g].cast(pl.String)
    names = sorted({v for v in labels.to_list() if v is not None})
    return labels, names


class OneWayANOVA(NodeModel):
    factory = "verknode.analytics.OneWayANOVA"
    name = "One-way ANOVA"
    category = TESTS
    node_type = "Other"
    icon = "F"
    has_view = True
    short_description = "Tests whether the means of several groups differ."
    description = (
        "One-way analysis of variance for each selected numeric column across the groups of the grouping "
        "column, with Levene's test of variance homogeneity and descriptive statistics per group."
    )
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Levene-Test"), Port(TABLE, "Group Statistics"), Port(TABLE, "ANOVA")]
    fields = [
        F("group_column", "column", "Grouping column"),
        F("columns", "columns", "Test columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("confidence", "float", "Confidence Interval (in %/100)", 0.95),
    ]
    GROUP = {"Column": pl.String, "Group": pl.String, "N": pl.Int64, "Missing Count": pl.Int64,
             "Mean": pl.Float64, "Standard Deviation": pl.Float64, "Standard Error": pl.Float64,
             "Confidence Interval (Lower Bound)": pl.Float64, "Confidence Interval (Upper Bound)": pl.Float64,
             "Minimum": pl.Float64, "Maximum": pl.Float64}
    ANOVA = {"Column": pl.String, "Source": pl.String, "Sum of Squares": pl.Float64, "df": pl.Int64,
             "Mean Square": pl.Float64, "F": pl.Float64, "p-value": pl.Float64}

    def execute(self, inputs, s, ctx):
        from scipy import stats

        _check_confidence(s["confidence"])
        df = inputs[0]
        g = require_column(s, "group_column", df.schema, "grouping column")
        labels, names = _groups(df, g)
        lev, grp, anova = [], [], []
        for c in _numeric(df, s["columns"]):
            if c == g:
                continue
            x = _values(df, c)
            parts = {n: x[(labels == n).fill_null(False).to_numpy()] for n in names}
            parts["Total"] = x
            for name, v in parts.items():
                d = _describe(v)
                vv = v[~np.isnan(v)]
                lo, hi = _t_ci(d["Mean"], d["Standard Error Mean"], d["N"] - 1, s["confidence"]) \
                    if d["Mean"] is not None else (None, None)
                grp.append({"Column": c, "Group": name, "N": d["N"], "Missing Count": d["Missing Count"],
                            "Mean": d["Mean"], "Standard Deviation": d["Standard Deviation"],
                            "Standard Error": d["Standard Error Mean"],
                            "Confidence Interval (Lower Bound)": lo, "Confidence Interval (Upper Bound)": hi,
                            "Minimum": _fl(vv.min()) if len(vv) else None,
                            "Maximum": _fl(vv.max()) if len(vv) else None})
            groups = [v[~np.isnan(v)] for n, v in parts.items() if n != "Total"]
            groups = [v for v in groups if len(v)]
            lev.append({"Column": c, **_levene(groups)})
            allv = np.concatenate(groups) if groups else np.array([])
            k, n = len(groups), len(allv)
            if k >= 2 and n > k:
                grand = allv.mean()
                ssb = float(sum(len(v) * (v.mean() - grand) ** 2 for v in groups))
                ssw = float(sum(((v - v.mean()) ** 2).sum() for v in groups))
                msb, msw = ssb / (k - 1), ssw / (n - k)
                f = msb / msw if msw else None
                p = _fl(stats.f.sf(f, k - 1, n - k)) if f is not None else None
                anova += [
                    {"Column": c, "Source": "Between Groups", "Sum of Squares": ssb, "df": k - 1,
                     "Mean Square": msb, "F": _fl(f), "p-value": p},
                    {"Column": c, "Source": "Within Groups", "Sum of Squares": ssw, "df": n - k, "Mean Square": msw},
                    {"Column": c, "Source": "Total", "Sum of Squares": ssb + ssw, "df": n - 1},
                ]
            else:
                anova += [{"Column": c, "Source": src} for src in ("Between Groups", "Within Groups", "Total")]
        levene, groups_table, anova_table = _frame(LEVENE, lev), _frame(self.GROUP, grp), _frame(self.ANOVA, anova)
        _view_tables(ctx, "One-way ANOVA", ("ANOVA", anova_table), ("Levene-Test", levene),
                     ("Group Statistics", groups_table))
        return [levene, groups_table, anova_table]


class KruskalWallisTest(NodeModel):
    factory = "verknode.analytics.KruskalWallisTest"
    name = "Kruskal-Wallis Test"
    category = TESTS
    node_type = "Other"
    icon = "H"
    short_description = "Non-parametric test whether several groups come from the same distribution."
    description = "Kruskal-Wallis H-test (rank based one-way ANOVA) for each selected numeric column across the groups of the grouping column."
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Kruskal-Wallis Test")]
    fields = [
        F("group_column", "column", "Grouping column"),
        F("columns", "columns", "Test columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
    ]

    def execute(self, inputs, s, ctx):
        from scipy import stats

        df = inputs[0]
        g = require_column(s, "group_column", df.schema, "grouping column")
        labels, names = _groups(df, g)
        rows = []
        for c in _numeric(df, s["columns"]):
            if c == g:
                continue
            x = _values(df, c)
            groups = [x[(labels == n).fill_null(False).to_numpy()] for n in names]
            groups = [v[~np.isnan(v)] for v in groups]
            groups = [v for v in groups if len(v)]
            h = p = dof = None
            if len(groups) >= 2:
                try:
                    res = _quiet(stats.kruskal, *groups)
                    h, p, dof = _fl(res.statistic), _fl(res.pvalue), len(groups) - 1
                except ValueError:  # all values identical
                    pass
            rows.append({"Column": c, "H": h, "df": dof, "p-value": p, "Groups": len(groups)})
        return [_frame({"Column": pl.String, "H": pl.Float64, "df": pl.Int64, "p-value": pl.Float64,
                        "Groups": pl.Int64}, rows)]


class ShapiroWilkNormalityTest(NodeModel):
    factory = "verknode.analytics.ShapiroWilkNormalityTest"
    name = "Shapiro-Wilk Normality Test"
    category = TESTS
    node_type = "Other"
    icon = "W"
    short_description = "Tests whether columns are normally distributed."
    description = "Shapiro-Wilk test of normality for each selected numeric column (missing values ignored, at least 3 values required)."
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Test results")]
    fields = [F("columns", "columns", "Test columns", {"include": [], "exclude": [], "enforce": "inclusion"})]

    def execute(self, inputs, s, ctx):
        from scipy import stats

        df = inputs[0]
        rows = []
        for c in _numeric(df, s["columns"]):
            x = _finite(_values(df, c))
            w = p = None
            if len(x) >= 3 and np.ptp(x) > 0:
                res = _quiet(stats.shapiro, x)
                w, p = _fl(res.statistic), _fl(res.pvalue)
            rows.append({"Column": c, "W": w, "p-value": p, "N": len(x)})
        return [_frame({"Column": pl.String, "W": pl.Float64, "p-value": pl.Float64, "N": pl.Int64}, rows)]


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------
ACCURACY = {"Class": pl.String, "TruePositives": pl.Int64, "FalsePositives": pl.Int64,
            "TrueNegatives": pl.Int64, "FalseNegatives": pl.Int64, "Recall": pl.Float64, "Precision": pl.Float64,
            "Sensitivity": pl.Float64, "Specificity": pl.Float64, "F-measure": pl.Float64,
            "Accuracy": pl.Float64, "Cohen's kappa": pl.Float64}


def _ratio(a, b):
    return a / b if b else None


class Scorer(NodeModel):
    factory = "verknode.analytics.Scorer"
    name = "Scorer"
    category = SCORING
    node_type = "Other"
    icon = "✓✗"
    has_view = True
    short_description = "Compares actual and predicted class values (confusion matrix)."
    description = (
        "Compares two nominal columns (actual and predicted class) and shows the confusion matrix and "
        "per-class statistics (true/false positives/negatives, recall, precision, sensitivity, "
        "specificity, F-measure) plus overall accuracy and Cohen's kappa. Pushes the flow variables "
        "<code>Accuracy</code>, <code>Error</code>, <code>#Correct classified</code>, "
        "<code>#False classified</code> and <code>Cohen's kappa</code>."
    )
    in_ports = [Port(TABLE, "Input table", "Table with actual and predicted class columns")]
    out_ports = [Port(TABLE, "Confusion matrix", "Rows: actual classes, columns: predicted classes"),
                 Port(TABLE, "Accuracy statistics")]
    fields = [
        F("actual", "column", "First Column (actual)"),
        F("predicted", "column", "Second Column (predicted)"),
        F("missing", "enum", "In case of missing values", "Ignore", options=["Ignore", "Fail"]),
        F("prefix", "string", "Flow variable name prefix", ""),
    ]
    VARS = ("Accuracy", "Error", "#Correct classified", "#False classified", "Cohen's kappa")

    def _push(self, ctx, s, values):
        types = {"#Correct classified": "INTEGER", "#False classified": "INTEGER"}
        for k in self.VARS:
            ctx.push_variable(f"{s['prefix']}{k}", values.get(k, 0), types.get(k, "DOUBLE"))

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        require_column(s, "actual", spec, "actual column")
        require_column(s, "predicted", spec, "predicted column")
        self._push(ctx, s, {})
        return [None, pl.Schema(ACCURACY)]  # confusion matrix columns are the class values

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        a = require_column(s, "actual", df.schema, "actual column")
        p = require_column(s, "predicted", df.schema, "predicted column")
        sub = df.select(pl.col(a).cast(pl.String).alias("a"), pl.col(p).cast(pl.String).alias("p"))
        clean = sub.drop_nulls()
        if clean.height < sub.height:
            if s["missing"] == "Fail":
                raise ValueError(f"{sub.height - clean.height} row(s) with missing values")
            ctx.warn(f"{sub.height - clean.height} row(s) with missing values ignored")
        classes = sorted(set(clean["a"].to_list()) | set(clean["p"].to_list()))
        idx = {c: i for i, c in enumerate(classes)}
        k = len(classes)
        cm = np.zeros((k, k), dtype=np.int64)
        for x, y in clean.iter_rows():
            cm[idx[x], idx[y]] += 1
        n = int(cm.sum())
        correct = int(np.trace(cm))
        acc = correct / n if n else 0.0
        pe = float((cm.sum(axis=1) * cm.sum(axis=0)).sum()) / n ** 2 if n else 0.0
        kappa = (acc - pe) / (1 - pe) if n and pe != 1 else 0.0
        rows = []
        for i, c in enumerate(classes):
            tp = int(cm[i, i])
            fp = int(cm[:, i].sum() - tp)
            fn = int(cm[i, :].sum() - tp)
            tn = n - tp - fp - fn
            recall, precision = _ratio(tp, tp + fn), _ratio(tp, tp + fp)
            f = _ratio(2 * recall * precision, recall + precision) if recall is not None and precision is not None else None
            rows.append({"Class": c, "TruePositives": tp, "FalsePositives": fp, "TrueNegatives": tn,
                         "FalseNegatives": fn, "Recall": recall, "Precision": precision, "Sensitivity": recall,
                         "Specificity": _ratio(tn, tn + fp), "F-measure": f})
        rows.append({"Class": "Overall", "Accuracy": acc, "Cohen's kappa": kappa})
        stats_table = _frame(ACCURACY, rows)
        first = f"{a} \\ {p}"
        matrix = pl.DataFrame([pl.Series(first, classes, dtype=pl.String)]
                              + [pl.Series(c, cm[:, j].tolist(), dtype=pl.Int64) for j, c in enumerate(classes)])
        self._push(ctx, s, {"Accuracy": acc, "Error": 1 - acc if n else 0.0, "#Correct classified": correct,
                            "#False classified": n - correct, "Cohen's kappa": kappa})
        if not ctx.dry:
            head = "".join(f"<th>{views.escape(c)}</th>" for c in classes)
            body = "".join(
                f"<tr><th>{views.escape(c)}</th>" + "".join(
                    f'<td class="num" style="background:{"#d9f2d9" if i == j else ("#fbe0e0" if cm[i, j] else "")}">'
                    f"{cm[i, j]}</td>" for j in range(k)) + "</tr>" for i, c in enumerate(classes))
            matrix_html = (f"<table><thead><tr><th>{views.escape(first)}</th>{head}</tr></thead>"
                           f"<tbody>{body}</tbody></table>")
            summary = views.key_values({"Correct classified": correct, "Wrong classified": n - correct,
                                        "Accuracy": f"{100 * acc:.3f} %", "Error": f"{100 * (1 - acc):.3f} %"
                                        if n else "0 %", "Cohen's kappa (κ)": f"{kappa:.3f}"})
            _view_tables(ctx, f"Confusion Matrix - {a} \\ {p}", ("Confusion matrix", matrix_html),
                         ("Overall", summary), ("Accuracy statistics", stats_table))
        return [matrix, stats_table]


NUMERIC_STATS = ["R^2", "mean absolute error", "mean squared error", "root mean squared error",
                 "mean signed difference", "mean absolute percentage error", "adjusted R^2"]


class NumericScorer(NodeModel):
    factory = "verknode.analytics.NumericScorer"
    name = "Numeric Scorer"
    category = SCORING
    node_type = "Other"
    icon = "R²"
    has_view = True
    short_description = "Computes error statistics of numeric predictions."
    description = (
        "Compares a numeric reference column with a predicted column and computes R², mean absolute error, "
        "mean squared error, root mean squared error, mean signed difference, mean absolute percentage "
        "error and (optionally) adjusted R². The values are also pushed as flow variables."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Statistics")]
    fields = [
        F("reference", "column", "Reference column", kinds=NUMERIC_KINDS),
        F("predicted", "column", "Predicted column", kinds=NUMERIC_KINDS),
        F("output", "string", "Output column name", "Value"),
        F("adjusted", "bool", "Compute adjusted R²", False),
        F("predictors", "int", "Number of predictors", 1, show_if={"adjusted": True}),
        F("prefix", "string", "Flow variable name prefix", ""),
    ]

    def _names(self, s):
        return NUMERIC_STATS if s["adjusted"] else NUMERIC_STATS[:-1]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        require_column(s, "reference", spec, "reference column")
        require_column(s, "predicted", spec, "predicted column")
        for name in self._names(s):
            ctx.push_variable(f"{s['prefix']}{name}", 0.0, "DOUBLE")
        return [pl.Schema({"Statistic": pl.String, s["output"] or "Value": pl.Float64})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        ref = require_column(s, "reference", df.schema, "reference column")
        pred = require_column(s, "predicted", df.schema, "predicted column")
        y, yp = _values(df, ref), _values(df, pred)
        m = np.isfinite(y) & np.isfinite(yp)
        if m.sum() < len(y):
            ctx.warn(f"{len(y) - m.sum()} row(s) with missing values ignored")
        y, yp = y[m], yp[m]
        n = len(y)
        err = yp - y
        values = {}
        if n:
            sst = float(((y - y.mean()) ** 2).sum())
            sse = float((err ** 2).sum())
            r2 = 1 - sse / sst if sst else None
            nz = y != 0
            values = {"R^2": r2, "mean absolute error": float(np.abs(err).mean()),
                      "mean squared error": sse / n, "root mean squared error": math.sqrt(sse / n),
                      "mean signed difference": float(err.mean()),
                      "mean absolute percentage error": float(np.abs(err[nz] / y[nz]).mean()) if nz.any() else None}
            k = s["predictors"]
            if s["adjusted"] and r2 is not None and n - k - 1 > 0:
                values["adjusted R^2"] = 1 - (1 - r2) * (n - 1) / (n - k - 1)
        names = self._names(s)
        out = pl.DataFrame([pl.Series("Statistic", names, dtype=pl.String),
                            pl.Series(s["output"] or "Value", [_fl(values.get(k)) for k in names], dtype=pl.Float64)])
        for name in names:
            ctx.push_variable(f"{s['prefix']}{name}", _fl(values.get(name)) or 0.0, "DOUBLE")
        if not ctx.dry:
            parts = [("Statistics", views.key_values({k: f"{v:.6g}" if v is not None else "?"
                                                     for k, v in ((k, _fl(values.get(k))) for k in names)}))]
            if n:
                fig, ax = views.figure(6, 5)
                ax.scatter(y, yp, s=12, alpha=0.7)
                lo, hi = float(min(y.min(), yp.min())), float(max(y.max(), yp.max()))
                ax.plot([lo, hi], [lo, hi], color="#999", linestyle="--", linewidth=1)
                ax.set_xlabel(ref)
                ax.set_ylabel(pred)
                ax.set_title("Predicted vs. reference")
                parts.append(("Predicted vs. reference", views.chart(views.svg(fig))))
            _view_tables(ctx, "Numeric Scorer", *parts)
        return [out]


class ROCCurve(NodeModel):
    factory = "verknode.analytics.ROCCurve"
    name = "ROC Curve"
    category = SCORING
    node_type = "Visualizer"
    icon = "ROC"
    has_view = True
    short_description = "Draws ROC curves and computes the area under the curve."
    description = (
        "Draws receiver operating characteristic curves for one or more columns with class probabilities "
        "(or scores) of the positive class, and computes the area under each curve (AUC). The chart is "
        "also available as image."
    )
    in_ports = [Port(TABLE, "Table with class column and probabilities")]
    out_ports = [Port(TABLE, "Area Under Curve"), Port(IMAGE, "Image", "ROC curve as SVG")]
    fields = [
        F("class_column", "column", "Class column"),
        F("positive", "string", "Positive class value", "", "Value of the class column treated as positive."),
        F("columns", "columns", "Columns containing the positive class probabilities",
          {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("title", "string", "Chart title", "ROC Curve"),
    ]
    AUC = {"Column": pl.String, "Area Under Curve": pl.Float64}

    def configure(self, in_specs, s, ctx):
        require_column(s, "class_column", in_specs[0], "class column")
        if not s["positive"]:
            raise InvalidSettingsError("No positive class value specified")
        return [pl.Schema(self.AUC), ImagePortObject(b"")]

    def execute(self, inputs, s, ctx):
        from sklearn.metrics import roc_auc_score, roc_curve

        df = inputs[0]
        cls = require_column(s, "class_column", df.schema, "class column")
        if not s["positive"]:
            raise InvalidSettingsError("No positive class value specified")
        cols = [c for c in _numeric(df, s["columns"]) if c != cls]
        if not cols:
            raise InvalidSettingsError("No probability columns selected")
        labels = df[cls].cast(pl.String)
        y = (labels == s["positive"]).to_numpy()
        valid_label = labels.is_not_null().to_numpy()
        fig, ax = views.figure(6, 5.5)
        ax.plot([0, 1], [0, 1], color="#999", linestyle="--", linewidth=1, label="random")
        rows = []
        for c in cols:
            score = _values(df, c)
            m = np.isfinite(score) & valid_label
            auc = None
            if len(np.unique(y[m])) == 2:
                fpr, tpr, _ = roc_curve(y[m], score[m])
                auc = float(roc_auc_score(y[m], score[m]))
                ax.plot(fpr, tpr, linewidth=2, label=f"{c} (AUC {auc:.3f})")
            elif not ctx.dry:
                ctx.warn("Both positive and negative examples are required to compute ROC curves")
            rows.append({"Column": c, "Area Under Curve": auc})
        ax.set_xlabel("False positive rate")
        ax.set_ylabel("True positive rate")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1.01)
        ax.set_title(s["title"] or "ROC Curve")
        ax.legend(loc="lower right", fontsize=8)
        svg = views.svg(fig)
        table = _frame(self.AUC, rows)
        _view_tables(ctx, s["title"] or "ROC Curve", ("", views.chart(svg)), ("Area Under Curve", table))
        return [table, ImagePortObject(svg.encode("utf-8"))]


# ----------------------------------------------------------------------
# Cross validation
# ----------------------------------------------------------------------
SAMPLING = ["Random sampling", "Linear sampling", "Stratified sampling"]


class XPartitioner(NodeModel):
    factory = "verknode.analytics.XPartitioner"
    closed_by = ["verknode.analytics.XAggregator"]
    name = "X-Partitioner"
    category = SCORING
    node_type = "LoopStart"
    icon = "X⟳"
    loop_role = "start"
    short_description = "Starts a cross validation loop."
    description = (
        "Splits the input table into k folds. In each iteration one fold is the test set and the remaining "
        "folds the training set. Use with an <i>X-Aggregator</i> that collects the predictions on the test "
        "sets. Pushes <code>currentIteration</code> and <code>maxIterations</code>."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Training data"), Port(TABLE, "Test data")]
    fields = [
        F("validations", "int", "Number of validations", 10),
        F("leave_one_out", "bool", "Leave-one-out", False, "One validation per row (overrides the number of validations)."),
        F("sampling", "enum", "Sampling", "Random sampling", options=SAMPLING, show_if={"leave_one_out": False}),
        F("class_column", "column", "Class column", None, "Column whose value distribution is kept in each fold.",
          show_if={"sampling": "Stratified sampling"}),
        F("seed", "int", "Random seed (-1: none)", 42, show_if={"sampling": ["Random sampling", "Stratified sampling"]}),
    ]

    def validate(self, s, in_specs):
        if not s["leave_one_out"] and s["validations"] < 2:
            raise InvalidSettingsError("Number of validations must be at least 2")
        if not s["leave_one_out"] and s["sampling"] == "Stratified sampling":
            require_column(s, "class_column", in_specs[0] if in_specs else None, "class column")

    def _folds(self, df, s) -> tuple[np.ndarray, int]:
        n = df.height
        if n == 0:
            return np.zeros(0, dtype=int), 1
        if s["leave_one_out"]:
            return np.arange(n), n
        k = min(s["validations"], n)
        rng = np.random.default_rng(None if s["seed"] == -1 else s["seed"])
        folds = np.empty(n, dtype=int)
        if s["sampling"] == "Linear sampling":
            folds[:] = np.arange(n) * k // n
        elif s["sampling"] == "Random sampling":
            folds[rng.permutation(n)] = np.arange(n) * k // n
        else:
            labels = df[s["class_column"]].cast(pl.String).fill_null("\0missing").to_numpy()
            offset = 0
            for value in sorted(set(labels)):
                idx = rng.permutation(np.flatnonzero(labels == value))
                folds[idx] = (np.arange(len(idx)) + offset) % k
                offset += len(idx)
        return folds, k

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        state = ctx.loop.setdefault("state", {})
        if "folds" not in state:
            state["folds"], state["k"] = self._folds(df, s)
        folds, k = state["folds"], state["k"]
        it = ctx.loop["iteration"]
        ctx.loop["last"] = it >= k - 1
        ctx.loop["total"] = k  # (the canvas shows "iteration / total")
        ctx.push_variable("maxIterations", int(k), "INTEGER")
        ctx.push_variable("currentIteration", int(it), "INTEGER")
        test = pl.Series(folds == it, dtype=pl.Boolean)
        return [df.filter(~test), df.filter(test)]


class XAggregator(NodeModel):
    factory = "verknode.analytics.XAggregator"
    name = "X-Aggregator"
    category = SCORING
    node_type = "LoopEnd"
    icon = "X⟲"
    loop_role = "end"
    short_description = "Collects the predictions of a cross validation loop."
    description = (
        "Ends a cross validation loop started by an <i>X-Partitioner</i>. Collects the test sets with their "
        "predictions from all iterations and computes the error per fold: the misclassification rate for "
        "nominal targets, the mean squared error for numeric targets."
    )
    in_ports = [Port(TABLE, "Test data with predictions")]
    out_ports = [Port(TABLE, "Prediction table", "Predictions of all folds"),
                 Port(TABLE, "Error rates", "Error statistics per fold")]
    fields = [
        F("target", "column", "Target column"),
        F("prediction", "column", "Prediction column"),
        F("add_fold", "bool", "Add column with fold id", True),
    ]
    ERRORS = {"Fold Index": pl.Int32, "Error in %": pl.Float64, "Size of Test Set": pl.Int64,
              "Error Count": pl.Int64, "Mean Squared Error": pl.Float64}

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        t = require_column(s, "target", df.schema, "target column")
        p = require_column(s, "prediction", df.schema, "prediction column")
        it = ctx.loop["iteration"]
        numeric = is_numeric_kind(dtype_kind(df.schema[t])) and is_numeric_kind(dtype_kind(df.schema[p]))
        row = {"Fold Index": it, "Size of Test Set": df.height}
        if numeric:
            e = _values(df, p) - _values(df, t)
            e = e[np.isfinite(e)]
            row["Mean Squared Error"] = _fl((e ** 2).mean()) if len(e) else None
        else:
            wrong = int((df[t].cast(pl.String) != df[p].cast(pl.String)).fill_null(True).sum())
            row["Error Count"] = wrong
            row["Error in %"] = 100.0 * wrong / df.height if df.height else None
        part = df.with_columns(pl.lit(it, pl.Int32).alias("Fold Index")) if s["add_fold"] else df
        ctx.loop["collected"].append((part, row))
        if not ctx.loop["last"]:
            return None
        parts = [d for d, _ in ctx.loop["collected"]]
        errors = _frame(self.ERRORS, [r for _, r in ctx.loop["collected"]])
        total = errors["Size of Test Set"].sum()
        if not numeric and total:
            ctx.log(f"Cross validation error: {100.0 * errors['Error Count'].sum() / total:.3f} %")
        return [pl.concat(parts, how="diagonal_relaxed"), errors]
