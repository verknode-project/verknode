"""Mapping between polars dtypes and column types, JSON conversion."""
from __future__ import annotations

import datetime as dt
import decimal
import math

import polars as pl

TYPE_NAMES = {
    "string": "String",
    "integer": "Number (integer)",
    "long": "Number (long)",
    "double": "Number (double)",
    "boolean": "Boolean",
    "date": "Local Date",
    "datetime": "Local Date Time",
    "time": "Local Time",
    "duration": "Duration",
    "list": "Collection",
    "struct": "Struct",
    "binary": "Binary",
    "molecule": "RDKit Molecule",
    "null": "Missing",
    "other": "Other",
}

# RDKit molecules are stored as a struct holding the pickled molecule
# (see jupyterlab_verknode.chem), so they survive parquet persistence.
MOL_FIELD = "rdkit_mol"
MOL_DTYPE = pl.Struct({MOL_FIELD: pl.Binary})

# Types offered in dialogs (Table Creator, Column Type Converter, ...)
CELL_TYPES = ["String", "Integer", "Long", "Double", "Boolean", "Date", "Date&Time", "Time"]

CELL_DTYPES = {
    "String": pl.String,
    "Integer": pl.Int32,
    "Long": pl.Int64,
    "Double": pl.Float64,
    "Boolean": pl.Boolean,
    "Date": pl.Date,
    "Date&Time": pl.Datetime("us"),
    "Time": pl.Time,
}


def dtype_kind(t) -> str:
    if t == pl.Boolean:
        return "boolean"
    if t in (pl.Int64, pl.UInt64, pl.UInt32):
        return "long"
    if t.is_integer():
        return "integer"
    if t.is_float() or isinstance(t, pl.Decimal):
        return "double"
    if t == pl.String or t == pl.Categorical or isinstance(t, pl.Enum):
        return "string"
    if t == pl.Date:
        return "date"
    if isinstance(t, pl.Datetime):
        return "datetime"
    if t == pl.Time:
        return "time"
    if isinstance(t, pl.Duration):
        return "duration"
    if isinstance(t, (pl.List, pl.Array)):
        return "list"
    if t == MOL_DTYPE:
        return "molecule"
    if isinstance(t, pl.Struct):
        return "struct"
    if t == pl.Binary:
        return "binary"
    if t == pl.Null:
        return "null"
    return "other"


def type_name(t) -> str:
    kind = dtype_kind(t)
    if kind == "list":
        return f"Collection of {type_name(t.inner)}"
    if kind == "datetime" and getattr(t, "time_zone", None):
        return "Zoned Date Time"
    return TYPE_NAMES[kind]


def spec_to_json(schema) -> list:
    return [
        {"name": name, "type": type_name(t), "kind": dtype_kind(t), "dtype": str(t)}
        for name, t in schema.items()
    ]


def is_numeric_kind(kind: str) -> bool:
    return kind in ("integer", "long", "double")


def json_value(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return v
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [json_value(x) for x in v]
    if isinstance(v, dict):
        return {k: json_value(x) for k, x in v.items()}
    return str(v)


def parse_cell(text, cell_type: str):
    """Convert user-entered text into a Python value of the given cell type."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if text == "" and cell_type != "String":
        return None
    if cell_type == "String":
        return text
    s = text.strip()
    if cell_type in ("Integer", "Long"):
        return int(float(s)) if any(c in s for c in ".eE") else int(s)
    if cell_type == "Double":
        return float(s)
    if cell_type == "Boolean":
        low = s.lower()
        if low in ("true", "1", "yes", "y", "t"):
            return True
        if low in ("false", "0", "no", "n", "f"):
            return False
        raise ValueError(f"Not a boolean: {text!r}")
    if cell_type == "Date":
        return dt.date.fromisoformat(s)
    if cell_type == "Date&Time":
        return dt.datetime.fromisoformat(s)
    if cell_type == "Time":
        return dt.time.fromisoformat(s)
    raise ValueError(f"Unknown type: {cell_type}")
