"""LLM nodes: prompts to a large language model served by Ollama or by an OpenAI-compatible
server (llama.cpp, vLLM, LM Studio, OpenAI, ...), one per row of a table - and the model calls all LLM
nodes use: chat() and chat_tools(), for Ollama, OpenAI-compatible servers and APIs, Anthropic's
Messages API and models run in this process with llama.cpp.

An LLM node's LLM port takes a connection of an LLM connector node (nodes/llm_connect.py) instead of
its own server settings (server_for).

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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from verknode.node import F, InvalidSettingsError, LLM, LLMConnectionPortObject, NodeModel, Port, TABLE

CAT = "AI/LLM"
DEFAULT_URL = "http://localhost:11434"
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")
TYPES = {"string": pl.String, "number": pl.Float64, "integer": pl.Int64, "boolean": pl.Boolean}
JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean"}

ANTHROPIC = "Anthropic"
LOCAL = "llama.cpp (in-process)"
ANTHROPIC_VERSION = "2023-06-01"

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


def llm_port(desc="A model connection of an LLM connector node (optional: else the server settings below)"):
    """The LLM port of an LLM node (last, so the other ports keep their numbers)."""
    return Port(LLM, "LLM", desc, optional=True)


def connection_of(inputs) -> LLMConnectionPortObject | None:
    return next((x for x in inputs or [] if isinstance(x, LLMConnectionPortObject)), None)


def server_for(s: dict, inputs) -> dict:
    """The server of a node: its LLM port's connection, else its settings (and the environment)."""
    conn = connection_of(inputs)
    return conn.server(s) if conn is not None else server(s)


def server(s: dict) -> dict:
    provider = s.get("provider") or "Default"
    if provider == "Default":
        provider = "OpenAI-compatible" if setting("VERKNODE_LLM_PROVIDER").lower() in (
            "openai", "openai-compatible", "llamacpp", "llama.cpp", "vllm") else "Ollama"
    url = (s.get("url") or "").strip() or setting("VERKNODE_LLM_URL") or DEFAULT_URL
    model = (s.get("model") or "").strip() or setting("VERKNODE_LLM_MODEL")
    key = setting((s.get("api_key_env") or "").strip() or "VERKNODE_LLM_API_KEY")
    return {"provider": provider, "url": url.rstrip("/"), "model": model, "key": key}


def _auth(srv: dict) -> dict:
    """The headers of a server's requests (its key: Bearer, or Anthropic's x-api-key)."""
    if srv.get("provider") == ANTHROPIC:
        return {"x-api-key": srv.get("key") or "", "anthropic-version": ANTHROPIC_VERSION, **(srv.get("headers") or {})}
    return {**({"Authorization": f"Bearer {srv['key']}"} if srv.get("key") else {}), **(srv.get("headers") or {})}


