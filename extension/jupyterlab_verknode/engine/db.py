"""Database connections of the DB nodes: SQLite (a file), PostgreSQL (psycopg) and Oracle (python-oracledb).

The DB nodes (nodes/database.py) write their SQL once; what differs between the databases is here:
connecting, parameter placeholders, table aliases, column types, the catalog (which tables there are),
LIMIT, random order and string aggregation.

A session (DBSessionPortObject) holds where the database is, never a password: a server's password is
read when connecting, from the environment variable the connector names (or ~/.verknode/db.env).

    with session.open() as con:          # commits when the block ends, rolls back on an error, closes
        con.execute(f"INSERT INTO t VALUES ({PH}, {PH})", (1, "a"))

PH is the parameter placeholder of every dialect (? / %s / :1 when executed).
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse

SQLITE, POSTGRES, ORACLE = "sqlite", "postgresql", "oracle"
NAMES = {SQLITE: "SQLite", POSTGRES: "PostgreSQL", ORACLE: "Oracle"}
PH = ""  # a parameter placeholder (a private-use character: it never occurs in real SQL)
CONNECT_TIMEOUT = 10  # seconds

WASM = sys.platform == "emscripten"


class DBConnectError(Exception):
    """The database cannot be reached (or the file opened)."""


def dialect(session) -> str:
    return getattr(session, "dialect", SQLITE) or SQLITE


# ---------------------------------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------------------------------
def secret(name: str) -> str:
    """The value of an environment variable of the Jupyter server, else of a NAME=value line of
    ~/.verknode/db.env. Empty when neither has it."""
    name = (name or "").strip()
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value
    from .home import user_path

    try:
        with open(user_path("db.env"), encoding="utf-8") as fh:
            for line in fh:
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == name:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------------------------------
# connecting
# ---------------------------------------------------------------------------------------------------
def _work_copy(path: str) -> str:
    """In the browser (Pyodide), SQLite's writes to a file of JupyterLite's drive leave it "malformed"
    (a new database at once, a grown one sooner or later), while whole files copy there fine. So the
    database is used in a copy in memory, and written back whole after a commit (Connection)."""
    fd, work = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        shutil.copyfile(path, work)
    else:
        os.remove(work)  # (SQLite starts a new database there)
    return work


def _oracle_lobs(cursor, metadata):
    """CLOB / BLOB columns as str / bytes instead of LOB objects."""
    import oracledb

    if metadata.type_code is oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_NCLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_NVARCHAR, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def connect_raw(session, read_only: bool = False):
    """The driver's own DB-API connection (sqlite3, psycopg or oracledb)."""
    d = dialect(session)
    if d == SQLITE:
        path = session.path
        if read_only:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Database file does not exist: {path}")
            return sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return sqlite3.connect(path)
    password = secret(session.password_env) or None
    hint = (f" (no password: {session.password_env} is neither set on the server nor in ~/.verknode/db.env)"
            if session.password_env and password is None else "")
    if d == POSTGRES:
        import psycopg

        try:
            con = psycopg.connect(session.options or "", host=session.host or None, port=session.port or None,
                                  dbname=session.database or None, user=session.user or None, password=password,
                                  connect_timeout=CONNECT_TIMEOUT)
        except psycopg.Error as e:
            raise DBConnectError(f"Cannot connect to PostgreSQL: {str(e).strip()}{hint}") from None
        if read_only:
            con.read_only = True
        return con
    if d == ORACLE:
        import oracledb

        try:
            if session.options:  # a connect string: host:port/service, a TNS alias, (DESCRIPTION=...)
                con = oracledb.connect(user=session.user or None, password=password, dsn=session.options,
                                       tcp_connect_timeout=CONNECT_TIMEOUT)
            else:
                con = oracledb.connect(user=session.user or None, password=password, host=session.host,
                                       port=session.port or 1521, service_name=session.database or None,
                                       tcp_connect_timeout=CONNECT_TIMEOUT)
        except oracledb.Error as e:
            raise DBConnectError(f"Cannot connect to Oracle: {str(e).strip()}{hint}") from None
        con.outputtypehandler = _oracle_lobs
        return con
    raise DBConnectError(f"Unknown database: {d}")


