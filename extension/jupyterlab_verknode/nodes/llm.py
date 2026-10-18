"""LLM nodes: prompts to a large language model served by Ollama or by an OpenAI-compatible
server (llama.cpp, vLLM, LM Studio, OpenAI, ...), one per row of a table.

Where the server is comes from the node, or else from the environment of the Jupyter server:

    VERKNODE_LLM_URL       e.g. http://localhost:11434 (the default), http://ollama:11434
    VERKNODE_LLM_MODEL     the model when the node does not name one
    VERKNODE_LLM_PROVIDER  ollama | openai (when the node says "Default")
    VERKNODE_LLM_API_KEY   the key for servers that need one (never stored in the workflow)

Without the environment variable, a setting is read from ~/.config/verknode/llm.env (KEY=VALUE
lines; another file: VERKNODE_LLM_CONFIG) - e.g. for a server that is already running.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

CAT = "AI/LLM"
DEFAULT_URL = "http://localhost:11434"
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")
TYPES = {"string": pl.String, "number": pl.Float64, "integer": pl.Int64, "boolean": pl.Boolean}
JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean"}

SERVER_HELP = ("Empty: the VERKNODE_LLM_URL environment variable of the Jupyter server, else "
               f"{DEFAULT_URL} (Ollama on this machine).")


CONFIG = os.path.join("~", ".config", "verknode", "llm.env")


def setting(name: str) -> str:
    """An LLM setting of the Jupyter server: the environment variable, else the config file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.path.expanduser(os.environ.get("VERKNODE_LLM_CONFIG", "").strip() or CONFIG)
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == name and not key.strip().startswith("#"):
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def server_fields():
    return [
        F("provider", "enum", "Server type", "Default", "Default: VERKNODE_LLM_PROVIDER, else Ollama. "
          "OpenAI-compatible: llama.cpp, vLLM, LM Studio, OpenAI and the like (/v1/chat/completions).",
          options=["Default", "Ollama", "OpenAI-compatible"]),
        F("url", "string", "Server URL", "", SERVER_HELP),
        F("model", "string", "Model", "", "Empty: VERKNODE_LLM_MODEL. The dialog lists the server's models."),
        F("api_key_env", "string", "API key variable", "VERKNODE_LLM_API_KEY",
          "Environment variable of the Jupyter server with the key (sent as a Bearer token if set). "
          "Keys are never stored in the workflow."),
        F("temperature", "float", "Temperature", 0.2, "0: the most likely answer; higher: more varied."),
        F("max_tokens", "int", "Longest answer (tokens)", 1024),
        F("seed", "int", "Seed", 0, "Same seed, same prompt: the same answer (if the server supports it). 0: none."),
        F("think", "bool", "Let reasoning models think first", False,
          "Off: ask for the answer directly (faster). On: models that reason (e.g. qwen3, deepseek-r1) "
          "think before answering; the thinking is not part of the answer."),
        F("timeout", "int", "Timeout per request (s)", 300),
    ]


def server(s: dict) -> dict:
    provider = s.get("provider") or "Default"
    if provider == "Default":
        provider = "OpenAI-compatible" if setting("VERKNODE_LLM_PROVIDER").lower() in (
            "openai", "openai-compatible", "llamacpp", "llama.cpp", "vllm") else "Ollama"
    url = (s.get("url") or "").strip() or setting("VERKNODE_LLM_URL") or DEFAULT_URL
    model = (s.get("model") or "").strip() or setting("VERKNODE_LLM_MODEL")
    key = setting((s.get("api_key_env") or "").strip() or "VERKNODE_LLM_API_KEY")
    return {"provider": provider, "url": url.rstrip("/"), "model": model, "key": key}


def _request(url: str, body: dict | None, key: str, timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail).get("error", detail)
            detail = detail.get("message", detail) if isinstance(detail, dict) else detail
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"{e.code} from the LLM server: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach the LLM server at {url.split('/api/')[0].split('/v1/')[0]}: {e.reason}") from None


