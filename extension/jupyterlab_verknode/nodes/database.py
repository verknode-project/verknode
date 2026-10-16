"""Database nodes on SQLite.

DB Session ports carry the connection (database file); DB Data ports carry
a SQL SELECT query that in-database nodes wrap as a subquery::

    SELECT ... FROM (<incoming query>) AS "table_123"

Nothing is read until a DB Reader executes the final query.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import sqlite3
import urllib.parse
import zlib

import polars as pl

from verknode.node import (
    DB_DATA,
    DB_SESSION,
    DBDataPortObject,
    DBSessionPortObject,
    F,
    InvalidSettingsError,
    NodeModel,
    Port,
    TABLE,
)
from verknode.node import dtype_kind, json_value
from .io import PATH_HELP
from .manipulation import selected_columns, substitute_variables

SESSION_IN = Port(DB_SESSION, "DB Connection", "Database session")
SESSION_OUT = Port(DB_SESSION, "DB Connection", "Database session (unchanged)")
DATA_IN = Port(DB_DATA, "DB Data", "Database query")
DATA_OUT = Port(DB_DATA, "DB Data", "Database query (not executed until read)")

SQL_TYPES = ["TEXT", "INTEGER", "REAL", "BOOLEAN", "DATE", "TIMESTAMP", "TIME", "NUMERIC", "BLOB"]
DB_HELP = (
    " The query is not executed; downstream in-database nodes extend it and a "
    "<i>DB Reader</i> reads the result into a table."
)


# ----------------------------------------------------------------------
# SQL helpers
# ----------------------------------------------------------------------
def q(name: str) -> str:
    """Quote an identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def strip_sql(sql: str) -> str:
    return (sql or "").strip().rstrip(";").strip()


def subquery(query: str) -> str:
    return f"({query}) AS {q(f'table_{zlib.crc32(query.encode()) % 10**8}')}"


def _ro_connect(session: DBSessionPortObject) -> sqlite3.Connection:
    """Read-only connection that never creates the database file."""
    if not os.path.isfile(session.path):
        raise FileNotFoundError(f"Database file does not exist: {session.path}")
    return sqlite3.connect(f"file:{urllib.parse.quote(session.path)}?mode=ro", uri=True)


def _rw_connect(session: DBSessionPortObject) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(session.path) or ".", exist_ok=True)
    return session.connect()


def _decl_dtype(decl: str):
    d = (decl or "").upper()
    if not d:
        return None
    if "BOOL" in d:
        return pl.Boolean
    if "INT" in d:
        return pl.Int64
    if any(t in d for t in ("CHAR", "CLOB", "TEXT")):
        return pl.String
    if any(t in d for t in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return pl.Float64
    if "TIMESTAMP" in d or "DATETIME" in d:
        return pl.Datetime("us")
    if d.startswith("DATE"):
        return pl.Date
    if d.startswith("TIME"):
        return pl.Time
    if "BLOB" in d:
        return pl.Binary
    return None


def _infer_dtype(values):
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return pl.String
    if kinds <= {int}:
        return pl.Int64
    if kinds <= {int, float}:
        return pl.Float64
    if kinds <= {bytes}:
        return pl.Binary
    return pl.String


def _columns_and_rows(con, query: str, limit: int | None = None):
    """Column names, declared types and rows of a query (via a temp view)."""
    con.execute('DROP VIEW IF EXISTS temp."__verknode_view"')
    con.execute(f'CREATE TEMP VIEW "__verknode_view" AS {query}')
    try:
        info = con.execute('PRAGMA temp.table_info("__verknode_view")').fetchall()
        sql = 'SELECT * FROM temp."__verknode_view"' + (f" LIMIT {limit}" if limit is not None else "")
        rows = con.execute(sql).fetchall()
    finally:
        con.execute('DROP VIEW IF EXISTS temp."__verknode_view"')
    return [r[1] for r in info], [r[2] for r in info], rows


def _schema(names, decls, rows) -> pl.Schema:
    schema = {}
    for i, (n, d) in enumerate(zip(names, decls)):
        schema[n] = _decl_dtype(d) or _infer_dtype([r[i] for r in rows])
    return pl.Schema(schema)


def query_schema(session: DBSessionPortObject, query: str) -> pl.Schema:
    with _ro_connect(session) as con:
        names, decls, rows = _columns_and_rows(con, query, limit=100)
    return _schema(names, decls, rows)


def _convert(value, dtype):
    if value is None:
        return None
    try:
        if dtype == pl.Boolean:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "t", "yes", "y")
            return bool(value)
        if dtype == pl.Date:
            return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
        if dtype == pl.Datetime("us"):
            return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
        if dtype == pl.Time:
            return value if isinstance(value, dt.time) else dt.time.fromisoformat(str(value))
        if dtype == pl.Int64:
            return int(value) if not isinstance(value, str) else int(float(value))
        if dtype == pl.Float64:
            return float(value)
        if dtype == pl.String:
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return None
    return value


