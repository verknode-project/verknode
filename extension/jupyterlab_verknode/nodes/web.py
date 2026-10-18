"""Web nodes: HTTP (curl, HTTP Request), feeds (RSS / Atom), and web services - Wikipedia, PubMed,
OpenStreetMap (Nominatim, Overpass), Google Maps, weather (Open-Meteo), Apple Podcasts.

Most take their query from a setting or, with a table connected, one per row of a column; the result
rows carry the query in front. They use the standard library (urllib), a User-Agent naming verknode
and the pace the services ask for (nodes/_http.py).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import shlex
import time
import urllib.parse
import xml.etree.ElementTree as ET

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

from ._http import basic_auth, fill, get_json, json_table, queries, request, table, with_query

CAT_HTTP = "Web Services/HTTP"
CAT_KNOWLEDGE = "Web Services/Knowledge"
CAT_MAPS = "Web Services/Maps & Weather"
CAT_MEDIA = "Web Services/Media"

QUERY_IN = [Port(TABLE, "Queries", "A table with a column to query, one request per row (optional)", optional=True)]


def column_field(label="Query column", help_="Of the input table: one request per row (empty: the setting below)."):
    return F("column", "column", label, None, help_, port=1)


class _WebNode(NodeModel):
    """A node that asks a web service: no request while the editor configures it. (Its name starts with
    "_": not a node itself.)"""

    node_type = "Source"
    in_ports = QUERY_IN
    out_ports = [Port(TABLE, "Results")]
    SCHEMA: dict | None = None

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(self.SCHEMA) if self.SCHEMA else None]


def _strip_html(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


# ---------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------
def parse_curl(command: str) -> dict:
    """A curl command (as browsers copy it) -> {method, url, headers, data}."""
    args = shlex.split(command.replace("\\\n", " "))
    if args and args[0] == "curl":
        args = args[1:]
    method, url, headers, data, get = None, None, {}, [], False
    i = 0
    takes = {"-X", "--request", "-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
             "--data-urlencode", "--json", "-u", "--user", "-A", "--user-agent", "-e", "--referer", "-b", "--cookie",
             "--url", "-o", "--output", "-m", "--max-time", "--connect-timeout", "-x", "--proxy"}
    while i < len(args):
        a = args[i]
        v = args[i + 1] if a in takes and i + 1 < len(args) else None
        if a in ("-X", "--request"):
            method = v.upper()
        elif a in ("-H", "--header"):
            k, _, val = v.partition(":")
            headers[k.strip()] = val.strip()
        elif a in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            data.append(v)
        elif a == "--data-urlencode":
            k, eq, val = v.partition("=")
            data.append(f"{k}={urllib.parse.quote_plus(val)}" if eq else urllib.parse.quote_plus(v))
        elif a == "--json":
            data.append(v)
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Accept", "application/json")
        elif a in ("-u", "--user"):
            headers["Authorization"] = basic_auth(v)
        elif a in ("-A", "--user-agent"):
            headers["User-Agent"] = v
        elif a in ("-e", "--referer"):
            headers["Referer"] = v
        elif a in ("-b", "--cookie"):
            headers["Cookie"] = v
        elif a in ("-G", "--get"):
            get = True
        elif a == "--url":
            url = v
        elif a.startswith("-"):
            pass  # (-s, -L, -k, --compressed, -i ...: no effect here)
        else:
            url = a
        i += 2 if v is not None else 1
    if not url:
        raise InvalidSettingsError("The curl command has no URL")
    body = "&".join(data) if data else None
    if get and body:
        url += ("&" if "?" in url else "?") + body
        body = None
    if body is not None and "Content-Type" not in {k.title(): v for k, v in headers.items()} and not \
            any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return {"method": method or ("POST" if body is not None else "GET"), "url": url, "headers": headers, "data": body}


RESPONSE_OPTIONS = ["Table from JSON (else text)", "Text", "JSON text"]
RESPONSE_SCHEMA = {"url": pl.String, "status": pl.Int64, "content_type": pl.String, "body": pl.String,
                   "seconds": pl.Float64}


def _responses(calls, s, ctx, call=None) -> pl.DataFrame:
    """Run (row, method, url, headers, body) calls; one table: the parsed JSON rows, or a row per call."""
    as_ = s.get("response") or RESPONSE_OPTIONS[0]
    rows, frames = [], []
    call = call or request
    for row, method, url, headers, body in calls:
        r = call(method, url, headers=headers, data=body, timeout=float(s.get("timeout") or 30), ctx=ctx,
                    check=not s.get("keep_errors", False))
        ctype = (r.headers.get("Content-Type") or "").split(";")[0]
        if as_ == RESPONSE_OPTIONS[0] and "json" in ctype and r.status < 400:
            t = json_table(r.json(), s.get("records_path"))
            if t is not None:
                # (the request's URL and status in front - named so they do not clash with the answer's)
                front = {"request_url" if "url" in t.columns else "url": url,
                         "http_status" if "status" in t.columns else "status": r.status}
                if row:
                    front.update({k: v for k, v in row.items() if k not in t.columns})
                frames.append(t.with_columns([pl.lit(v).alias(k) for k, v in front.items()]).select(
                    list(front) + t.columns))
                continue
        text = r.text
        if as_ == "JSON text" and "json" in ctype:
            text = json.dumps(r.json(), ensure_ascii=False)
        rows.append({**(row or {}), "url": url, "status": r.status, "content_type": ctype, "body": text,
                     "seconds": round(r.seconds, 3)})
    out = frames + ([table(rows)] if rows else [])
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame(schema=RESPONSE_SCHEMA)


class Curl(_WebNode):
    factory = "verknode.web.Curl"
    name = "curl"
    category = CAT_HTTP
    icon = "curl"
    sandbox_unsafe = True  # (it can change things on servers: not in the agents' test runs)
    short_description = "Runs a curl command (as browsers copy it) - once, or once per input row."
    description = (
        "Paste a curl command - e.g. from the browser's developer tools (Copy as cURL) or an API's "
        "documentation: -X, -H, -d / --data / --data-raw / --data-urlencode / --json, -u, -G, -A, -e, -b and "
        "--url are understood (no curl program is needed). With a table connected, it runs once per row, with "
        "$column$ in the command replaced by the row's value; $${Sname}$$ inserts a flow variable. A JSON "
        "answer becomes a table (Records path: where its list is, e.g. results or data.items); else one row "
        "with the status and the body."
    )
    in_ports = [Port(TABLE, "Rows", "One request per row ($column$ in the command), optional", optional=True)]
    fields = [
        F("command", "code", "curl command", "curl -s https://api.github.com/repos/python/cpython",
          "Values: $column$ (per input row), $${Sname}$$ (flow variables)."),
        F("response", "enum", "Response", RESPONSE_OPTIONS[0], options=RESPONSE_OPTIONS),
        F("records_path", "string", "Records path", "", "Where the list of records is in the JSON (dotted), e.g. items."),
        F("keep_errors", "bool", "Keep error answers as rows", False, "Else a status of 400 or more fails the node."),
        F("timeout", "int", "Timeout (s)", 30),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=RESPONSE_SCHEMA)]
        df = inputs[0] if inputs and inputs[0] is not None else None
        variables = ctx.variables()
        rows = list(df.iter_rows(named=True)) if df is not None else [None]
        calls = []
        for row in rows:
            c = parse_curl(fill(s["command"], row, variables))
            calls.append((row, c["method"], c["url"], c["headers"], c["data"]))
        return [_responses(calls, s, ctx)]


class HTTPRequest(_WebNode):
    factory = "verknode.web.HTTPRequest"
    name = "HTTP Request"
    category = CAT_HTTP
    icon = "⇅"
    sandbox_unsafe = True
    short_description = "An HTTP request in the style of Python's requests - once, or once per input row."
    description = (
        "Method, URL, query parameters, headers and a body (JSON, form or text), like requests.request(...). "
        "With a table connected, one request per row: $column$ in the URL, the parameters, headers and body is "
        "the row's value (URL-encoded in the URL). An API key: give the name of an environment variable of "
        "the Jupyter server (Token variable), sent as Authorization: Bearer - keys are never stored in the "
        "workflow. A JSON answer becomes a table (Records path), else one row with the status and the body. "
        "Uses the requests package when it is installed (else the standard library)."
    )
    in_ports = [Port(TABLE, "Rows", "One request per row ($column$ in URL, parameters, headers, body), optional",
                     optional=True)]
    fields = [
        F("method", "enum", "Method", "GET", options=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]),
        F("url", "string", "URL", "https://api.github.com/repos/python/cpython/releases",
          "$column$ and $${Sname}$$ are filled in."),
        F("params", "records", "Query parameters", [], fields=[F("name", "string", "Name", ""), F("value", "string", "Value", "")]),
        F("headers", "records", "Headers", [], fields=[F("name", "string", "Name", ""), F("value", "string", "Value", "")]),
        F("body_type", "enum", "Body", "None", options=["None", "JSON", "Form", "Text"]),
        F("body", "code", "Body content", "", "JSON text, name=value lines (form) or text."),
        F("token_env", "string", "Token variable", "", "Environment variable with a bearer token (e.g. GITHUB_TOKEN)."),
        F("response", "enum", "Response", RESPONSE_OPTIONS[0], options=RESPONSE_OPTIONS),
        F("records_path", "string", "Records path", ""),
        F("keep_errors", "bool", "Keep error answers as rows", False),
        F("timeout", "int", "Timeout (s)", 30),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=RESPONSE_SCHEMA)]
        df = inputs[0] if inputs and inputs[0] is not None else None
        variables = ctx.variables()
        calls = []
        for row in (list(df.iter_rows(named=True)) if df is not None else [None]):
            url = fill(s["url"], row, variables, quote=True)
            params = [(fill(p["name"], row, variables), fill(p["value"], row, variables)) for p in s.get("params") or [] if p.get("name")]
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            headers = {fill(h["name"], row, variables): fill(h["value"], row, variables) for h in s.get("headers") or [] if h.get("name")}
            if s.get("token_env"):
                token = os.environ.get(s["token_env"].strip())
                if not token:
                    raise InvalidSettingsError(f"The environment variable {s['token_env']} is not set (on the Jupyter server)")
                headers["Authorization"] = f"Bearer {token}"
            body, kind = None, s.get("body_type") or "None"
            if kind != "None":
                text = fill(s.get("body") or "", row, variables)
                if kind == "JSON":
                    json.loads(text or "null")  # (a clear error for bad JSON)
                    headers.setdefault("Content-Type", "application/json")
                    body = text
                elif kind == "Form":
                    body = urllib.parse.urlencode([tuple(line.split("=", 1)) for line in text.splitlines() if "=" in line])
                    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                else:
                    body = text
            calls.append((row, s.get("method") or "GET", url, headers, body))
        try:
            import requests  # noqa: F401 - optional: then the requests are made by requests
        except ImportError:
            return [_responses(calls, s, ctx)]
        return [_responses(calls, s, ctx, call=_requests_call)]


def _requests_call(method, url, headers=None, data=None, timeout=30, ctx=None, check=True):
    """request() made with the requests package (HTTPRequest calls it only when requests is there)."""
    try:
        import requests
    except ImportError:  # pragma: no cover - (optional: see HTTPRequest.execute)
        raise InvalidSettingsError("The requests package is not installed") from None

    from ._http import Response, user_agent

    if ctx is not None:
        ctx.check_canceled()
    t0 = time.time()
    r = requests.request(method, url, headers={"User-Agent": user_agent(), **(headers or {})}, data=data, timeout=timeout)
    if check and r.status_code >= 400:
        raise InvalidSettingsError(f"{method} {url}: HTTP {r.status_code} {r.reason} - {r.text[:300]}")
    return Response(r.status_code, dict(r.headers), r.content, r.url, time.time() - t0)


# ---------------------------------------------------------------------------------------------------
# feeds
# ---------------------------------------------------------------------------------------------------
NS = {"atom": "http://www.w3.org/2005/Atom", "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
      "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/",
      "media": "http://search.yahoo.com/mrss/"}
FEED_SCHEMA = {"feed": pl.String, "feed_title": pl.String, "title": pl.String, "link": pl.String,
               "published": pl.Datetime("us", "UTC"), "author": pl.String, "summary": pl.String, "id": pl.String,
               "enclosure_url": pl.String, "enclosure_type": pl.String, "duration": pl.String}


def _date(text):
    if not text:
        return None
    from email.utils import parsedate_to_datetime

    for parse in (parsedate_to_datetime, lambda t: dt.datetime.fromisoformat(t.replace("Z", "+00:00"))):
        try:
            d = parse(text.strip())
            return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
    return None


def parse_feed(xml_bytes: bytes, url: str, limit: int) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise InvalidSettingsError(f"{url}: not an RSS / Atom feed ({e})") from None
    t = lambda el, path: (el.findtext(path, namespaces=NS) or "").strip() or None  # noqa: E731
    out = []
    if root.tag.endswith("feed"):  # Atom
        feed_title = t(root, "atom:title")
        for e in root.findall("atom:entry", NS)[:limit]:
            link = next((l.get("href") for l in e.findall("atom:link", NS) if l.get("rel", "alternate") == "alternate"), None)
            enc = next((l for l in e.findall("atom:link", NS) if l.get("rel") == "enclosure"), None)
            out.append({"title": t(e, "atom:title"), "link": link, "published": _date(t(e, "atom:published") or t(e, "atom:updated")),
                        "author": t(e, "atom:author/atom:name"), "summary": _strip_html(t(e, "atom:summary") or t(e, "atom:content")),
                        "id": t(e, "atom:id"), "enclosure_url": enc.get("href") if enc is not None else None,
                        "enclosure_type": enc.get("type") if enc is not None else None, "duration": None})
    else:  # RSS 2.0 / 1.0
        channel = root.find("channel")
        feed_title = t(channel, "title") if channel is not None else None
        items = (channel.findall("item") if channel is not None else []) or root.findall("{http://purl.org/rss/1.0/}item")
        for e in items[:limit]:
            enc = e.find("enclosure")
            out.append({"title": t(e, "title") or t(e, "{http://purl.org/rss/1.0/}title"),
                        "link": t(e, "link") or t(e, "{http://purl.org/rss/1.0/}link"),
                        "published": _date(t(e, "pubDate") or t(e, "dc:date")),
                        "author": t(e, "author") or t(e, "dc:creator") or t(e, "itunes:author"),
                        "summary": _strip_html(t(e, "description") or t(e, "itunes:summary")),
                        "id": t(e, "guid"), "enclosure_url": enc.get("url") if enc is not None else None,
                        "enclosure_type": enc.get("type") if enc is not None else None, "duration": t(e, "itunes:duration")})
    return [{"feed": url, "feed_title": feed_title, **r} for r in out]


class FeedReader(_WebNode):
    factory = "verknode.web.FeedReader"
    name = "RSS / Atom Reader"
    category = CAT_HTTP
    icon = "RSS"
    short_description = "The items of RSS and Atom feeds (news, blogs, podcasts) as a table."
    description = (
        "Reads feeds - one URL per line, or a column of the input table (e.g. the feed_url of Apple Podcasts "
        "Search) - and outputs their items: title, link, published (UTC), author, summary (text), id, and the "
        "enclosure (a podcast's audio) with its duration."
    )
    SCHEMA = FEED_SCHEMA
    fields = [
        column_field("Feed URL column"),
        F("urls", "code", "Feed URLs", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
          "One per line (without a column)."),
        F("limit", "int", "Items per feed at most", 50),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=FEED_SCHEMA)]
        rows = []
        for _, url in queries(inputs, s, "column", "urls", "feed URL"):
            r = request("GET", url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, */*"},
                        ctx=ctx)
            rows += parse_feed(r.body, url, max(1, int(s.get("limit") or 50)))
        return [pl.DataFrame(rows, schema=FEED_SCHEMA) if rows else pl.DataFrame(schema=FEED_SCHEMA)]


# ---------------------------------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------------------------------
WIKI_SCHEMA = {"query": pl.String, "title": pl.String, "description": pl.String, "extract": pl.String,
               "url": pl.String, "thumbnail": pl.String, "lat": pl.Float64, "lon": pl.Float64}


class Wikipedia(_WebNode):
    factory = "verknode.web.Wikipedia"
    name = "Wikipedia"
    category = CAT_KNOWLEDGE
    icon = "W"
    short_description = "Wikipedia summaries, searches and page texts (any language)."
    description = (
        "Summary: the short description, the first paragraph, the link, the picture and - for places - the "
        "coordinates (lat, lon: for a Map Display) of each title. Search: the pages that match each query "
        "(title, snippet). Page text: the whole article as plain text. Titles or queries come from a setting "
        "(one per line) or a column of the input table."
    )
    SCHEMA = None
    fields = [
        F("mode", "enum", "What", "Summary", options=["Summary", "Search", "Page text"]),
        column_field("Title / query column"),
        F("titles", "code", "Titles / queries", "Kyoto\nLisbon\nVancouver", "One per line (without a column)."),
        F("language", "string", "Language", "en", "Wikipedia's language code (en, de, ja, ...)."),
        F("limit", "int", "Search results per query", 5),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(WIKI_SCHEMA) if s.get("mode") == "Summary" else None]

    def execute(self, inputs, s, ctx):
        mode, lang = s.get("mode") or "Summary", (s.get("language") or "en").strip()
        if ctx.dry:
            return [pl.DataFrame(schema=WIKI_SCHEMA)]
        base = f"https://{lang}.wikipedia.org"
        rows = []
        for _, q in queries(inputs, s, "column", "titles", "title"):
            if mode == "Summary":
                r = request("GET", f"{base}/api/rest_v1/page/summary/{urllib.parse.quote(q.replace(' ', '_'), safe='')}",
                            ctx=ctx, check=False)
                if r.status == 404:
                    rows.append({"query": q, "title": None, "description": "(no such page)"})
                    continue
                j = r.json()
                c = j.get("coordinates") or {}
                rows.append({"query": q, "title": j.get("title"), "description": j.get("description"),
                             "extract": j.get("extract"), "url": (j.get("content_urls") or {}).get("desktop", {}).get("page"),
                             "thumbnail": (j.get("thumbnail") or {}).get("source"), "lat": c.get("lat"), "lon": c.get("lon")})
            elif mode == "Search":
                j = get_json(f"{base}/w/api.php", {"action": "query", "list": "search", "srsearch": q, "format": "json",
                                                   "srlimit": int(s.get("limit") or 5)}, ctx=ctx)
                rows += with_query([{"title": h["title"], "snippet": _strip_html(h.get("snippet")), "words": h.get("wordcount"),
                                     "url": f"{base}/wiki/{urllib.parse.quote(h['title'].replace(' ', '_'))}"}
                                    for h in j.get("query", {}).get("search", [])], q)
            else:
                j = get_json(f"{base}/w/api.php", {"action": "query", "prop": "extracts", "explaintext": 1, "titles": q,
                                                   "format": "json", "redirects": 1}, ctx=ctx)
                for page in (j.get("query", {}).get("pages") or {}).values():
                    rows.append({"query": q, "title": page.get("title"), "text": page.get("extract")})
        if mode == "Summary":
            return [pl.DataFrame(rows, schema=WIKI_SCHEMA) if rows else pl.DataFrame(schema=WIKI_SCHEMA)]
        return [table(rows)]


PUBMED_SCHEMA = {"query": pl.String, "pmid": pl.String, "title": pl.String, "journal": pl.String, "pubdate": pl.String,
                 "authors": pl.String, "doi": pl.String, "abstract": pl.String, "url": pl.String}


class PubMed(_WebNode):
    factory = "verknode.web.PubMed"
    name = "PubMed Search"
    category = CAT_KNOWLEDGE
    icon = "PM"
    short_description = "Articles from PubMed for search terms: titles, journals, authors, DOIs, abstracts."
    description = (
        "Searches PubMed (NCBI E-utilities) with PubMed's query syntax - e.g. crispr AND 2024[dp], "
        "smith j[au] - and outputs the articles: PMID, title, journal, date, authors, DOI and (with Abstracts) "
        "the abstract. An NCBI API key (environment variable NCBI_API_KEY) allows more requests a second."
    )
    SCHEMA = PUBMED_SCHEMA
    fields = [
        column_field("Query column"),
        F("query", "string", "Search terms", "CRISPR base editing[ti]"),
        F("limit", "int", "Articles per query", 10),
        F("sort", "enum", "Sort", "relevance", options=["relevance", "pub_date", "first_author"]),
        F("abstracts", "bool", "Abstracts", True, "Fetch the abstracts too (one more request)."),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=PUBMED_SCHEMA)]
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        key = {"api_key": os.environ["NCBI_API_KEY"]} if os.environ.get("NCBI_API_KEY") else {}
        rows = []
        for _, q in queries(inputs, s, "column", "query", "search term"):
            ids = get_json(f"{base}/esearch.fcgi", {"db": "pubmed", "term": q, "retmode": "json", "sort": s.get("sort"),
                                                   "retmax": int(s.get("limit") or 10), **key}, ctx=ctx)["esearchresult"]["idlist"]
            if not ids:
                continue
            summ = get_json(f"{base}/esummary.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "json", **key}, ctx=ctx)["result"]
            abstracts = {}
            if s.get("abstracts", True):
                x = request("GET", f"{base}/efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", **key}, ctx=ctx)
                for art in ET.fromstring(x.body).findall(".//PubmedArticle"):
                    pmid = art.findtext(".//PMID")
                    parts = [("".join(a.itertext())).strip() for a in art.findall(".//Abstract/AbstractText")]
                    abstracts[pmid] = "\n".join(p for p in parts if p) or None
            for i in ids:
                a = summ.get(i) or {}
                doi = next((x["value"] for x in a.get("articleids", []) if x.get("idtype") == "doi"), None)
                rows.append({"query": q, "pmid": i, "title": a.get("title"), "journal": a.get("fulljournalname") or a.get("source"),
                             "pubdate": a.get("pubdate"), "authors": ", ".join(x["name"] for x in a.get("authors", [])),
                             "doi": doi, "abstract": abstracts.get(i), "url": f"https://pubmed.ncbi.nlm.nih.gov/{i}/"})
        return [pl.DataFrame(rows, schema=PUBMED_SCHEMA) if rows else pl.DataFrame(schema=PUBMED_SCHEMA)]


# ---------------------------------------------------------------------------------------------------
# maps and weather
# ---------------------------------------------------------------------------------------------------
GEO_SCHEMA = {"query": pl.String, "name": pl.String, "lat": pl.Float64, "lon": pl.Float64, "type": pl.String,
              "class": pl.String, "country": pl.String, "osm_type": pl.String, "osm_id": pl.Int64,
              "boundingbox": pl.String, "address": pl.String}


class OSMGeocode(_WebNode):
    factory = "verknode.web.OSMGeocode"
    name = "OpenStreetMap Geocode"
    category = CAT_MAPS
    icon = "OSM"
    short_description = "Places for addresses and names (OpenStreetMap Nominatim) - or the address of coordinates."
    description = (
        "Search: the places that match each address or name, with their coordinates (lat, lon - for a Map "
        "Display or Geo nodes), type, country and address. Reverse: the address at each lat / lon of the input "
        "table. Nominatim's usage policy allows one request a second: the node keeps to it. Data © "
        "OpenStreetMap contributors (ODbL)."
    )
    SCHEMA = GEO_SCHEMA
    fields = [
        F("mode", "enum", "What", "Search", options=["Search", "Reverse (lat, lon)"]),
        column_field("Address / name column"),
        F("query", "code", "Addresses / names", "Fushimi Inari Taisha, Kyoto\nBelém Tower, Lisbon", "One per line."),
        F("lat", "column", "Latitude column (reverse)", None, port=1),
        F("lon", "column", "Longitude column (reverse)", None, port=1),
        F("limit", "int", "Places per search", 1),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=GEO_SCHEMA)]
        base = "https://nominatim.openstreetmap.org"
        rows = []

        def row(q, p):
            return {"query": q, "name": p.get("display_name"), "lat": float(p["lat"]), "lon": float(p["lon"]),
                    "type": p.get("type") or p.get("addresstype"), "class": p.get("class") or p.get("category"),
                    "country": (p.get("address") or {}).get("country"), "osm_type": p.get("osm_type"),
                    "osm_id": p.get("osm_id"), "boundingbox": json.dumps(p.get("boundingbox")),
                    "address": json.dumps(p.get("address"), ensure_ascii=False)}

        if (s.get("mode") or "Search").startswith("Reverse"):
            df = inputs[0] if inputs and inputs[0] is not None else None
            if df is None or not s.get("lat") or not s.get("lon"):
                raise InvalidSettingsError("Reverse geocoding needs an input table with latitude and longitude columns")
            for r in df.iter_rows(named=True):
                if r[s["lat"]] is None or r[s["lon"]] is None:
                    continue
                p = get_json(f"{base}/reverse", {"lat": r[s["lat"]], "lon": r[s["lon"]], "format": "jsonv2",
                                                 "addressdetails": 1}, ctx=ctx)
                if "lat" in p:
                    rows.append(row(f"{r[s['lat']]},{r[s['lon']]}", p))
        else:
            for _, q in queries(inputs, s, "column", "query", "address"):
                for p in get_json(f"{base}/search", {"q": q, "format": "jsonv2", "addressdetails": 1,
                                                     "limit": int(s.get("limit") or 1)}, ctx=ctx):
                    rows.append(row(q, p))
        return [pl.DataFrame(rows, schema=GEO_SCHEMA) if rows else pl.DataFrame(schema=GEO_SCHEMA)]


OVERPASS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
OSM_SCHEMA = {"query": pl.String, "osm_type": pl.String, "osm_id": pl.Int64, "name": pl.String, "lat": pl.Float64,
              "lon": pl.Float64, "tags": pl.String}


class OSMOverpass(_WebNode):
    factory = "verknode.web.OSMOverpass"
    name = "OpenStreetMap Places Nearby"
    category = CAT_MAPS
    icon = "OSM"
    short_description = "OpenStreetMap features with a tag (cafes, parks, stations...) around points (Overpass API)."
    description = (
        "Finds the OpenStreetMap features with a tag - amenity=cafe, tourism=museum, railway=station, "
        "leisure=park ... - within a radius of each point (the lat / lon columns of the input table, or the "
        "coordinates below), with their name, coordinates and tags. Or give your own Overpass QL query "
        "(output with out center;). Data © OpenStreetMap contributors (ODbL)."
    )
    SCHEMA = OSM_SCHEMA
    fields = [
        F("tag", "string", "Tag (key=value)", "amenity=cafe", "Key alone (e.g. shop) for any value."),
        F("radius", "int", "Radius (m)", 500),
        F("lat", "column", "Latitude column", None, port=1),
        F("lon", "column", "Longitude column", None, port=1),
        F("point", "string", "Point (lat, lon)", "35.0116, 135.7681", "Without an input table."),
        F("query", "code", "Overpass QL (instead)", "", "Your own query; empty: the tag around the points."),
        F("limit", "int", "Features per point at most", 50),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=OSM_SCHEMA)]
        df = inputs[0] if inputs and inputs[0] is not None else None
        points = []
        if s.get("query", "").strip():
            jobs = [("query", s["query"].strip())]
        else:
            if df is not None and s.get("lat") and s.get("lon"):
                points = [(r[s["lat"]], r[s["lon"]]) for r in df.iter_rows(named=True) if r[s["lat"]] is not None]
            else:
                try:
                    lat, lon = [float(x) for x in (s.get("point") or "").split(",")]
                except ValueError:
                    raise InvalidSettingsError("Point: give lat, lon (e.g. 35.01, 135.77)") from None
                points = [(lat, lon)]
            key, _, value = (s.get("tag") or "amenity").partition("=")
            sel = f'["{key.strip()}"="{value.strip()}"]' if value else f'["{key.strip()}"]'
            r_ = int(s.get("radius") or 500)
            # (one query for all points - the public servers are busy: each place goes to its nearest point)
            around = "".join(f"nwr{sel}(around:{r_},{lat},{lon});" for lat, lon in points)
            jobs = [(None, f"[out:json][timeout:60];({around});out center;")]
        rows = []
        limit = int(s.get("limit") or 50)
        for q, ql in jobs:
            last = None
            for endpoint in OVERPASS:
                try:
                    j = request("POST", endpoint, data="data=" + urllib.parse.quote(ql),
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=90, retries=1, ctx=ctx).json()
                    break
                except InvalidSettingsError as e:
                    last = e
            else:
                raise last
            per: dict = {}
            for el in j.get("elements", []):
                c = el.get("center") or el
                where = q
                if q is None and c.get("lat") is not None:
                    lat, lon = min(points, key=lambda p: (p[0] - c["lat"]) ** 2 + ((p[1] - c["lon"]) * math.cos(math.radians(p[0]))) ** 2)
                    where = f"{lat},{lon}"
                    per[where] = per.get(where, 0) + 1
                    if per[where] > limit:
                        continue
                rows.append({"query": where, "osm_type": el.get("type"), "osm_id": el.get("id"),
                             "name": (el.get("tags") or {}).get("name"), "lat": c.get("lat"), "lon": c.get("lon"),
                             "tags": json.dumps(el.get("tags") or {}, ensure_ascii=False)})
        return [pl.DataFrame(rows, schema=OSM_SCHEMA) if rows else pl.DataFrame(schema=OSM_SCHEMA)]


class GoogleMaps(_WebNode):
    factory = "verknode.web.GoogleMaps"
    name = "Google Maps"
    category = CAT_MAPS
    icon = "G"
    short_description = "Google Maps Platform: geocoding, place search, travel distances and times (API key)."
    description = (
        "Geocode: coordinates and the formatted address of addresses. Places: places for a text search "
        "(\"ramen near Kyoto Station\") with rating and address. Distance: the travel distance and time from "
        "origins to destinations (driving, walking, bicycling, transit). Needs a Google Maps Platform API key in "
        "an environment variable of the Jupyter server (API key variable, default GOOGLE_MAPS_API_KEY), with "
        "the APIs enabled - requests are billed by Google. Without a key, OpenStreetMap Geocode does geocoding."
    )
    fields = [
        F("mode", "enum", "What", "Geocode", options=["Geocode", "Places (text search)", "Distance"]),
        column_field("Query / origin column"),
        F("query", "code", "Addresses / searches / origins", "Kyoto Station", "One per line."),
        F("destination", "string", "Destination (Distance)", "Kinkaku-ji, Kyoto"),
        F("travel_mode", "enum", "Travel mode", "driving", options=["driving", "walking", "bicycling", "transit"]),
        F("key_env", "string", "API key variable", "GOOGLE_MAPS_API_KEY"),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame()]
        key = os.environ.get((s.get("key_env") or "GOOGLE_MAPS_API_KEY").strip())
        if not key:
            raise InvalidSettingsError(f"Google Maps needs an API key: set the environment variable "
                                       f"{s.get('key_env') or 'GOOGLE_MAPS_API_KEY'} of the Jupyter server "
                                       "(or use OpenStreetMap Geocode)")
        base, rows, mode = "https://maps.googleapis.com/maps/api", [], s.get("mode") or "Geocode"
        for _, q in queries(inputs, s, "column", "query"):
            if mode == "Geocode":
                j = get_json(f"{base}/geocode/json", {"address": q, "key": key}, ctx=ctx)
                items = [{"name": r["formatted_address"], "lat": r["geometry"]["location"]["lat"],
                          "lon": r["geometry"]["location"]["lng"], "types": ",".join(r.get("types", [])),
                          "place_id": r.get("place_id")} for r in j.get("results", [])]
            elif mode.startswith("Places"):
                j = get_json(f"{base}/place/textsearch/json", {"query": q, "key": key}, ctx=ctx)
                items = [{"name": r.get("name"), "address": r.get("formatted_address"), "lat": r["geometry"]["location"]["lat"],
                          "lon": r["geometry"]["location"]["lng"], "rating": r.get("rating"),
                          "ratings": r.get("user_ratings_total"), "place_id": r.get("place_id")} for r in j.get("results", [])]
            else:
                j = get_json(f"{base}/distancematrix/json", {"origins": q, "destinations": s.get("destination"),
                                                            "mode": s.get("travel_mode"), "key": key}, ctx=ctx)
                items = []
                for e in (j.get("rows") or [{}])[0].get("elements", []):
                    items.append({"destination": (j.get("destination_addresses") or [None])[0], "status": e.get("status"),
                                  "meters": (e.get("distance") or {}).get("value"), "seconds": (e.get("duration") or {}).get("value"),
                                  "text": f"{(e.get('distance') or {}).get('text')}, {(e.get('duration') or {}).get('text')}"})
            if j.get("status") not in ("OK", "ZERO_RESULTS", None):
                raise InvalidSettingsError(f"Google Maps: {j.get('status')} - {j.get('error_message', '')}")
            rows += with_query(items, q)
        return [table(rows)]


WEATHER_DAILY = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "precipitation_probability_max",
                 "wind_speed_10m_max", "weather_code", "sunshine_duration", "uv_index_max"]
WEATHER_HOURLY = ["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m", "cloud_cover",
                  "weather_code", "apparent_temperature"]


def _place(name, ctx, state):
    """(name, lat, lon) of a place: Open-Meteo's geocoding, else OpenStreetMap's (Nominatim) - also for
    the rest of the run once Open-Meteo's does not answer."""
    if not state.get("meteo_down"):
        try:
            g = request("GET", "https://geocoding-api.open-meteo.com/v1/search", {"name": name, "count": 1},
                        timeout=8, retries=0, ctx=ctx).json()
            hit = (g.get("results") or [None])[0]
            if hit is not None:
                return f"{hit['name']}, {hit.get('country', '')}".strip(", "), hit["latitude"], hit["longitude"]
        except InvalidSettingsError:
            state["meteo_down"] = True
    hits = get_json("https://nominatim.openstreetmap.org/search", {"q": name, "format": "jsonv2", "limit": 1}, ctx=ctx)
    if not hits:
        raise InvalidSettingsError(f"No place {name!r} found")
    return name, float(hits[0]["lat"]), float(hits[0]["lon"])


