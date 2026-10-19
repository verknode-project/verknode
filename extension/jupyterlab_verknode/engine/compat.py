"""Differences between polars versions the engine runs on - polars 2 here, polars 1.18 in the browser
(JupyterLite's Pyodide) - and the browser's lack of threads.

- is_in(expr, values): membership in a Series (polars 2 wants it imploded, polars 1.18 does not).
- join_nulls(): the join argument that matches nulls (nulls_equal, formerly join_nulls).
- WASM: running in the browser (Pyodide), where threads cannot be started (see jupyterlab_verknode.lite).
"""
from __future__ import annotations

import inspect
import sys

import polars as pl

WASM = sys.platform == "emscripten"


def _imploded_is_in() -> bool:
    try:
        # (three rows against an imploded Series of two: polars 1.18 compares row by row and fails)
        out = pl.DataFrame({"a": [1, 2, 3]}).select(pl.col("a").is_in(pl.Series([1, 2]).implode()))
        return out["a"].to_list() == [True, True, False]
    except Exception:  # noqa: BLE001 - (older polars: a list of lists, shapes do not match)
        return False


IMPLODE = _imploded_is_in()
_NULLS = "nulls_equal" if "nulls_equal" in inspect.signature(pl.DataFrame.join).parameters else "join_nulls"


def _parquet_missing(e: Exception) -> bool:
    # (Pyodide's polars is built without its Parquet reader and writer)
    return isinstance(e, AttributeError) or "parquet" in str(e).lower() and "feature" in str(e).lower()


def read_parquet(source) -> pl.DataFrame:
    """pl.read_parquet - in the browser through pyarrow (Pyodide's polars has no working Parquet)."""
    if WASM:
        import pyarrow.parquet as pq

        return pl.from_arrow(pq.read_table(source))
    try:
        return pl.read_parquet(source)
    except Exception as e:  # noqa: BLE001
        if not _parquet_missing(e):
            raise
        import pyarrow.parquet as pq

        if hasattr(source, "seek"):
            source.seek(0)
        return pl.from_arrow(pq.read_table(source))


def to_arrow(df: pl.DataFrame):
    """df as a pyarrow Table. In the browser column by column: Pyodide's polars panics in to_arrow
    ("capacity overflow") on frames it made itself."""
    if not WASM:
        return df.to_arrow()
    import pyarrow as pa

    def arrow_type(t):
        if t == pl.Boolean:
            return pa.bool_()
        if t.is_integer() or t.is_float():
            return {pl.Int8: pa.int8(), pl.Int16: pa.int16(), pl.Int32: pa.int32(), pl.Int64: pa.int64(),
                    pl.UInt8: pa.uint8(), pl.UInt16: pa.uint16(), pl.UInt32: pa.uint32(), pl.UInt64: pa.uint64(),
                    pl.Float32: pa.float32(), pl.Float64: pa.float64()}.get(t)
        if t in (pl.String, pl.Categorical) or isinstance(t, pl.Enum):
            return pa.string()
        if t == pl.Date:
            return pa.date32()
        if t == pl.Time:
            return pa.time64("us")
        if isinstance(t, pl.Datetime):
            return pa.timestamp(t.time_unit or "us", t.time_zone)
        if isinstance(t, pl.Duration):
            return pa.duration(t.time_unit or "us")
        if t == pl.Binary:
            return pa.binary()
        return None  # (lists, structs, decimals: inferred from the values)

    arrays = []
    for s in df.get_columns():
        typ = arrow_type(s.dtype)
        values = s.cast(pl.String).to_list() if isinstance(s.dtype, (pl.Categorical, pl.Enum)) else s.to_list()
        arrays.append(pa.array(values, type=typ))
    return pa.table(arrays, names=df.columns) if arrays else pa.table({})


def write_parquet(df: pl.DataFrame, target, **kw) -> None:
    """df.write_parquet - in the browser through pyarrow (Pyodide's polars panics writing Parquet)."""
    if WASM:
        import pyarrow.parquet as pq

        comp = kw.get("compression") or "snappy"
        pq.write_table(to_arrow(df), target, compression="none" if comp == "uncompressed" else comp)
        return
    try:
        df.write_parquet(target, **kw)
    except Exception as e:  # noqa: BLE001
        if not _parquet_missing(e):
            raise
        import pyarrow.parquet as pq

        if hasattr(target, "seek"):
            target.seek(0)
            target.truncate()
        pq.write_table(df.to_arrow(), target, compression=kw.get("compression", "snappy") if kw.get("compression") != "uncompressed" else "none")


def is_in(expr: pl.Expr, values: pl.Series) -> pl.Expr:
    """expr's values that are among values (a Series)."""
    return expr.is_in(values.implode() if IMPLODE else values)


def join_nulls(flag: bool = True) -> dict:
    """The keyword argument of DataFrame.join that makes null keys match."""
    return {_NULLS: flag}