def read_query(session: DBSessionPortObject, query: str, ctx=None) -> pl.DataFrame:
    with _ro_connect(session) as con:
        names, decls, rows = _columns_and_rows(con, query)
    schema = _schema(names, decls, rows)
    cols = {}
    for i, (name, dtype) in enumerate(schema.items()):
        if ctx is not None:
            ctx.check_canceled()
        cols[name] = pl.Series(name, [_convert(r[i], dtype) for r in rows], dtype=dtype)
    return pl.DataFrame(cols, schema=schema)


def sql_type(dtype) -> str:
    kind = dtype_kind(dtype)
    return {
        "integer": "INTEGER", "long": "INTEGER", "double": "REAL", "boolean": "BOOLEAN",
        "date": "DATE", "datetime": "TIMESTAMP", "time": "TIME", "binary": "BLOB",
    }.get(kind, "TEXT")


def sql_value(v):
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return json.dumps(json_value(v))


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    if any(dtype_kind(t) == "molecule" for t in df.schema.values()):
        from verknode.node import chem
        molecules_to_smiles = chem.molecules_to_smiles

        df = molecules_to_smiles(df)
    return df


def table_exists(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
                       (table,)).fetchone() is not None


def require_table(s, key="table") -> str:
    name = (s.get(key) or "").strip()
    if not name:
        raise InvalidSettingsError("No table name specified")
    return name


def same_session(a: DBSessionPortObject, b: DBSessionPortObject) -> bool:
    return os.path.realpath(a.path) == os.path.realpath(b.path)


def make_data(session, query: str, ctx, upstream_schema_known=True) -> DBDataPortObject:
    """DB Data output with the result schema (None if unknown during configure)."""
    try:
        schema = query_schema(session, query)
    except FileNotFoundError:
        if ctx.dry:
            return DBDataPortObject(session, query, None)
        raise
    except sqlite3.Error as e:
        # tables may be created by upstream nodes that have not been executed yet
        if ctx.dry and (not upstream_schema_known or "no such table" in str(e)):
            return DBDataPortObject(session, query, None)
        raise InvalidSettingsError(f"Invalid SQL query: {e}") from None
    return DBDataPortObject(session, query, schema)


def _need(obj, what="input"):
    if obj is None:
        raise InvalidSettingsError(f"No {what}")
    return obj


def _check_columns(data: DBDataPortObject, names):
    if data.schema is None:
        return
    for n in names:
        if n not in data.schema:
            raise InvalidSettingsError(f'Column "{n}" not found in database query')


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------
class SQLiteConnector(NodeModel):
    factory = "verknode.db.SQLiteConnector"
    name = "SQLite Connector"
    category = "DB/Connection"
    node_type = "Source"
    icon = "SQLi"
    short_description = "Creates a connection to an SQLite database file."
    description = (
        "Opens a DB session on an SQLite database file. Connect DB nodes (Table Selector, Writer, "
        "Query Reader, ...) to its DB Connection port. The port view lists the tables."
    )
    out_ports = [Port(DB_SESSION, "DB Connection", "SQLite database session")]
    fields = [
        F("path", "string", "Database file", "", PATH_HELP, path=True),
        F("create", "bool", "Create database if it does not exist", True),
    ]

    def _session(self, s, ctx):
        path = ctx.resolve_path(s["path"])
        if not os.path.isfile(path) and not s["create"]:
            raise InvalidSettingsError(f"Database file does not exist: {path}")
        return DBSessionPortObject(path)

    def configure(self, in_specs, s, ctx):
        return [self._session(s, ctx)]

    def execute(self, inputs, s, ctx):
        session = self._session(s, ctx)
        with _rw_connect(session) as con:
            con.execute("SELECT 1")
        ctx.log(f"Connected to {session.path}")
        return [session]


# ----------------------------------------------------------------------
# Query (in-database processing)
# ----------------------------------------------------------------------
class DBTableSelector(NodeModel):
    factory = "verknode.db.TableSelector"
    name = "DB Table Selector"
    category = "DB/Query"
    node_type = "Source"
    icon = "SEL"
    short_description = "Selects a database table or custom query."
    description = (
        "Creates a DB Data object for a table (or a custom SQL SELECT). The available tables are shown "
        "in the port view of the DB Connection." + DB_HELP
    )
    in_ports = [SESSION_IN]
    out_ports = [DATA_OUT]
    fields = [
        F("mode", "enum", "Source", "Table", options=["Table", "Custom query"]),
        F("table", "string", "Table name", "", show_if={"mode": "Table"}),
        F("query", "code", "SQL query", "SELECT * FROM my_table", language="sql", show_if={"mode": "Custom query"}),
    ]

    def execute(self, inputs, s, ctx):
        session = _need(inputs[0], "DB connection")
        if s["mode"] == "Table":
            query = f"SELECT * FROM {q(require_table(s))}"
        else:
            query = strip_sql(substitute_variables(s["query"] or "", ctx.variables()))
            if not query:
                raise InvalidSettingsError("SQL query is empty")
        return [make_data(session, query, ctx)]