class Weather(_WebNode):
    factory = "verknode.web.Weather"
    name = "Weather (Open-Meteo)"
    category = CAT_MAPS
    icon = "☀"
    short_description = "Weather forecasts (and the past days) for places or coordinates - Open-Meteo, no key."
    description = (
        "The daily or hourly forecast - up to 16 days, and past days - of the weather at each place: the "
        "lat / lon columns of the input table (e.g. of OpenStreetMap Geocode), or place names (found with "
        "Open-Meteo's geocoding). One row per place and day (or hour), the chosen variables as columns; "
        "weather_code is the WMO code. Free for non-commercial use (open-meteo.com)."
    )
    fields = [
        F("lat", "column", "Latitude column", None, port=1),
        F("lon", "column", "Longitude column", None, port=1),
        F("name", "column", "Place name column", None, "Shown in the place column (optional).", port=1),
        F("places", "code", "Places (names)", "Kyoto\nLisbon", "Without coordinate columns: one per line."),
        F("step", "enum", "Every", "Day", options=["Day", "Hour"]),
        F("daily", "multienum", "Daily variables", ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"],
          options=WEATHER_DAILY),
        F("hourly", "multienum", "Hourly variables", ["temperature_2m", "precipitation"], options=WEATHER_HOURLY),
        F("days", "int", "Forecast days", 7, "1 to 16."),
        F("past_days", "int", "Past days", 0, "0 to 92."),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame()]
        df = inputs[0] if inputs and inputs[0] is not None else None
        places = []
        if df is not None and s.get("lat") and s.get("lon"):
            for r in df.iter_rows(named=True):
                if r[s["lat"]] is not None and r[s["lon"]] is not None:
                    places.append((str(r.get(s["name"])) if s.get("name") else f"{r[s['lat']]:.4f},{r[s['lon']]:.4f}",
                                   r[s["lat"]], r[s["lon"]]))
        else:
            state = {}
            for _, name in queries([None], s, "", "places", "place"):
                places.append(_place(name, ctx, state))
        hourly = (s.get("step") or "Day") == "Hour"
        variables = (s.get("hourly") if hourly else s.get("daily")) or (WEATHER_HOURLY[:2] if hourly else WEATHER_DAILY[:3])
        frames = []
        for name, lat, lon in places:
            j = get_json("https://api.open-meteo.com/v1/forecast", {
                "latitude": lat, "longitude": lon, ("hourly" if hourly else "daily"): ",".join(variables),
                "forecast_days": max(1, min(16, int(s.get("days") or 7))), "past_days": max(0, min(92, int(s.get("past_days") or 0))),
                "timezone": "auto"}, ctx=ctx)
            block = j["hourly" if hourly else "daily"]
            t = pl.DataFrame(block).with_columns(pl.lit(name).alias("place"), pl.lit(lat).alias("lat"), pl.lit(lon).alias("lon"))
            t = t.with_columns(pl.col("time").str.to_datetime() if hourly else pl.col("time").str.to_date())
            frames.append(t.select(["place", "lat", "lon", "time"] + [c for c in t.columns if c not in ("place", "lat", "lon", "time")]))
        return [pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()]