def errors() -> tuple:
    """The error classes of the drivers in use (for except clauses)."""
    out = [sqlite3.Error, DBConnectError]
    for module in ("psycopg", "oracledb"):
        m = sys.modules.get(module)
        if m is not None and hasattr(m, "Error"):
            out.append(m.Error)
    return tuple(out)


def missing_table(error) -> bool:
    """Whether a database error says a table does not exist (yet)."""
    text = str(error)
    return "no such table" in text or "ORA-00942" in text or bool(re.search(r'relation ".*" does not exist', text))


class Connection:
    """A connection with one interface for the three databases: execute / executemany with PH
    placeholders, script (several statements), and a with-block that commits (rolls back on an error)
    and closes."""

    def __init__(self, raw, dialect_: str, work: str | None = None, path: str | None = None):
        self.raw = raw
        self.dialect = dialect_
        self.work, self.path = work, path  # (browser: the copy in memory, and the file to write back)

    def _sql(self, sql: str, params: bool) -> str:
        if self.dialect == SQLITE:
            return sql.replace(PH, "?")
        if self.dialect == POSTGRES:
            return sql.replace("%", "%%").replace(PH, "%s") if params else sql
        parts = sql.split(PH)
        return "".join(p + (f":{i + 1}" if i < len(parts) - 1 else "") for i, p in enumerate(parts))

    def execute(self, sql: str, params=None):
        cur = self.raw.cursor()
        if params is None:
            cur.execute(self._sql(sql, False))
        else:
            cur.execute(self._sql(sql, True), list(params))
        return cur

    def executemany(self, sql: str, rows):
        rows = [list(r) for r in rows]
        if rows:
            self.raw.cursor().executemany(self._sql(sql, True), rows)

    def script(self, sql: str) -> None:
        """Several statements (separated by ;) in one transaction."""
        sql = sql.strip().rstrip(";")
        if self.dialect == SQLITE:
            self.raw.executescript(f"BEGIN; {sql}; COMMIT;")
        elif self.dialect == POSTGRES:
            self.raw.cursor().execute(sql)  # (psycopg runs several statements when there are no parameters)
        else:
            for statement in split_statements(sql):
                self.raw.cursor().execute(statement)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        try:
            if kind is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()
            if self.work:
                try:
                    if kind is None and self.path:
                        shutil.copyfile(self.work, self.path)
                finally:
                    os.remove(self.work)
        return False


def open_session(session, read_only: bool = False) -> Connection:
    if WASM and dialect(session) == SQLITE:
        path = session.path
        if read_only and not os.path.isfile(path):
            raise FileNotFoundError(f"Database file does not exist: {path}")
        if not read_only:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        work = _work_copy(path)
        return Connection(sqlite3.connect(work), SQLITE, work, None if read_only else path)
    return Connection(connect_raw(session, read_only), dialect(session))


_PLSQL = re.compile(r"^\s*(BEGIN|DECLARE|CREATE\s+(OR\s+REPLACE\s+)?(EDITIONABLE\s+|NONEDITIONABLE\s+)?"
                    r"(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b)", re.I)


def split_statements(sql: str) -> list:
    """Oracle runs one statement at a time: split at ; outside strings, quoted names and comments.
    A PL/SQL block (BEGIN / DECLARE / CREATE PROCEDURE ...) runs whole; separate several with a line
    holding only /."""
    out = []
    for chunk in re.split(r"(?m)^\s*/\s*$", sql):
        if not chunk.strip():
            continue
        if _PLSQL.match(chunk):
            out.append(chunk.strip())
            continue
        buf, i, n = [], 0, len(chunk)
        while i < n:
            c = chunk[i]
            if c in "'\"":
                j = i + 1
                while j < n:
                    if chunk[j] == c:
                        if j + 1 < n and chunk[j + 1] == c:
                            j += 2
                            continue
                        break
                    j += 1
                buf.append(chunk[i:j + 1])
                i = j + 1
            elif chunk.startswith("--", i):
                j = chunk.find("\n", i)
                j = n if j < 0 else j
                buf.append(chunk[i:j])
                i = j
            elif chunk.startswith("/*", i):
                j = chunk.find("*/", i + 2)
                j = n if j < 0 else j + 2
                buf.append(chunk[i:j])
                i = j
            elif c == ";":
                out.append("".join(buf).strip())
                buf = []
                i += 1
            else:
                buf.append(c)
                i += 1
        out.append("".join(buf).strip())
    return [s for s in out if s and not re.fullmatch(r"(\s|--[^\n]*|/\*.*?\*/)*", s, re.S)]