def _request(url: str, body: dict | None, key: str, timeout: float, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if headers is not None:
        for k, v in headers.items():
            if v:
                req.add_header(k, v)
    elif key:
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
    except TimeoutError:
        raise RuntimeError(f"The LLM server at {url.split('/api/')[0].split('/v1/')[0]} did not answer in {timeout:.0f} s") from None


def _openai_base(url: str) -> str:
    return url if url.endswith("/v1") else url + "/v1"


def list_models(srv: dict, timeout: float = 3) -> list:
    if srv["provider"] == "Ollama":
        return sorted(m["name"] for m in _request(srv["url"] + "/api/tags", None, srv["key"], timeout).get("models", []))
    if srv["provider"] == LOCAL:
        return [os.path.basename(srv["url"])]
    if srv["provider"] == ANTHROPIC:
        return sorted(m["id"] for m in _request(_anthropic_base(srv["url"]) + "/models", None, "", timeout, _auth(srv)).get("data", []))
    return sorted(m["id"] for m in _request(_openai_base(srv["url"]) + "/models", None, srv["key"], timeout, _auth(srv)).get("data", []))


def _extra(body: dict, s: dict):
    """What a connection adds to OpenAI-style requests (extra_body: e.g. Ollama's reasoning_effort none,
    unless the node lets the model think)."""
    for k, v in (s.get("extra_body") or {}).items():
        if not (k == "reasoning_effort" and s.get("think")):
            body.setdefault(k, v)


def _with_options(srv: dict, s: dict) -> dict:
    """The node's settings, with what its connection sets (e.g. a temperature of its own)."""
    return {**(s or {}), **{k: v for k, v in (srv.get("options") or {}).items() if v not in (None, "")}}


def chat(srv: dict, messages: list, s: dict, schema: dict | None = None) -> dict:
    """One answer: {"text", "tokens_in", "tokens_out", "seconds"}."""
    if not srv["model"] and srv["provider"] != LOCAL:
        raise InvalidSettingsError("No model: set Model, or VERKNODE_LLM_MODEL for the Jupyter server")
    s = _with_options(srv, s)
    if srv["provider"] == ANTHROPIC:
        return _anthropic(srv, messages, s, schema=schema)
    if srv["provider"] == LOCAL:
        return _local_chat(srv, messages, s, schema)
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
    _extra(body, s)
    r = _request(_openai_base(srv["url"]) + "/chat/completions", body, srv["key"], timeout, _auth(srv))
    usage = r.get("usage") or {}
    return {"text": ((r.get("choices") or [{}])[0].get("message") or {}).get("content", ""),
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
            "seconds": time.time() - t0}


def chat_tools(srv: dict, messages: list, s: dict, tools: list) -> dict:
    """One step of a conversation in which the model calls tools (functions):
    {"text", "calls": [{"id", "name", "args"}], "message": the model's message (append it), "seconds"}.
    Answer each call with tool_message(srv, call, result) before the next step."""
    if not srv["model"] and srv["provider"] != LOCAL:
        raise InvalidSettingsError("No model: set Model, or VERKNODE_LLM_MODEL for the Jupyter server")
    s = _with_options(srv, s)
    if srv["provider"] == ANTHROPIC:
        return _anthropic(srv, messages, s, tools=tools)
    if srv["provider"] == LOCAL:
        return _local_tools(srv, messages, s, tools)
    t0 = time.time()
    timeout = max(1, int(s.get("timeout") or 300))
    specs = [{"type": "function", "function": t} for t in tools]
    if srv["provider"] == "Ollama":
        options = {"temperature": float(s.get("temperature") or 0.0), "num_predict": int(s.get("max_tokens") or 2048)}
        if s.get("seed"):
            options["seed"] = int(s["seed"])
        body = {"model": srv["model"], "messages": messages, "stream": False, "options": options, "tools": specs}
        if not s.get("think"):
            body["think"] = False
        try:
            r = _request(srv["url"] + "/api/chat", body, srv["key"], timeout)
        except RuntimeError as e:
            if "think" not in body or "think" not in str(e).lower():
                raise
            del body["think"]
            r = _request(srv["url"] + "/api/chat", body, srv["key"], timeout)
        msg = r.get("message") or {}
        raw = msg.get("tool_calls") or []
    else:
        body = {"model": srv["model"], "messages": messages, "tools": specs,
                "temperature": float(s.get("temperature") or 0.0), "max_tokens": int(s.get("max_tokens") or 2048)}
        if s.get("seed"):
            body["seed"] = int(s["seed"])
        _extra(body, s)
        r = _request(_openai_base(srv["url"]) + "/chat/completions", body, srv["key"], timeout, _auth(srv))
        msg = ((r.get("choices") or [{}])[0].get("message") or {})
        raw = msg.get("tool_calls") or []
    calls = []
    for i, c in enumerate(raw):
        f = c.get("function") or {}
        args = f.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_unreadable": args}
        calls.append({"id": c.get("id") or f"call_{i}", "name": f.get("name"), "args": args if isinstance(args, dict) else {}})
    message = {"role": "assistant", "content": msg.get("content") or ""}
    if raw:
        message["tool_calls"] = raw
    usage = r.get("usage") or {}
    return {"text": msg.get("content") or "", "calls": calls, "message": message, "seconds": time.time() - t0,
            "tokens_in": r.get("prompt_eval_count", usage.get("prompt_tokens")),
            "tokens_out": r.get("eval_count", usage.get("completion_tokens"))}


def tool_message(srv: dict, call: dict, content: str) -> dict:
    """The answer to a tool call, for the next chat_tools step."""
    if srv["provider"] == "Ollama":
        return {"role": "tool", "content": content, "tool_name": call["name"]}
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


# ---------------------------------------------------------------------------------------------------
# Anthropic's Messages API (the conversation is kept in the OpenAI form; converted for each request)
# ---------------------------------------------------------------------------------------------------
def _anthropic_base(url: str) -> str:
    url = (url or "https://api.anthropic.com").rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


def _args(f: dict) -> dict:
    args = f.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    return args if isinstance(args, dict) else {}


def anthropic_messages(messages: list) -> tuple[str, list]:
    """(system, messages) of a conversation for the Messages API: system prompts apart, tool calls as
    tool_use blocks, tool answers as tool_result blocks of a user turn, turns of one role merged."""
    system = "\n\n".join(m.get("content") or "" for m in messages if m.get("role") == "system" and m.get("content"))
    out: list = []

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"] += blocks
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "", "content": m.get("content") or "(empty)"}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if (m.get("content") or "").strip() else []
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                blocks.append({"type": "tool_use", "id": c.get("id") or "", "name": f.get("name") or "", "input": _args(f)})
            add("assistant", blocks or [{"type": "text", "text": "(no answer)"}])
        else:
            add("user", [{"type": "text", "text": m.get("content") or "(empty)"}])
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(the conversation begins)"}]})
    return system, out