def _openai_base(url: str) -> str:
    return url if url.endswith("/v1") else url + "/v1"


def list_models(srv: dict, timeout: float = 3) -> list:
    if srv["provider"] == "Ollama":
        return sorted(m["name"] for m in _request(srv["url"] + "/api/tags", None, srv["key"], timeout).get("models", []))
    return sorted(m["id"] for m in _request(_openai_base(srv["url"]) + "/models", None, srv["key"], timeout).get("data", []))


def chat(srv: dict, messages: list, s: dict, schema: dict | None = None) -> dict:
    """One answer: {"text", "tokens_in", "tokens_out", "seconds"}."""
    if not srv["model"]:
        raise InvalidSettingsError("No model: set Model, or VERKNODE_LLM_MODEL for the Jupyter server")
    t0 = time.time()
    timeout = max(1, int(s.get("timeout") or 300))
    if srv["provider"] == "Ollama":
        options = {"temperature": float(s.get("temperature") or 0.0), "num_predict": int(s.get("max_tokens") or 1024)}
        if s.get("seed"):
            options["seed"] = int(s["seed"])
        body = {"model": srv["model"], "messages": messages, "stream": False, "options": options}
        if not s.get("think"):
            body["think"] = False
        if schema is not None:
            body["format"] = schema
        try:
            r = _request(srv["url"] + "/api/chat", body, srv["key"], timeout)
        except RuntimeError as e:
            if "think" not in body or "think" not in str(e).lower():
                raise
            del body["think"]  # (a model that does not think at all)
            r = _request(srv["url"] + "/api/chat", body, srv["key"], timeout)
        return {"text": (r.get("message") or {}).get("content", ""), "tokens_in": r.get("prompt_eval_count"),
                "tokens_out": r.get("eval_count"), "seconds": time.time() - t0}
    body = {"model": srv["model"], "messages": messages, "temperature": float(s.get("temperature") or 0.0),
            "max_tokens": int(s.get("max_tokens") or 1024)}
    if s.get("seed"):
        body["seed"] = int(s["seed"])
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
    r = _request(_openai_base(srv["url"]) + "/chat/completions", body, srv["key"], timeout)
    usage = r.get("usage") or {}
    return {"text": ((r.get("choices") or [{}])[0].get("message") or {}).get("content", ""),
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
            "seconds": time.time() - t0}