class _DBDataNode(NodeModel):
    """In-database node: DB Data in, DB Data out."""

    category = "DB/Query"
    in_ports = [DATA_IN]
    out_ports = [DATA_OUT]

    def build(self, data: DBDataPortObject, s, ctx) -> str:
        raise NotImplementedError

    def execute(self, inputs, s, ctx):
        data = _need(inputs[0], "DB data")
        query = self.build(data, s, ctx)
        return [make_data(data.session, query, ctx, data.schema is not None)]


class DBQuery(_DBDataNode):
    factory = "verknode.db.Query"
    name = "DB Query"
    icon = "SQL"
    short_description = "Modifies the incoming query with custom SQL."
    description = (
        "Wraps the incoming query in custom SQL. <code>#table#</code> is replaced by the incoming "
        "query as a subquery, e.g. <code>SELECT name, price * 2 AS double FROM #table#</code>. "
        "Flow variables can be used as <code>$${Sname}$$</code>." + DB_HELP
    )
    fields = [F("query", "code", "SQL statement", "SELECT * FROM #table#", language="sql")]

    def build(self, data, s, ctx):
        sql = strip_sql(substitute_variables(s["query"] or "", ctx.variables()))
        if "#table#" not in sql:
            raise InvalidSettingsError("The SQL statement must reference #table#")
        return sql.replace("#table#", subquery(data.query))


ROW_OPS = ["=", "!=", "<", "<=", ">", ">=", "LIKE", "NOT LIKE", "IN", "IS NULL", "IS NOT NULL"]


class DBRowFilter(_DBDataNode):
    factory = "verknode.db.RowFilter"
    name = "DB Row Filter"
    icon = "▤"
    short_description = "Filters rows in the database."
    description = (
        "Adds a WHERE clause. Conditions are combined with AND or OR. LIKE uses SQL wildcards "
        "(% and _); IN takes a comma separated list." + DB_HELP
    )
    fields = [
        F("conditions", "records", "Conditions", [{"column": None, "operator": "=", "value": ""}], fields=[
            F("column", "column", "Column"),
            F("operator", "enum", "Operator", "=", options=ROW_OPS),
            F("value", "string", "Value", ""),
        ]),
        F("combine", "enum", "Combine conditions with", "AND", options=["AND", "OR"]),
    ]

    def _value(self, data, column, text):
        dtype = data.schema.get(column) if data.schema is not None else None
        kind = dtype_kind(dtype) if dtype is not None else None
        text = (text or "").strip()
        if kind in ("integer", "long", "double") or (kind is None and _is_number(text)):
            if not _is_number(text):
                raise InvalidSettingsError(f'Value {text!r} for column "{column}" is not a number')
            return text
        if kind == "boolean":
            return "1" if text.lower() in ("1", "true", "yes", "t") else "0"
        return literal(text)

    def build(self, data, s, ctx):
        conds = []
        for c in s["conditions"]:
            col = c.get("column")
            if not col:
                continue
            _check_columns(data, [col])
            op = c.get("operator", "=")
            if op in ("IS NULL", "IS NOT NULL"):
                conds.append(f"{q(col)} {op}")
            elif op == "IN":
                values = [self._value(data, col, v) for v in (c.get("value") or "").split(",") if v.strip()]
                if not values:
                    raise InvalidSettingsError(f'No values for IN condition on "{col}"')
                conds.append(f"{q(col)} IN ({', '.join(values)})")
            elif op in ("LIKE", "NOT LIKE"):
                conds.append(f"{q(col)} {op} {literal(c.get('value') or '')}")
            else:
                conds.append(f"{q(col)} {op} {self._value(data, col, c.get('value'))}")
        if not conds:
            raise InvalidSettingsError("No filter conditions defined")
        where = f" {s['combine']} ".join(f"({c})" for c in conds)
        return f"SELECT * FROM {subquery(data.query)} WHERE {where}"


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


