"""HTTP for the web nodes: requests with verknode's User-Agent, a polite pace per host (some services
ask for at most one request a second), retries on 429 / 5xx, clear errors - and the helpers the nodes
share: $column$ / $${var}$$ templates, a query per input row, JSON into tables.

(A module of helpers, not of nodes: its name starts with "_".)
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import polars as pl

from verknode.node import InvalidSettingsError


def user_agent() -> str:
    try:
        from jupyterlab_verknode._version import __version__
    except ImportError:  # pragma: no cover
        __version__ = "0"
    return f"verknode/{__version__} (+https://github.com/verknode-project/verknode)"


# seconds between requests to a host (their usage policies)
PACE = {"nominatim.openstreetmap.org": 1.05, "eutils.ncbi.nlm.nih.gov": 0.35, "rest.ensembl.org": 0.07,
        "overpass-api.de": 1.0, "overpass.kumi.systems": 1.0, "api.genome.ucsc.edu": 0.1}
_last: dict[str, float] = {}
_lock = threading.Lock()


def _wait_turn(host: str):
    gap = PACE.get(host, 0.0)
    if not gap:
        return
    with _lock:
        now = time.time()
        wait = _last.get(host, 0) + gap - now
        _last[host] = max(now, _last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


class Response:
    def __init__(self, status, headers, body: bytes, url, seconds):
        self.status, self.headers, self.body, self.url, self.seconds = status, headers, body, url, seconds

    @property
    def text(self) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.headers.get("Content-Type", "") or "")
        if m:
            charset = m.group(1)
        return self.body.decode(charset, "replace")

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            raise InvalidSettingsError(f"{self.url}: not JSON: {self.text[:200]!r}") from None


def request(method: str, url: str, params=None, headers=None, data: bytes | str | None = None,
            timeout: float = 30, retries: int = 2, ctx=None, check: bool = True) -> Response:
    """One HTTP request (urllib: nothing to install). check: a status >= 400 raises with the server's message."""
    if params:
        q = urllib.parse.urlencode([(k, v) for k, v in (params.items() if isinstance(params, dict) else params)
                                    if v is not None], doseq=True)
        url += ("&" if "?" in url else "?") + q
    hdrs = {"User-Agent": user_agent(), "Accept": "application/json, */*;q=0.5"}
    hdrs.update({k: v for k, v in (headers or {}).items() if v is not None})
    body = data.encode() if isinstance(data, str) else data
    host = urllib.parse.urlsplit(url).hostname or ""
    for attempt in range(retries + 1):
        if ctx is not None:
            ctx.check_canceled()
        _wait_turn(host)
        t0 = time.time()
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                final = getattr(r, "url", None) or url  # (the browser's responses (pyodide_http) know no final URL)
                return Response(r.status, dict(r.headers), r.read(), final, time.time() - t0)
        except urllib.error.HTTPError as e:
            content = e.read()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1))
                continue
            resp = Response(e.code, dict(e.headers), content, url, time.time() - t0)
            if not check:
                return resp
            raise InvalidSettingsError(f"{method.upper()} {_short_url(url)}: HTTP {e.code} {e.reason} - "
                                       f"{_message(resp)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise InvalidSettingsError(f"{method.upper()} {_short_url(url)}: {getattr(e, 'reason', e)}") from None
    raise InvalidSettingsError(f"{url}: no answer")  # pragma: no cover


def _short_url(url: str) -> str:
    return url if len(url) < 120 else url[:117] + "…"


def _message(resp: Response) -> str:
    """The error a server gives (JSON error fields, else the start of the text)."""
    try:
        j = json.loads(resp.text)
        for key in ("error_message", "message", "error", "detail", "errors", "reason"):
            if isinstance(j, dict) and j.get(key):
                v = j[key]
                return (v.get("message") or json.dumps(v)) if isinstance(v, dict) else str(v)[:300]
    except ValueError:
        pass
    text = resp.text
    title = re.search(r"<title[^>]*>([\s\S]*?)</title>", text, re.I)
    if title and title.group(1).strip():  # (an HTML error page: its title)
        return re.sub(r"\s+", " ", title.group(1)).strip()[:300]
    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>|<!--[\s\S]*?-->", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:300].strip()


def get_json(url, params=None, headers=None, timeout=30, ctx=None):
    return request("GET", url, params, headers, timeout=timeout, ctx=ctx).json()


def basic_auth(user_pass: str) -> str:
    return "Basic " + base64.b64encode(user_pass.encode()).decode()


# ---------------------------------------------------------------------------------------------------
# templates, rows, tables
# ---------------------------------------------------------------------------------------------------
def fill(template: str, row: dict | None, variables: dict | None, quote: bool = False) -> str:
    """$column$ from the row and $${Sname}$$ / $${Iname}$$ / $${Dname}$$ from the flow variables."""
    def var(m):
        v = (variables or {}).get(m.group(2))
        if v is None:
            raise InvalidSettingsError(f"There is no flow variable {m.group(2)!r}")
        v = getattr(v, "value", v)
        return urllib.parse.quote(str(v), safe="") if quote else str(v)

    out = re.sub(r"\$\$\{([SIDB])([^}]+)\}\$\$", var, template or "")
    if row is not None:
        def col(m):
            if m.group(1) not in row:
                raise InvalidSettingsError(f"There is no column {m.group(1)!r}")
            v = row[m.group(1)]
            return "" if v is None else (urllib.parse.quote(str(v), safe="") if quote else str(v))
        out = re.sub(r"\$([^$\s]+)\$", col, out)
    return out


def queries(inputs, s: dict, column_key: str, value_key: str, what: str = "query") -> list[tuple[dict | None, str]]:
    """(input row, query) pairs: one per row of the input table's column, else the setting's value."""
    df = inputs[0] if inputs and isinstance(inputs[0], pl.DataFrame) else None
    if df is not None and s.get(column_key):
        col = s[column_key]
        if col not in df.columns:
            raise InvalidSettingsError(f"There is no column {col!r}")
        return [(r, str(r[col]).strip()) for r in df.iter_rows(named=True) if r[col] is not None and str(r[col]).strip()]
    value = str(s.get(value_key) or "").strip()
    if not value:
        raise InvalidSettingsError(f"Give a {what} (or connect a table and pick its column)")
    return [(None, v.strip()) for v in value.split("\n") if v.strip()]


def table(rows: list[dict], schema: dict | None = None) -> pl.DataFrame:
    """Rows of dicts -> a table (nested values as JSON text)."""
    clean = [{k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in rows]
    if not clean:
        return pl.DataFrame(schema=schema or {})
    df = pl.DataFrame(clean, infer_schema_length=None, strict=False)
    if schema:
        df = df.with_columns([pl.lit(None, dtype=t).alias(c) for c, t in schema.items() if c not in df.columns])
        df = df.select(list(schema) + [c for c in df.columns if c not in schema])
    return df


def with_query(rows: list[dict], query: str) -> list[dict]:
    """Rows of one query, with the query in front."""
    return [{"query": query, **r} for r in rows]


def records_at(obj, path: str | None):
    """The list at a dotted path of a JSON object ("results", "data.items"; empty: the object itself)."""
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)]
        else:
            return None
    return obj


def json_table(obj, path: str | None = None, max_level: int = 2) -> pl.DataFrame | None:
    """JSON as a table: a list of objects (at path) flattened (a.b columns), one object as one row."""
    recs = records_at(obj, path)
    if isinstance(recs, dict):
        recs = [recs]
    if isinstance(recs, list) and not recs:
        return pl.DataFrame()  # (an empty list: no rows)
    if not isinstance(recs, list):
        return None
    if not all(isinstance(r, dict) for r in recs):
        return pl.DataFrame({"value": [json.dumps(r) if isinstance(r, (dict, list)) else r for r in recs]}, strict=False)

    def flat(d, prefix="", level=0):
        out = {}
        for k, v in d.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict) and level < max_level:
                out.update(flat(v, key + ".", level + 1))
            else:
                out[key] = json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
        return out

    return table([flat(r) for r in recs])