def _anthropic(srv: dict, messages: list, s: dict, tools: list | None = None, schema: dict | None = None) -> dict:
    t0 = time.time()
    system, msgs = anthropic_messages(messages)
    body = {"model": srv["model"], "messages": msgs, "max_tokens": int(s.get("max_tokens") or 1024),
            "temperature": float(s.get("temperature") or 0.0)}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t.get("description") or "",
                          "input_schema": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]
    if schema is not None:  # (the answer as the input of a tool it must call)
        body["tools"] = [{"name": "answer", "description": "Give the answer.", "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "answer"}
    r = _request(_anthropic_base(srv["url"]) + "/messages", body, "", max(1, int(s.get("timeout") or 300)), _auth(srv))
    blocks = r.get("content") or []
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    usage = r.get("usage") or {}
    out = {"tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"), "seconds": time.time() - t0}
    if schema is not None:
        return {**out, "text": json.dumps(uses[0].get("input") or {}) if uses else text}
    calls = [{"id": b.get("id") or f"call_{i}", "name": b.get("name"), "args": b.get("input") or {}} for i, b in enumerate(uses)]
    message = {"role": "assistant", "content": text}
    if uses:
        message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                 for c in calls]
    return {**out, "text": text, "calls": calls, "message": message}


# ---------------------------------------------------------------------------------------------------
# llama.cpp in this process (llama-cpp-python): a GGUF model file; tools by JSON-schema-constrained output
# ---------------------------------------------------------------------------------------------------
_LOCAL: dict = {}
_LOCAL_LOCK = threading.Lock()