# ---------------------------------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------------------------------
PODCAST_SCHEMA = {"query": pl.String, "podcast": pl.String, "artist": pl.String, "feed_url": pl.String,
                  "genre": pl.String, "episodes": pl.Int64, "latest": pl.String, "country": pl.String,
                  "artwork": pl.String, "url": pl.String}


class ApplePodcasts(_WebNode):
    factory = "verknode.web.ApplePodcasts"
    name = "Apple Podcasts Search"
    category = CAT_MEDIA
    icon = "🎙"
    short_description = "Podcasts in Apple's podcast directory, with their RSS feed (iTunes Search API)."
    description = (
        "Searches Apple's podcast directory for each term and outputs the podcasts: name, artist, genre, number "
        "of episodes, latest release and their RSS feed URL - read the episodes with an RSS / Atom Reader on the "
        "feed_url column."
    )
    SCHEMA = PODCAST_SCHEMA
    fields = [
        column_field("Search term column"),
        F("query", "code", "Search terms", "science", "One per line."),
        F("country", "string", "Store country", "US", "Two-letter country code."),
        F("limit", "int", "Podcasts per term", 10),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=PODCAST_SCHEMA)]
        rows = []
        for _, q in queries(inputs, s, "column", "query", "search term"):
            j = get_json("https://itunes.apple.com/search", {"media": "podcast", "term": q, "country": s.get("country") or "US",
                                                            "limit": int(s.get("limit") or 10)}, ctx=ctx)
            for r in j.get("results", []):
                rows.append({"query": q, "podcast": r.get("collectionName"), "artist": r.get("artistName"),
                             "feed_url": r.get("feedUrl"), "genre": r.get("primaryGenreName"), "episodes": r.get("trackCount"),
                             "latest": r.get("releaseDate"), "country": r.get("country"), "artwork": r.get("artworkUrl600"),
                             "url": r.get("collectionViewUrl")})
        return [pl.DataFrame(rows, schema=PODCAST_SCHEMA) if rows else pl.DataFrame(schema=PODCAST_SCHEMA)]