# ---------------------------------------------------------------------------------------------------
# SQL that differs
# ---------------------------------------------------------------------------------------------------
def q(name: str) -> str:
    """A quoted identifier (the same in the three databases)."""
    return '"' + str(name).replace('"', '""') + '"'


def qt(d: str, name: str) -> str:
    """A quoted table name; on a server, schema.table names a table of another schema."""
    name = str(name)
    if d != SQLITE and "." in name and not name.startswith('"'):
        schema, table = name.split(".", 1)
        return f"{q(schema)}.{q(table)}"
    return q(name)


def alias(d: str, name: str) -> str:
    """ AS "name" after a subquery (Oracle has no AS for table aliases)."""
    return f" {q(name)}" if d == ORACLE else f" AS {q(name)}"


def limit(d: str, sql: str, n) -> str:
    """The rows of a SELECT, at most n (a number or SQL)."""
    if d == ORACLE:
        return f"{sql} FETCH FIRST {n} ROWS ONLY"
    return f"{sql} LIMIT {n}"


def random_order(d: str) -> str:
    return "DBMS_RANDOM.VALUE" if d == ORACLE else "RANDOM()"


def group_concat(d: str, column_sql: str) -> str:
    if d == POSTGRES:
        return f"STRING_AGG(CAST({column_sql} AS TEXT), ',')"
    if d == ORACLE:
        return f"LISTAGG({column_sql}, ',') WITHIN GROUP (ORDER BY {column_sql})"
    return f"GROUP_CONCAT({column_sql})"


def bool_literal(d: str, value: bool) -> str:
    if d == POSTGRES:
        return "TRUE" if value else "FALSE"
    return "1" if value else "0"


# the DB nodes' column types (SQL_TYPES) in each database
TYPES = {
    POSTGRES: {"TEXT": "TEXT", "INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "BOOLEAN": "BOOLEAN", "DATE": "DATE",
               "TIMESTAMP": "TIMESTAMP", "TIME": "TIME", "NUMERIC": "NUMERIC", "BLOB": "BYTEA"},
    ORACLE: {"TEXT": "VARCHAR2(4000)", "INTEGER": "NUMBER(19)", "REAL": "BINARY_DOUBLE", "BOOLEAN": "NUMBER(1)",
             "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIME": "VARCHAR2(18)", "NUMERIC": "NUMBER", "BLOB": "BLOB"},
}


def column_type(d: str, generic: str) -> str:
    return TYPES.get(d, {}).get(generic, generic)


def value(d: str, v):
    """A cell as a query parameter."""
    if v is None or isinstance(v, (int, float, str, bytes)):
        if isinstance(v, bool) and d != POSTGRES:
            return int(v)
        return v
    if d == POSTGRES and isinstance(v, (dt.date, dt.time, decimal.Decimal)):
        return v
    if d == ORACLE and isinstance(v, (dt.date, decimal.Decimal)):
        return v
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    from .node import json_value

    return json.dumps(json_value(v))


# ---------------------------------------------------------------------------------------------------
# what a query returns, and the catalog
# ---------------------------------------------------------------------------------------------------
def _pg_type(oid) -> str:
    import psycopg

    info = psycopg.postgres.types.get(oid)
    name = (info.name if info else "").upper()
    return {"BYTEA": "BLOB", "JSON": "TEXT", "JSONB": "TEXT", "UUID": "TEXT"}.get(name, name)


def _oracle_type(desc) -> str | None:
    name = getattr(desc[1], "name", str(desc[1])).upper().replace("DB_TYPE_", "")
    precision, scale = desc[4], desc[5]
    if name == "NUMBER":
        if precision and scale == 0:
            return "INTEGER"
        return None if not precision else "NUMERIC"  # (no precision, e.g. COUNT(*): from the values)
    if name in ("BINARY_FLOAT", "BINARY_DOUBLE"):
        return "REAL"
    if name in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "LONG", "LONG_NVARCHAR", "CLOB", "NCLOB", "ROWID", "UROWID"):
        return "TEXT"
    if name == "DATE" or name.startswith("TIMESTAMP"):
        return "TIMESTAMP"  # (an Oracle DATE has a time too)
    if name in ("RAW", "LONG_RAW", "BLOB"):
        return "BLOB"
    if name == "BOOLEAN":
        return "BOOLEAN"
    return None