class DBColumnFilter(_DBDataNode):
    factory = "verknode.db.ColumnFilter"
    name = "DB Column Filter"
    icon = "▥"
    short_description = "Selects columns in the database."
    description = "Restricts the query to the included columns." + DB_HELP
    fields = [F("columns", "columns", "Columns", {"include": [], "exclude": [], "enforce": "exclusion"})]

    def build(self, data, s, ctx):
        if data.schema is None:
            cols = list((s["columns"] or {}).get("include", []))
        else:
            cols = selected_columns(s["columns"], data.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        return f"SELECT {', '.join(q(c) for c in cols)} FROM {subquery(data.query)}"


class DBColumnRename(_DBDataNode):
    factory = "verknode.db.ColumnRename"
    name = "DB Column Rename"
    icon = "A→B"
    short_description = "Renames columns in the database."
    description = "Renames columns of the query result using SQL aliases." + DB_HELP
    fields = [
        F("renames", "records", "Renamings", [], fields=[
            F("column", "column", "Column"),
            F("new_name", "string", "New name", ""),
        ]),
    ]

    def build(self, data, s, ctx):
        mapping = {r["column"]: r["new_name"] for r in s["renames"] if r.get("column") and r.get("new_name")}
        _check_columns(data, mapping)
        if data.schema is None:
            raise InvalidSettingsError("Input columns unknown")
        cols = [f"{q(c)} AS {q(mapping[c])}" if c in mapping else q(c) for c in data.schema]
        return f"SELECT {', '.join(cols)} FROM {subquery(data.query)}"


DB_AGGREGATIONS = {
    "SUM": "SUM({})",
    "AVG": "AVG({})",
    "MIN": "MIN({})",
    "MAX": "MAX({})",
    "COUNT": "COUNT({})",
    "COUNT DISTINCT": "COUNT(DISTINCT {})",
    "GROUP_CONCAT": "GROUP_CONCAT({})",
}


class DBGroupBy(_DBDataNode):
    factory = "verknode.db.GroupBy"
    name = "DB GroupBy"
    icon = "Σ"
    short_description = "Groups and aggregates rows in the database."
    description = (
        "Adds GROUP BY with aggregation functions. Output columns are named "
        "<code>METHOD(column)</code> unless a name is given." + DB_HELP
    )
    fields = [
        F("groups", "columns", "Group columns", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("aggregations", "records", "Aggregations", [{"column": None, "method": "SUM", "name": ""}], fields=[
            F("column", "column", "Column"),
            F("method", "enum", "Aggregation", "SUM", options=list(DB_AGGREGATIONS)),
            F("name", "string", "Output name", ""),
        ]),
        F("count_rows", "bool", "Add COUNT(*) column", False),
    ]

    def build(self, data, s, ctx):
        groups = (selected_columns(s["groups"], data.schema) if data.schema is not None
                  else list((s["groups"] or {}).get("include", [])))
        parts = [q(g) for g in groups]
        for a in s["aggregations"]:
            if not a.get("column"):
                continue
            _check_columns(data, [a["column"]])
            name = a.get("name") or f"{a['method']}({a['column']})"
            parts.append(f"{DB_AGGREGATIONS[a['method']].format(q(a['column']))} AS {q(name)}")
        if s["count_rows"]:
            parts.append(f'COUNT(*) AS {q("COUNT(*)")}')
        if len(parts) == len(groups):
            if not groups:
                raise InvalidSettingsError("Select group columns or aggregations")
        sql = f"SELECT {', '.join(parts)} FROM {subquery(data.query)}"
        if groups:
            sql += " GROUP BY " + ", ".join(q(g) for g in groups)
        return sql


class DBSorter(_DBDataNode):
    factory = "verknode.db.Sorter"
    name = "DB Sorter"
    icon = "↓A"
    short_description = "Sorts rows in the database."
    description = "Adds ORDER BY. Missing values are sorted first (ascending) as in SQLite." + DB_HELP
    fields = [
        F("keys", "records", "Sort by", [{"column": None, "order": "Ascending"}], fields=[
            F("column", "column", "Column"),
            F("order", "enum", "Order", "Ascending", options=["Ascending", "Descending"]),
        ]),
    ]

    def build(self, data, s, ctx):
        keys = [k for k in s["keys"] if k.get("column")]
        if not keys:
            raise InvalidSettingsError("No sort column selected")
        _check_columns(data, [k["column"] for k in keys])
        order = ", ".join(f"{q(k['column'])} {'DESC' if k['order'] == 'Descending' else 'ASC'}" for k in keys)
        return f"SELECT * FROM {subquery(data.query)} ORDER BY {order}"


class DBRowSampling(_DBDataNode):
    factory = "verknode.db.RowSampling"
    name = "DB Row Sampling"
    icon = "⁘"
    short_description = "Samples rows in the database."
    description = "Takes the first or random rows, as an absolute count or a fraction of all rows." + DB_HELP
    fields = [
        F("mode", "enum", "Count mode", "Absolute", options=["Absolute", "Relative"]),
        F("count", "int", "Number of rows", 100, show_if={"mode": "Absolute"}),
        F("fraction", "float", "Fraction (0..1)", 0.1, show_if={"mode": "Relative"}),
        F("random", "bool", "Random sampling", False),
    ]

    def build(self, data, s, ctx):
        sub = subquery(data.query)
        if s["mode"] == "Absolute":
            limit = str(max(0, s["count"]))
        else:
            frac = min(max(0.0, s["fraction"]), 1.0)
            limit = f"(SELECT CAST(COUNT(*) * {frac!r} AS INTEGER) FROM {subquery(data.query)})"
        order = " ORDER BY RANDOM()" if s["random"] else ""
        return f"SELECT * FROM {sub}{order} LIMIT {limit}"


JOIN_MODES = {
    "Inner join": "INNER JOIN",
    "Left outer join": "LEFT OUTER JOIN",
    "Right outer join": "RIGHT OUTER JOIN",
    "Full outer join": "FULL OUTER JOIN",
}


class DBJoiner(NodeModel):
    factory = "verknode.db.Joiner"
    name = "DB Joiner"
    category = "DB/Query"
    icon = "⋈"
    short_description = "Joins two database queries."
    description = (
        "Joins two DB Data inputs of the same database on key column pairs. Right columns whose "
        "names exist in the left table get the suffix." + DB_HELP
    )
    in_ports = [Port(DB_DATA, "Left DB Data"), Port(DB_DATA, "Right DB Data")]
    out_ports = [DATA_OUT]
    fields = [
        F("mode", "enum", "Join mode", "Inner join", options=list(JOIN_MODES)),
        F("keys", "records", "Joining columns", [{"left": None, "right": None}], fields=[
            F("left", "column", "Left column", port=1),
            F("right", "column", "Right column", port=2),
        ]),
        F("remove_right_keys", "bool", "Remove right key columns", True),
        F("suffix", "string", "Duplicate column suffix", " (Right)"),
    ]

    def execute(self, inputs, s, ctx):
        left, right = (_need(i, "DB data") for i in inputs)
        if not same_session(left.session, right.session):
            raise InvalidSettingsError("Both inputs must come from the same database")
        keys = [k for k in s["keys"] if k.get("left") and k.get("right")]
        if not keys:
            raise InvalidSettingsError("No joining columns selected")
        _check_columns(left, [k["left"] for k in keys])
        _check_columns(right, [k["right"] for k in keys])
        if left.schema is None or right.schema is None:
            return [DBDataPortObject(left.session, "", None)]
        cols = [f'"l".{q(c)} AS {q(c)}' for c in left.schema]
        right_keys = {k["right"] for k in keys}
        for c in right.schema:
            if s["remove_right_keys"] and c in right_keys:
                continue
            out = c if c not in left.schema else f"{c}{s['suffix'] or ' (Right)'}"
            cols.append(f'"r".{q(c)} AS {q(out)}')
        on = " AND ".join(f'"l".{q(k["left"])} = "r".{q(k["right"])}' for k in keys)
        query = (f'SELECT {", ".join(cols)} FROM ({left.query}) AS "l" '
                 f'{JOIN_MODES[s["mode"]]} ({right.query}) AS "r" ON {on}')
        return [make_data(left.session, query, ctx)]


class DBConcatenate(NodeModel):
    factory = "verknode.db.Concatenate"
    name = "DB Concatenate"
    category = "DB/Query"
    icon = "⊕"
    short_description = "Concatenates the rows of two database queries."
    description = (
        "Combines both inputs with UNION ALL (keep duplicates) or UNION (remove duplicates). Columns are "
        "matched by name; missing columns are filled with NULL." + DB_HELP
    )
    in_ports = [Port(DB_DATA, "First DB Data"), Port(DB_DATA, "Second DB Data")]
    out_ports = [DATA_OUT]
    fields = [F("mode", "enum", "Mode", "UNION ALL", options=["UNION ALL", "UNION"])]

    def execute(self, inputs, s, ctx):
        a, b = (_need(i, "DB data") for i in inputs)
        if not same_session(a.session, b.session):
            raise InvalidSettingsError("Both inputs must come from the same database")
        if a.schema is None or b.schema is None:
            return [DBDataPortObject(a.session, "", None)]
        cols = list(a.schema) + [c for c in b.schema if c not in a.schema]

        def select(d):
            return ", ".join(f"{q(c)}" if c in d.schema else f"NULL AS {q(c)}" for c in cols)

        query = (f"SELECT {select(a)} FROM {subquery(a.query)} {s['mode']} "
                 f"SELECT {select(b)} FROM {subquery(b.query)}")
        return [make_data(a.session, query, ctx)]


# ----------------------------------------------------------------------
# Read / Write
# ----------------------------------------------------------------------
class DBReader(NodeModel):
    factory = "verknode.db.Reader"
    name = "DB Reader"
    category = "DB/Read-Write"
    node_type = "Source"
    icon = "DB▤"
    short_description = "Executes the database query and reads the result into a table."
    description = "Runs the incoming query and returns the result as a data table. Declared column types are preserved."
    in_ports = [DATA_IN]
    out_ports = [Port(TABLE, "Data from Database")]
    fields = []

    def configure(self, in_specs, s, ctx):
        data = _need(in_specs[0], "DB data")
        return [data.schema]

    def execute(self, inputs, s, ctx):
        data = _need(inputs[0], "DB data")
        df = read_query(data.session, data.query, ctx)
        ctx.log(f"Read {df.height} rows from database")
        return [df]


class DBQueryReader(NodeModel):
    factory = "verknode.db.QueryReader"
    name = "DB Query Reader"
    category = "DB/Read-Write"
    node_type = "Source"
    icon = "SQL▤"
    short_description = "Executes a SQL query and reads the result into a table."
    description = "Runs the SQL SELECT against the database and returns the result. Flow variables: <code>$${Sname}$$</code>."
    in_ports = [SESSION_IN]
    out_ports = [Port(TABLE, "Result table")]
    fields = [F("query", "code", "SQL query", "SELECT * FROM my_table", language="sql")]

    def _query(self, s, ctx):
        query = strip_sql(substitute_variables(s["query"] or "", ctx.variables()))
        if not query:
            raise InvalidSettingsError("SQL query is empty")
        return query

    def configure(self, in_specs, s, ctx):
        data = make_data(_need(in_specs[0], "DB connection"), self._query(s, ctx), ctx)
        return [data.schema]

    def execute(self, inputs, s, ctx):
        try:
            return [read_query(_need(inputs[0], "DB connection"), self._query(s, ctx), ctx)]
        except sqlite3.Error as e:
            raise InvalidSettingsError(f"Query failed: {e}") from None


def _create_table_sql(table: str, schema, if_not_exists=False) -> str:
    cols = ", ".join(f"{q(c)} {sql_type(t)}" for c, t in schema.items())
    return f"CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{q(table)} ({cols})"


class DBWriter(NodeModel):
    factory = "verknode.db.Writer"
    name = "DB Writer"
    category = "DB/Read-Write"
    node_type = "Sink"
    icon = "▤DB"
    short_description = "Writes a table into a database table."
    description = (
        "Creates the database table from the input columns (types: INTEGER, REAL, TEXT, BOOLEAN, DATE, "
        "TIMESTAMP, ...) and inserts all rows. Molecules are written as SMILES, collections as JSON."
    )
    in_ports = [Port(TABLE, "Data table"), SESSION_IN]
    out_ports = [SESSION_OUT]
    fields = [
        F("table", "string", "Table name", ""),
        F("if_exists", "enum", "If table exists", "Fail", options=["Fail", "Replace", "Append"],
          description="Replace drops and re-creates the table; Append inserts into the existing table."),
        F("batch_size", "int", "Batch size", 1000),
    ]

    def configure(self, in_specs, s, ctx):
        require_table(s)
        return [_need(in_specs[1], "DB connection")]

    def execute(self, inputs, s, ctx):
        df, session = inputs[0], _need(inputs[1], "DB connection")
        table = require_table(s)
        df = _prepare(df)
        with _rw_connect(session) as con:
            exists = table_exists(con, table)
            if exists and s["if_exists"] == "Fail":
                raise InvalidSettingsError(f'Table "{table}" already exists')
            if exists and s["if_exists"] == "Replace":
                con.execute(f"DROP TABLE {q(table)}")
                exists = False
            if not exists:
                con.execute(_create_table_sql(table, df.schema))
            sql = f"INSERT INTO {q(table)} ({', '.join(q(c) for c in df.columns)}) VALUES ({', '.join('?' * df.width)})"
            batch = max(1, s["batch_size"])
            for start in range(0, df.height, batch):
                ctx.check_canceled()
                rows = [tuple(sql_value(v) for v in r) for r in df.slice(start, batch).iter_rows()]
                con.executemany(sql, rows)
        ctx.log(f'Wrote {df.height} rows to table "{table}"')
        return [session]


class _RowWiseDML(NodeModel):
    """Insert/Update/Delete: input table + session -> table with status column + session."""

    category = "DB/Read-Write"
    node_type = "Sink"
    in_ports = [Port(TABLE, "Data table"), SESSION_IN]
    status_column = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.out_ports = [Port(TABLE, "Data table with status", f'Input table with "{cls.status_column}" column'),
                         SESSION_OUT]

    def statement(self, df, s):
        """Return (sql, parameter columns)."""
        raise NotImplementedError

    def configure(self, in_specs, s, ctx):
        require_table(s)
        schema = in_specs[0]
        self.statement(pl.DataFrame(schema=schema), s)
        out = dict(schema)
        out[self.status_column] = pl.Int32
        return [pl.Schema(out), _need(in_specs[1], "DB connection")]

    def execute(self, inputs, s, ctx):
        df, session = _prepare(inputs[0]), _need(inputs[1], "DB connection")
        table = require_table(s)
        sql, params = self.statement(df, s)
        counts = []
        with _rw_connect(session) as con:
            if not table_exists(con, table):
                raise InvalidSettingsError(f'Table "{table}" does not exist')
            for row in df.select(params).iter_rows():
                ctx.check_canceled()
                counts.append(con.execute(sql, tuple(sql_value(v) for v in row)).rowcount)
        ctx.log(f"{self.name}: {sum(counts)} row(s) affected")
        return [df.with_columns(pl.Series(self.status_column, counts, dtype=pl.Int32)), session]


def _cols(s, key, schema, label):
    cols = selected_columns(s[key], schema)
    if not cols:
        raise InvalidSettingsError(f"No {label} selected")
    return cols


class DBInsert(_RowWiseDML):
    factory = "verknode.db.Insert"
    name = "DB Insert"
    icon = "+DB"
    status_column = "Insert Status"
    short_description = "Inserts the rows of a table into an existing database table."
    description = "Inserts each row into the database table. The status column holds the number of inserted rows."
    fields = [
        F("table", "string", "Table name", ""),
        F("columns", "columns", "Columns to insert", {"include": [], "exclude": [], "enforce": "exclusion"}),
    ]

    def statement(self, df, s):
        cols = _cols(s, "columns", df.schema, "columns to insert")
        table = q(require_table(s))
        return f"INSERT INTO {table} ({', '.join(q(c) for c in cols)}) VALUES ({', '.join('?' * len(cols))})", cols


class DBUpdate(_RowWiseDML):
    factory = "verknode.db.Update"
    name = "DB Update"
    icon = "↻DB"
    status_column = "Update Status"
    short_description = "Updates database rows matching the identification columns."
    description = (
        "For each input row, sets the selected columns in all database rows whose identification "
        "columns match (WHERE). The status column holds the number of updated rows."
    )
    fields = [
        F("table", "string", "Table name", ""),
        F("set_columns", "columns", "Columns to update (SET)", {"include": [], "exclude": [], "enforce": "inclusion"}),
        F("where_columns", "columns", "Identification columns (WHERE)", {"include": [], "exclude": [], "enforce": "inclusion"}),
    ]

    def statement(self, df, s):
        sets = _cols(s, "set_columns", df.schema, "columns to update")
        where = _cols(s, "where_columns", df.schema, "identification columns")
        sql = (f"UPDATE {q(require_table(s))} SET {', '.join(f'{q(c)} = ?' for c in sets)} "
               f"WHERE {' AND '.join(f'{q(c)} = ?' for c in where)}")
        return sql, sets + where


class DBDelete(_RowWiseDML):
    factory = "verknode.db.Delete"
    name = "DB Delete"
    icon = "−DB"
    status_column = "Delete Status"
    short_description = "Deletes database rows matching the identification columns."
    description = "For each input row, deletes all database rows whose identification columns match. The status column holds the number of deleted rows."
    fields = [
        F("table", "string", "Table name", ""),
        F("where_columns", "columns", "Identification columns (WHERE)", {"include": [], "exclude": [], "enforce": "inclusion"}),
    ]

    def statement(self, df, s):
        where = _cols(s, "where_columns", df.schema, "identification columns")
        return f"DELETE FROM {q(require_table(s))} WHERE {' AND '.join(f'{q(c)} = ?' for c in where)}", where


class DBTableCreator(NodeModel):
    factory = "verknode.db.TableCreator"
    name = "DB Table Creator"
    category = "DB/Read-Write"
    node_type = "Other"
    icon = "+▦"
    short_description = "Creates a database table."
    description = (
        "Creates a table with the defined columns. If no columns are defined and the optional input "
        "table is connected, the columns and types of the input table are used."
    )
    in_ports = [SESSION_IN, Port(TABLE, "Table spec", "Columns to create (optional)", optional=True)]
    out_ports = [SESSION_OUT]
    fields = [
        F("table", "string", "Table name", ""),
        F("columns", "records", "Columns", [{"name": "id", "type": "INTEGER", "not_null": True, "primary_key": True}],
          fields=[
              F("name", "string", "Column name", ""),
              F("type", "enum", "Type", "TEXT", options=SQL_TYPES),
              F("not_null", "bool", "Not null", False),
              F("primary_key", "bool", "Primary key", False),
          ]),
        F("if_not_exists", "bool", "Do not fail if the table exists", False),
    ]

    def _sql(self, s, spec):
        table = require_table(s)
        cols = [c for c in s["columns"] if (c.get("name") or "").strip()]
        if not cols and spec is not None:
            defs = [f"{q(c)} {sql_type(t)}" for c, t in spec.items()]
        else:
            if not cols:
                raise InvalidSettingsError("No columns defined")
            defs = [f"{q(c['name'])} {c['type']}{' NOT NULL' if c.get('not_null') else ''}" for c in cols]
            pk = [q(c["name"]) for c in cols if c.get("primary_key")]
            if pk:
                defs.append(f"PRIMARY KEY ({', '.join(pk)})")
        return f"CREATE TABLE {'IF NOT EXISTS ' if s['if_not_exists'] else ''}{q(table)} ({', '.join(defs)})"

    def configure(self, in_specs, s, ctx):
        self._sql(s, in_specs[1])
        return [_need(in_specs[0], "DB connection")]

    def execute(self, inputs, s, ctx):
        session = _need(inputs[0], "DB connection")
        sql = self._sql(s, inputs[1].schema if inputs[1] is not None else None)
        try:
            with _rw_connect(session) as con:
                con.execute(sql)
        except sqlite3.Error as e:
            raise InvalidSettingsError(f"Create table failed: {e}") from None
        return [session]


# ----------------------------------------------------------------------
# Utility
# ----------------------------------------------------------------------
class DBSQLExecutor(NodeModel):
    factory = "verknode.db.SQLExecutor"
    name = "DB SQL Executor"
    category = "DB/Utility"
    node_type = "Other"
    icon = "SQL!"
    short_description = "Executes arbitrary SQL statements."
    description = (
        "Executes the SQL statements (separated by ;) in one transaction, e.g. CREATE INDEX, UPDATE or "
        "DELETE. Flow variables: <code>$${Sname}$$</code>."
    )
    in_ports = [SESSION_IN]
    out_ports = [SESSION_OUT]
    fields = [F("sql", "code", "SQL statements", "", language="sql")]

    def configure(self, in_specs, s, ctx):
        if not (s["sql"] or "").strip():
            raise InvalidSettingsError("No SQL statements")
        return [_need(in_specs[0], "DB connection")]

    def execute(self, inputs, s, ctx):
        session = _need(inputs[0], "DB connection")
        sql = substitute_variables(s["sql"] or "", ctx.variables())
        try:
            with _rw_connect(session) as con:
                con.executescript(f"BEGIN; {sql.strip().rstrip(';')}; COMMIT;")
        except sqlite3.Error as e:
            raise InvalidSettingsError(f"SQL failed: {e}") from None
        return [session]


class DBTableRemover(NodeModel):
    factory = "verknode.db.TableRemover"
    name = "DB Table Remover"
    category = "DB/Utility"
    node_type = "Other"
    icon = "✕▦"
    short_description = "Removes a database table."
    description = "Drops the table from the database."
    in_ports = [SESSION_IN]
    out_ports = [SESSION_OUT]
    fields = [
        F("table", "string", "Table name", ""),
        F("fail_if_missing", "bool", "Fail if table does not exist", True),
    ]

    def configure(self, in_specs, s, ctx):
        require_table(s)
        return [_need(in_specs[0], "DB connection")]

    def execute(self, inputs, s, ctx):
        session = _need(inputs[0], "DB connection")
        table = require_table(s)
        with _rw_connect(session) as con:
            if not table_exists(con, table):
                if s["fail_if_missing"]:
                    raise InvalidSettingsError(f'Table "{table}" does not exist')
                ctx.warn(f'Table "{table}" does not exist')
                return [session]
            con.execute(f"DROP TABLE {q(table)}")
        ctx.log(f'Removed table "{table}"')
        return [session]


class DBTableList(NodeModel):
    factory = "verknode.db.TableList"
    name = "DB Table List"
    category = "DB/Utility"
    node_type = "Source"
    icon = "☰DB"
    short_description = "Lists the tables and views of a database."
    description = "Outputs one row per table or view with its type and number of columns."
    in_ports = [SESSION_IN]
    out_ports = [Port(TABLE, "Tables")]
    fields = []
    SCHEMA = pl.Schema({"Table": pl.String, "Type": pl.String, "Columns": pl.Int64})

    def configure(self, in_specs, s, ctx):
        _need(in_specs[0], "DB connection")
        return [self.SCHEMA]

    def execute(self, inputs, s, ctx):
        session = _need(inputs[0], "DB connection")
        rows = []
        with _ro_connect(session) as con:
            for name, kind in con.execute(
                    "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                n = len(con.execute(f"PRAGMA table_info({q(name)})").fetchall())
                rows.append((name, kind.upper(), n))
        return [pl.DataFrame(rows, schema=self.SCHEMA, orient="row")]