def parse_fields(text: str) -> list:
    """"sentiment, score:number" -> [("sentiment", "string"), ("score", "number")]."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, kind = part.partition(":")
        kind = (kind.strip().lower() or "string")
        if kind not in TYPES:
            raise InvalidSettingsError(f'Field "{part}": the type is one of {", ".join(TYPES)}')
        out.append((name.strip(), kind))
    return out


def fill(template: str, row: dict) -> str:
    """The prompt for a row: {Column} becomes its value ({{ and }}: literal braces)."""
    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        v = row[m.group(1).strip()]
        return "" if v is None else str(v)
    return PLACEHOLDER.sub(sub, template)


def placeholders(template: str) -> list:
    return [m.group(1).strip() for m in PLACEHOLDER.finditer(template) if m.group(1)]


def _json_answer(text: str):
    """The first JSON object of an answer (models may wrap it in ``` fences or add words after it)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def _cast(v, kind: str):
    if v is None:
        return None
    try:
        if kind == "number":
            return float(v)
        if kind == "integer":
            return int(float(v))
        if kind == "boolean":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, str) else json.dumps(v)


class LLMPrompter(NodeModel):
    factory = "verknode.llm.Prompter"
    name = "LLM Prompter"
    category = CAT
    icon = "LLM"
    node_type = "Manipulator"
    short_description = "Asks a large language model (Ollama, OpenAI-compatible) once per row."
    description = (
        "Fills the prompt template with the values of each row ({Column} placeholders; {{ and }} for "
        "literal braces) and adds the model's answer as a column. Answers can also be JSON with the fields "
        "you name (e.g. <code>sentiment, score:number</code>; types string, number, integer, boolean): "
        "one column per field, the model held to that shape where the server supports it (Ollama, "
        "llama.cpp). Without an input table the prompt is sent once. Requests run several at a time. "
        "The server: an Ollama URL (default " + DEFAULT_URL + ") or an OpenAI-compatible one; empty "
        "settings use the VERKNODE_LLM_URL / VERKNODE_LLM_MODEL / VERKNODE_LLM_PROVIDER environment "
        "variables of the Jupyter server, and an API key comes from an environment variable (never from "
        "the workflow). Answers depend on the model; check them before relying on them."
    )
    in_ports = [Port(TABLE, "Rows", "A prompt for each row (optional: none = one prompt)", optional=True)]
    out_ports = [Port(TABLE, "Answers", "The input with the answer columns")]
    fields = [
        F("system", "code", "System prompt", "You are a careful assistant. Answer briefly.",
          "Instructions for every request (empty: none)."),
        F("prompt", "code", "Prompt", "Summarize in one sentence: {Text}",
          "{Column} is replaced by the row's value."),
        F("answer", "enum", "Answer", "Text", options=["Text", "JSON fields"]),
        F("fields", "string", "JSON fields", "answer", "Comma separated, each name or name:type "
          "(string, number, integer, boolean).", show_if={"answer": "JSON fields"}),
        F("new_column", "string", "Answer column", "Answer", show_if={"answer": "Text"}),
        F("keep_text", "bool", "Also keep the answer text", False, show_if={"answer": "JSON fields"}),
        F("replace", "bool", "Replace answer columns already in the input", False,
          "E.g. in a loop that asks again with the previous answer (a coding agent)."),
        F("parallel", "int", "Requests at a time", 4, "Ollama: set OLLAMA_NUM_PARALLEL on its server too."),
        F("limit", "int", "Rows at most", 100, "Protects against sending a huge table by mistake. 0: all rows."),
        F("on_error", "enum", "If a request fails", "Fail the node",
          options=["Fail the node", "Missing answer and an error column"]),
        F("stats", "bool", "Add time and token columns", False),
        *server_fields(),
    ]

    # -- columns ------------------------------------------------------------
    def _answer_columns(self, s) -> dict:
        if s.get("answer") == "JSON fields":
            cols = {name: TYPES[kind] for name, kind in parse_fields(s.get("fields"))}
            if not cols:
                raise InvalidSettingsError("Name at least one JSON field")
            if s.get("keep_text"):
                cols["LLM answer"] = pl.String
        else:
            cols = {(s.get("new_column") or "Answer"): pl.String}
        if s.get("on_error") != "Fail the node":
            cols["LLM error"] = pl.String
        if s.get("stats"):
            cols.update({"LLM seconds": pl.Float64, "LLM tokens in": pl.Int64, "LLM tokens out": pl.Int64})
        return cols

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0] if in_specs and in_specs[0] is not None else pl.Schema()
        missing = [p for p in placeholders(s.get("prompt") or "") + placeholders(s.get("system") or "")
                   if p not in spec]
        if in_specs and in_specs[0] is not None and missing:
            raise InvalidSettingsError(f"Not a column of the input: {', '.join('{' + m + '}' for m in missing)}")
        cols = self._answer_columns(s)
        clash = [c for c in cols if c in spec]
        if clash and not s.get("replace"):
            raise InvalidSettingsError(f"The input already has the column(s) {', '.join(clash)} "
                                       "(or turn on Replace answer columns)")
        return [pl.Schema({**dict(spec), **cols})]

    def dialog_data(self, inputs, s, connected=(), context=None):
        srv = server(s)
        out = {"placeholder": {"url": setting("VERKNODE_LLM_URL") or DEFAULT_URL,
                               "model": setting("VERKNODE_LLM_MODEL") or "(a model of the server)"}}
        try:
            out["suggest"] = {"model": list_models(srv)}
        except Exception as e:  # noqa: BLE001 - the dialog opens anyway
            out["error"] = str(e)
        return out

    # -- running --------------------------------------------------------------
    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if ctx.dry:
            return [pl.DataFrame(schema=self.configure([df.schema if df is not None else None], s, ctx)[0])]
        rows = [{}] if df is None else df.to_dicts()
        limit = int(s.get("limit") or 0)
        if limit and len(rows) > limit:
            raise InvalidSettingsError(f"{len(rows)} rows, more than Rows at most ({limit}): raise it (0: all rows)")
        srv = server(s)
        fields = parse_fields(s.get("fields")) if s.get("answer") == "JSON fields" else None
        schema = None
        if fields:
            schema = {"type": "object", "properties": {n: {"type": JSON_TYPES[k]} for n, k in fields},
                      "required": [n for n, _ in fields]}
        system = (s.get("system") or "").strip()
        names = ", ".join(f'"{n}" ({k})' for n, k in fields) if fields else ""

        def one(row):
            ctx.check_canceled()
            messages = []
            sys_text = fill(system, row) if system else ""
            if fields:
                sys_text = (sys_text + "\n\n" if sys_text else "") + f"Answer with a JSON object with the keys {names}."
            if sys_text:
                messages.append({"role": "system", "content": sys_text})
            messages.append({"role": "user", "content": fill(s.get("prompt") or "", row)})
            r = chat(srv, messages, s, schema)
            if fields:
                try:
                    r["json"] = _json_answer(r["text"])
                    if not isinstance(r["json"], dict):
                        raise ValueError("not an object")
                except ValueError as e:
                    raise RuntimeError(f"The answer is not the JSON asked for ({e}): {r['text'][:120]!r}") from None
            return r

        results, done = [None] * len(rows), [0]
        workers = max(1, min(int(s.get("parallel") or 1), len(rows)))
        ctx.log(f"{srv['provider']} {srv['url']} · {srv['model'] or '(no model)'} · {len(rows)} prompt(s)")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, row): i for i, row in enumerate(rows)}
            for fut in futures:
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except InvalidSettingsError:
                    raise
                except Exception as e:  # noqa: BLE001 - per row, or the whole node (on_error)
                    if s.get("on_error") == "Fail the node":
                        for f in futures:
                            f.cancel()
                        raise RuntimeError(f"Row {i + 1}: {e}") from None
                    results[i] = {"error": str(e)}
                done[0] += 1
                ctx.set_progress(done[0] / len(rows))
        cols = self._answer_columns(s)
        data = {c: [] for c in cols}
        for r in results:
            if fields:
                for n, k in fields:
                    data[n].append(_cast((r.get("json") or {}).get(n), k))
                if "LLM answer" in data:
                    data["LLM answer"].append(r.get("text"))
            else:
                data[next(iter(cols))].append(r.get("text"))
            if "LLM error" in data:
                data["LLM error"].append(r.get("error"))
            if "LLM seconds" in data:
                data["LLM seconds"].append(r.get("seconds"))
                data["LLM tokens in"].append(r.get("tokens_in"))
                data["LLM tokens out"].append(r.get("tokens_out"))
        answers = pl.DataFrame({c: pl.Series(c, v, dtype=cols[c]) for c, v in data.items()})
        failed = sum(1 for r in results if r.get("error"))
        if failed:
            ctx.warn(f"{failed} of {len(rows)} request(s) failed (see the LLM error column)")
        tokens = sum(r.get("tokens_out") or 0 for r in results)
        ctx.log(f"{len(rows) - failed} answer(s), {tokens} tokens")
        if df is None:
            return [answers]
        # (replaced columns stay where they were)
        return [df.with_columns(answers[c] for c in answers.columns if c in df.columns)
                .hstack(answers.select(c for c in answers.columns if c not in df.columns))]