def describe(con: Connection, query: str, n: int | None = None):
    """Column names, declared types (SQLite style: INTEGER, TEXT, ...; None: unknown) and rows of a
    query, at most n rows."""
    if con.dialect == SQLITE:
        con.execute('DROP VIEW IF EXISTS temp."__verknode_view"')
        con.execute(f'CREATE TEMP VIEW "__verknode_view" AS {query}')
        try:
            info = con.execute('PRAGMA temp.table_info("__verknode_view")').fetchall()
            sql = 'SELECT * FROM temp."__verknode_view"' + (f" LIMIT {n}" if n is not None else "")
            rows = con.execute(sql).fetchall()
        finally:
            con.execute('DROP VIEW IF EXISTS temp."__verknode_view"')
        return [r[1] for r in info], [r[2] for r in info], rows
    sql = query if n is None else limit(con.dialect, f"SELECT * FROM ({query}){alias(con.dialect, '__verknode')}", n)
    cur = con.execute(sql)
    rows = cur.fetchall()
    desc = cur.description or []
    names = [d[0] for d in desc]
    if con.dialect == POSTGRES:
        decls = [_pg_type(d[1]) for d in desc]
    else:
        decls = [_oracle_type(d) for d in desc]
    return names, decls, rows


def table_exists(con: Connection, table: str) -> bool:
    d = con.dialect
    if d == SQLITE:
        return con.execute(f"SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = {PH}",
                           (table,)).fetchone() is not None
    if d == POSTGRES:
        return bool(con.execute(f"SELECT to_regclass({PH}) IS NOT NULL", (qt(d, table),)).fetchone()[0])
    if "." in table and not table.startswith('"'):
        owner, name = table.split(".", 1)
        return con.execute(f"SELECT 1 FROM all_objects WHERE owner = {PH} AND object_name = {PH} "
                           "AND object_type IN ('TABLE', 'VIEW')", (owner, name)).fetchone() is not None
    return con.execute(f"SELECT 1 FROM user_objects WHERE object_name = {PH} AND object_type IN ('TABLE', 'VIEW')",
                       (table,)).fetchone() is not None


def list_tables(con: Connection) -> list:
    """(name, TABLE | VIEW, number of columns) of the tables and views. On a server: those of the
    user's schema (PostgreSQL: of all schemas, with schema.name for other schemas than the current)."""
    d = con.dialect
    if d == SQLITE:
        out = []
        for name, kind in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            out.append((name, kind.upper(), len(con.execute(f"PRAGMA table_info({q(name)})").fetchall())))
        return out
    if d == POSTGRES:
        sql = ("SELECT CASE WHEN t.table_schema = current_schema() THEN t.table_name "
               "ELSE t.table_schema || '.' || t.table_name END, "
               "CASE WHEN t.table_type = 'BASE TABLE' THEN 'TABLE' ELSE t.table_type END, "
               "(SELECT COUNT(*) FROM information_schema.columns c "
               " WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name) "
               "FROM information_schema.tables t "
               "WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1")
    else:
        sql = ("SELECT o.object_name, o.object_type, "
               "(SELECT COUNT(*) FROM user_tab_columns c WHERE c.table_name = o.object_name) "
               "FROM user_objects o WHERE o.object_type IN ('TABLE', 'VIEW') "
               "AND o.object_name NOT LIKE 'BIN$%' ORDER BY 1")
    return [(r[0], r[1], int(r[2])) for r in con.execute(sql).fetchall()]


def key(session) -> tuple:
    """What tells two sessions on the same database."""
    d = dialect(session)
    if d == SQLITE:
        return (d, os.path.realpath(session.path))
    return (d, session.host, session.port, session.database, session.user, session.options)


def summary(session) -> dict:
    d = dialect(session)
    out = {"Database": NAMES.get(d, d)}
    if d == SQLITE:
        out["Path"] = session.path
    elif session.options and d == ORACLE:
        out["Connect string"] = session.options
        out["User"] = session.user
    else:
        out["Server"] = f"{session.host}:{session.port}" if session.port else session.host
        out["Database name" if d == POSTGRES else "Service"] = session.database
        out["User"] = session.user
    return out
