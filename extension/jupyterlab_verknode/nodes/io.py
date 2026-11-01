"""IO nodes: readers, writers and the Table Creator."""
from __future__ import annotations

import os

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE
from verknode.node import CELL_DTYPES, CELL_TYPES, dtype_kind, parse_cell
from jupyterlab_verknode.engine.compat import read_parquet, write_parquet

PATH_HELP = "Absolute path, or relative to the folder containing the workflow (.vkf) file."


def _unescape(sep: str) -> str:
    return sep.encode("utf-8").decode("unicode_escape") if "\\" in sep else sep


def _existing(ctx, path):
    p = ctx.resolve_path(path)
    if not os.path.isfile(p):
        raise InvalidSettingsError(f"File does not exist: {p}")
    return p


class CSVReader(NodeModel):
    factory = "verknode.io.CSVReader"
    name = "CSV Reader"
    category = "IO/Read"
    node_type = "Source"
    icon = "CSV"
    short_description = "Reads a CSV file into a table."
    description = "Reads a delimiter separated text file. Column types are detected automatically."
    out_ports = [Port(TABLE, "File Table", "Table read from the CSV file")]
    fields = [
        F("path", "string", "File", "", PATH_HELP, path=True, extensions=[".csv", ".tsv", ".txt", ".gz"]),
        F("delimiter", "string", "Column delimiter", ",", "Use \\t for tab."),
        F("has_header", "bool", "Has column header", True),
        F("skip_rows", "int", "Skip first lines", 0),
        F("null_values", "string", "Missing value pattern", "", "Text read as missing value (empty: none)."),
        F("infer_schema_length", "int", "Rows used to detect types", 10000, "0 reads all columns as String."),
        F("try_parse_dates", "bool", "Detect date & time columns", True),
    ]

    def _kwargs(self, s):
        return dict(
            separator=_unescape(s["delimiter"] or ","),
            has_header=s["has_header"],
            skip_rows=s["skip_rows"],
            null_values=s["null_values"] or None,
            infer_schema_length=s["infer_schema_length"],
            try_parse_dates=s["try_parse_dates"],
        )

    def configure(self, in_specs, s, ctx):
        p = _existing(ctx, s["path"])
        return [pl.scan_csv(p, **self._kwargs(s)).collect_schema()]

    def execute(self, inputs, s, ctx):
        p = _existing(ctx, s["path"])
        df = pl.read_csv(p, **self._kwargs(s))
        ctx.log(f"Read {df.height} rows from {p}")
        return [df]


class ParquetReader(NodeModel):
    factory = "verknode.io.ParquetReader"
    name = "Parquet Reader"
    category = "IO/Read"
    node_type = "Source"
    icon = "PQ"
    short_description = "Reads a Parquet file into a table."
    description = "Reads an Apache Parquet file, preserving all column types."
    out_ports = [Port(TABLE, "File Table", "Table read from the Parquet file")]
    fields = [F("path", "string", "File", "", PATH_HELP, path=True, extensions=[".parquet", ".pq"])]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(pl.read_parquet_schema(_existing(ctx, s["path"])))]

    def execute(self, inputs, s, ctx):
        return [read_parquet(_existing(ctx, s["path"]))]


class _Writer(NodeModel):
    category = "IO/Write"
    node_type = "Sink"
    in_ports = [Port(TABLE, "Input table", "Table to write")]
    out_ports = []

    def _target(self, s, ctx):
        p = ctx.resolve_path(s["path"])
        if os.path.exists(p) and s["if_exists"] == "Fail":
            raise InvalidSettingsError(f"File exists and must not be overwritten: {p}")
        return p

    def configure(self, in_specs, s, ctx):
        ctx.resolve_path(s["path"])
        return []

    def execute(self, inputs, s, ctx):
        p = self._target(s, ctx)
        if s.get("create_dirs", True):
            os.makedirs(os.path.dirname(p), exist_ok=True)
        self.write(inputs[0], p, s)
        ctx.log(f"Wrote {inputs[0].height} rows to {p}")
        return []


class CSVWriter(_Writer):
    factory = "verknode.io.CSVWriter"
    name = "CSV Writer"
    icon = "CSV"
    short_description = "Writes a table to a CSV file."
    description = "Writes the input table to a delimiter separated text file. Molecule columns are written as SMILES."
    fields = [
        F("path", "string", "File", "", PATH_HELP, path="save", extensions=[".csv", ".tsv", ".txt", ".gz"]),
        F("delimiter", "string", "Column delimiter", ","),
        F("include_header", "bool", "Write column header", True),
        F("if_exists", "enum", "If file exists", "Overwrite", options=["Overwrite", "Append", "Fail"]),
        F("create_dirs", "bool", "Create missing folders", True),
    ]

    def write(self, df, p, s):
        if any(dtype_kind(t) == "molecule" for t in df.schema.values()):
            from verknode.node import chem
            molecules_to_smiles = chem.molecules_to_smiles

            df = molecules_to_smiles(df)
        append = s["if_exists"] == "Append" and os.path.exists(p)
        with open(p, "a" if append else "w", encoding="utf-8", newline="") as fh:
            df.write_csv(fh, separator=_unescape(s["delimiter"] or ","),
                         include_header=s["include_header"] and not append)


class ParquetWriter(_Writer):
    factory = "verknode.io.ParquetWriter"
    name = "Parquet Writer"
    icon = "PQ"
    short_description = "Writes a table to a Parquet file."
    description = "Writes the input table to an Apache Parquet file, preserving column types."
    fields = [
        F("path", "string", "File", "", PATH_HELP, path="save", extensions=[".parquet", ".pq"]),
        F("compression", "enum", "Compression", "zstd", options=["zstd", "snappy", "lz4", "gzip", "uncompressed"]),
        F("if_exists", "enum", "If file exists", "Overwrite", options=["Overwrite", "Fail"]),
        F("create_dirs", "bool", "Create missing folders", True),
    ]

    def write(self, df, p, s):
        write_parquet(df, p, compression=s["compression"])


class TableCreator(NodeModel):
    factory = "verknode.io.TableCreator"
    name = "Table Creator"
    category = "IO/Other"
    node_type = "Source"
    icon = "▦"
    short_description = "Manually enter a table."
    description = (
        "Creates a table from values entered in the dialog. Choose a type for each column; "
        "cells are parsed accordingly (dates as ISO 8601, e.g. 2024-01-31). Empty cells are missing values."
    )
    out_ports = [Port(TABLE, "Manually created table")]
    fields = [
        F("table", "tablecreator", "Table", {
            "columns": [{"name": "column1", "type": "String"}, {"name": "column2", "type": "Integer"}],
            "rows": [["a", "1"], ["b", "2"], ["c", "3"]],
        }, "Column names, types and cell values.", options=CELL_TYPES),
    ]

    def execute(self, inputs, s, ctx):
        table = s["table"] or {}
        cols = table.get("columns", [])
        rows = table.get("rows", [])
        names = [c["name"] for c in cols]
        if len(set(names)) != len(names):
            raise InvalidSettingsError("Column names must be unique")
        data = {}
        for j, c in enumerate(cols):
            ctype = c.get("type", "String")
            values = []
            for i, row in enumerate(rows):
                cell = row[j] if j < len(row) else None
                try:
                    values.append(parse_cell(cell, ctype))
                except (TypeError, ValueError) as e:
                    raise InvalidSettingsError(f'Row {i + 1}, column "{c["name"]}": {e}') from None
            data[c["name"]] = pl.Series(c["name"], values, dtype=CELL_DTYPES[ctype])
        return [pl.DataFrame(data)]