def _local_model(srv: dict):
    """The loaded model (once per file and options) and the lock its calls take (one at a time)."""
    path = os.path.expanduser(srv["url"])
    o = srv.get("options") or {}
    key = (path, int(o.get("n_ctx") or 8192), int(o.get("n_gpu_layers") or 0), int(o.get("n_threads") or 0))
    with _LOCAL_LOCK:
        if key not in _LOCAL:
            if not os.path.isfile(path):
                raise InvalidSettingsError(f"There is no model file {path} (a GGUF file)")
            try:
                from llama_cpp import Llama
            except ImportError:
                raise InvalidSettingsError("Models in this process need llama-cpp-python (pip install llama-cpp-python)") from None
            _LOCAL[key] = (Llama(model_path=path, n_ctx=key[1], n_gpu_layers=key[2], n_threads=key[3] or None,
                                 verbose=False), threading.Lock())
        return _LOCAL[key]


def _local_messages(messages: list) -> list:
    """Tool calls and answers as text (the model's chat template may know no tools)."""
    out, names = [], {}
    for m in messages:
        role = m.get("role")
        if role == "tool":
            name = m.get("tool_name") or names.get(m.get("tool_call_id")) or "the tool"
            out.append({"role": "user", "content": f"Result of {name}:\n{m.get('content') or ''}"})
        elif role == "assistant" and m.get("tool_calls"):
            names.update({c.get("id"): (c.get("function") or {}).get("name") for c in m["tool_calls"]})
            calls = [{"tool": (c.get("function") or {}).get("name"), "args": _args(c.get("function") or {})} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": json.dumps(calls[0] if len(calls) == 1 else calls)})
        else:
            out.append({"role": role if role in ("system", "user", "assistant") else "user", "content": m.get("content") or ""})
    return out


def _local_run(srv, messages, s, schema=None) -> dict:
    llm, lock = _local_model(srv)
    kw = {"temperature": float(s.get("temperature") or 0.0), "max_tokens": int(s.get("max_tokens") or 1024)}
    if s.get("seed"):
        kw["seed"] = int(s["seed"])
    if schema is not None:
        kw["response_format"] = {"type": "json_object", "schema": schema}
    t0 = time.time()
    with lock:
        r = llm.create_chat_completion(messages=messages, **kw)
    usage = r.get("usage") or {}
    return {"text": ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "",
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"), "seconds": time.time() - t0}


def _local_chat(srv, messages, s, schema=None) -> dict:
    return _local_run(srv, _local_messages(messages), s, schema)


def _local_tools(srv, messages, s, tools) -> dict:
    listing = "\n".join(f"- {t['name']}: {t.get('description') or ''} Arguments: {json.dumps(t.get('parameters') or {})}" for t in tools)
    msgs = _local_messages(messages)
    note = (f"You can call these tools:\n{listing}\n\nReply with JSON only: {{\"tool\": name, \"args\": {{...}}}} to call "
            "a tool (you then get its result), or {\"answer\": \"...\"} with your final answer.")
    if msgs and msgs[0]["role"] == "system":
        msgs[0] = {"role": "system", "content": msgs[0]["content"] + "\n\n" + note}
    else:
        msgs.insert(0, {"role": "system", "content": note})
    schema = {"anyOf": [{"type": "object", "properties": {"tool": {"const": t["name"]},
                                                          "args": t.get("parameters") or {"type": "object"}},
                         "required": ["tool", "args"]} for t in tools]
              + [{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}]}
    r = _local_run(srv, msgs, s, schema)
    try:
        j = _json_answer(r["text"])
    except ValueError:
        j = {"answer": r["text"]}
    if isinstance(j, dict) and j.get("tool"):
        call = {"id": f"call_{int(time.time() * 1000) % 100000}", "name": j["tool"], "args": j.get("args") or {}}
        message = {"role": "assistant", "content": "", "tool_calls": [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}]}
        return {**r, "text": "", "calls": [call], "message": message}
    text = str(j.get("answer", r["text"])) if isinstance(j, dict) else r["text"]
    return {**r, "text": text, "calls": [], "message": {"role": "assistant", "content": text}}


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
    in_ports = [Port(TABLE, "Rows", "A prompt for each row (optional: none = one prompt)", optional=True), llm_port()]
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
        srv = server_for(s, inputs)
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
        srv = server_for(s, inputs)
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
