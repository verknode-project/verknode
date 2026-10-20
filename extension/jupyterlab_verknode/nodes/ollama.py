"""Ollama management nodes: the models of an Ollama server, what is loaded (and how much of it sits in
GPU memory), model details, loading / unloading, downloading, creating and deleting models.

The server: an Ollama connection on the node's LLM port (Ollama Connector), else the node's URL, else
the VERKNODE_LLM_URL environment variable, else http://localhost:11434 (see llm.py and docs/llm.md).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, dtype_kind

from .llm import DEFAULT, DEFAULT_URL, connection_of, default_server_name, llm_port, saved_server, saved_servers, setting

CAT = "AI/Ollama"
GB = 1e9
URL_HELP = f"Empty: the VERKNODE_LLM_URL environment variable of the Jupyter server, else {DEFAULT_URL}."
UTC = pl.Datetime("us", "UTC")


LLM_IN = llm_port("An Ollama connection (Ollama Connector): its server (optional: else the Ollama URL)")


def ollama_settings(s: dict, inputs) -> dict:
    """The node's settings with its server: the Ollama connection on its LLM port, else its saved server, else
    its URL, else the default saved server (an Ollama)."""
    conn = connection_of(inputs)
    if conn is None:
        name = (s.get("server") or DEFAULT).strip()
        if name == DEFAULT:
            if (s.get("url") or "").strip():
                return s
            name = default_server_name()  # (the default saved server, if it is an Ollama)
            r = saved_servers().get(name) if name else None
            if r is None or (r.get("provider") or "Ollama") != "Ollama":
                return s
        srv = saved_server(name)
        if srv["provider"] != "Ollama":
            raise InvalidSettingsError(f"The saved server {name} is not an Ollama server")
        return {**s, "url": srv["url"], "_key": srv["key"]}
    srv = conn.server()
    url = srv["url"]
    if srv["provider"] != "Ollama":
        if not (conn.label.startswith("Ollama") and url.endswith("/v1")):
            raise InvalidSettingsError(f"{conn.label} is not an Ollama server: connect an Ollama Connector")
        url = url[:-3]  # (Ollama's OpenAI API: the server itself)
    return {**s, "url": url, "_key": srv["key"]}


def base_url(s: dict) -> str:
    return ((s.get("url") or "").strip() or setting("VERKNODE_LLM_URL") or DEFAULT_URL).rstrip("/")


def call(s: dict, method: str, path: str, body: dict | None = None, timeout: float = 60):
    url = base_url(s) + path
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if s.get("_key"):
        req.add_header("Authorization", f"Bearer {s['_key']}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(_error(e)) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Ollama at {base_url(s)}: {e.reason}") from None


def _error(e: urllib.error.HTTPError) -> str:
    text = e.read().decode("utf-8", "replace")
    try:
        return f"{e.code}: {json.loads(text).get('error', text)}"
    except ValueError:
        return f"{e.code}: {text[:200]}"


def _time(text):
    """Ollama's times (nanoseconds, any offset) as UTC datetimes."""
    if not text:
        return None
    m = re.match(r"(.*?\.\d{1,6})\d*(.*)$", text)
    t = (m.group(1) + m.group(2)) if m else text
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


SERVER_FIELD = F("server", "enum", "Server", DEFAULT, "Used when the LLM port is not connected. A saved Ollama server "
                 "(Settings › Node defaults › LLM servers), or Default: the URL below, else the default saved "
                 "server if it is an Ollama, else VERKNODE_LLM_URL.", options=[DEFAULT], section="Server")


def url_field():
    return F("url", "string", "Ollama URL", "", URL_HELP + " Used when the LLM port is not connected.", section="Server",
             show_if={"server": DEFAULT})


def model_fields(what="Models"):
    return [
        F("models", "string", what, "", "Comma separated. With an input table: also the models of its column."),
        F("column", "column", "Model column (input table)", None, "Empty: the column named Model, if any.",
          kinds=["string"]),
    ]


def wanted_models(df, s) -> list:
    names = [m.strip() for m in (s.get("models") or "").split(",") if m.strip()]
    if df is not None:
        col = s.get("column") or ("Model" if "Model" in df.columns else None)
        if col is None:
            col = next((c for c, t in df.schema.items() if dtype_kind(t) == "string"), None)
        if col is None or col not in df.columns:
            raise InvalidSettingsError("The input has no model column (a String column, e.g. Model)")
        names += [m for m in df[col].cast(pl.String).to_list() if m]
    seen, out = set(), []
    for m in names:
        if m not in seen:
            seen.add(m)
            out.append(m)
    if not out and df is None:
        raise InvalidSettingsError("No model: name some in Models, or connect a table with a model column")
    return out  # (an empty input table: nothing to do)


def show(s: dict, model: str) -> dict:
    return call(s, "POST", "/api/show", {"model": model})


def _context(info: dict):
    mi = info.get("model_info") or {}
    return next((v for k, v in mi.items() if k.endswith(".context_length")), None)


def suggest_models(s, keys=("models",)) -> dict:
    """Dialog data: the server's models offered for the model settings, and where the server is."""
    out = {"placeholder": {"url": setting("VERKNODE_LLM_URL") or DEFAULT_URL}}
    try:
        names = sorted(m["name"] for m in call(s, "GET", "/api/tags", timeout=3).get("models", []))
        out["suggest"] = {k: names for k in keys}
    except Exception as e:  # noqa: BLE001 - the dialog opens anyway
        out["error"] = str(e)
    return out


class _OllamaNode(NodeModel):
    """An Ollama node: its server from the LLM port, else its URL. (Name starts with "_": not a node.)"""

    abstract = True
    suggest_keys: tuple = ()  # (settings the dialog offers the server's models for)

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if "fields" in cls.__dict__:  # (the server last, under its heading: a saved server, else the URL)
            url = [f for f in cls.fields if f.key == "url"]
            cls.fields = [f for f in cls.fields if f.key not in ("url", "server")] + ([SERVER_FIELD, *url] if url else [])

    def dialog_data(self, inputs, s, connected=(), context=None):
        port = len(self.in_ports) - 1
        used = 0 <= port < len(connected) and connected[port]
        try:
            s = ollama_settings(s, inputs)
        except InvalidSettingsError as e:
            return {"error": str(e)}
        out = suggest_models(s, self.suggest_keys) if self.suggest_keys else {
            "placeholder": {"url": setting("VERKNODE_LLM_URL") or DEFAULT_URL}}
        names = [n for n, r in saved_servers().items() if (r.get("provider") or "Ollama") == "Ollama"]
        current = (s.get("server") or DEFAULT).strip()
        out["options"] = {"server": [DEFAULT, *names, *([current] if current not in (DEFAULT, *names) else [])]}
        if used:
            conn = connection_of(inputs)
            out["hide"] = ["server", "url"]
            out["notes"] = {"Server": f"From the LLM port: {conn.label if conn else 'the connector node (run it to see which)'}."}
        return out


def _source_configure(schema):
    return [pl.Schema(schema)]


# ----------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------
MODELS = {"Model": pl.String, "Family": pl.String, "Parameters": pl.String, "Parameters (billions)": pl.Float64,
          "Quantization": pl.String, "Format": pl.String, "Size (GB)": pl.Float64, "Modified": UTC, "Loaded": pl.Boolean}
DETAILS = {"Capabilities": pl.String, "Context length": pl.Int64}


def _billions(text):
    m = re.match(r"([\d.]+)\s*([KMBT])", text or "")
    if not m:
        return None
    return float(m.group(1)) * {"K": 1e-6, "M": 1e-3, "B": 1, "T": 1e3}[m.group(2)]


class OllamaModels(_OllamaNode):
    factory = "verknode.ollama.Models"
    name = "Ollama Models"
    category = CAT
    icon = "OL"
    node_type = "Source"
    short_description = "The models of an Ollama server."
    description = ("One row per model on the Ollama server: family, parameters, quantization, size on disk, when "
                   "it changed, and whether it is loaded. Details adds what each model can do (completion, vision, "
                   "tools, thinking, embedding) and its context length (one more request per model).")
    in_ports = [LLM_IN]
    out_ports = [Port(TABLE, "Models")]
    fields = [url_field(), F("details", "bool", "Details (capabilities, context length)", False)]

    def _schema(self, s):
        return {**MODELS, **(DETAILS if s.get("details") else {})}

    def configure(self, in_specs, s, ctx):
        return _source_configure(self._schema(s))

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        if ctx.dry:
            return [pl.DataFrame(schema=self._schema(s))]
        models = call(s, "GET", "/api/tags").get("models", [])
        loaded = {m.get("name") for m in call(s, "GET", "/api/ps").get("models", [])}
        rows = []
        for m in sorted(models, key=lambda m: m.get("name", "")):
            d = m.get("details") or {}
            row = {"Model": m.get("name"), "Family": d.get("family"), "Parameters": d.get("parameter_size"),
                   "Parameters (billions)": _billions(d.get("parameter_size")), "Quantization": d.get("quantization_level"),
                   "Format": d.get("format"), "Size (GB)": (m.get("size") or 0) / GB, "Modified": _time(m.get("modified_at")),
                   "Loaded": m.get("name") in loaded}
            if s.get("details"):
                ctx.check_canceled()
                info = show(s, m["name"])
                row["Capabilities"] = ", ".join(info.get("capabilities") or [])
                row["Context length"] = _context(info)
            rows.append(row)
        ctx.log(f"{len(rows)} model(s) on {base_url(s)}")
        return [pl.DataFrame(rows, schema=self._schema(s))]


RUNNING = {"Model": pl.String, "Size (GB)": pl.Float64, "GPU memory (GB)": pl.Float64, "CPU memory (GB)": pl.Float64,
           "On GPU (%)": pl.Float64, "Context length": pl.Int64, "Unloads at": UTC, "Unloads in (min)": pl.Float64}


class OllamaRunning(_OllamaNode):
    factory = "verknode.ollama.Running"
    name = "Ollama Running Models"
    category = CAT
    icon = "OL"
    node_type = "Source"
    short_description = "The loaded models and their GPU memory."
    description = ("One row per model loaded by the Ollama server: its size in memory, how much of it is in GPU memory "
                   "(VRAM) and how much in CPU memory, the share on the GPU, its context length and when it unloads. "
                   "Pushes the flow variables ollama_gpu_memory_gb, ollama_cpu_memory_gb and ollama_loaded_models. "
                   "(Ollama reports the memory of its models, not the use of the GPU as a whole.)")
    in_ports = [LLM_IN]
    out_ports = [Port(TABLE, "Loaded models")]
    fields = [url_field()]

    def configure(self, in_specs, s, ctx):
        return _source_configure(RUNNING)

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        if ctx.dry:
            return [pl.DataFrame(schema=RUNNING)]
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for m in call(s, "GET", "/api/ps").get("models", []):
            size, vram = (m.get("size") or 0), (m.get("size_vram") or 0)
            until = _time(m.get("expires_at"))
            if until is not None and until < now:
                continue  # (being unloaded)
            rows.append({"Model": m.get("name"), "Size (GB)": size / GB, "GPU memory (GB)": vram / GB,
                         "CPU memory (GB)": max(size - vram, 0) / GB, "On GPU (%)": 100 * vram / size if size else None,
                         "Context length": m.get("context_length"), "Unloads at": until,
                         "Unloads in (min)": (until - now).total_seconds() / 60 if until else None})
        df = pl.DataFrame(rows, schema=RUNNING)
        ctx.push_variable("ollama_gpu_memory_gb", float(df["GPU memory (GB)"].sum() or 0), "DOUBLE")
        ctx.push_variable("ollama_cpu_memory_gb", float(df["CPU memory (GB)"].sum() or 0), "DOUBLE")
        ctx.push_variable("ollama_loaded_models", df.height, "INTEGER")
        return [df]


INFO = {"Model": pl.String, "Capabilities": pl.String, "Context length": pl.Int64, "Embedding length": pl.Int64,
        "Parameter count": pl.Int64, "Family": pl.String, "Quantization": pl.String, "Parameters": pl.String,
        "System prompt": pl.String, "License": pl.String, "Error": pl.String}


class OllamaModelInfo(_OllamaNode):
    factory = "verknode.ollama.ModelInfo"
    name = "Ollama Model Info"
    category = CAT
    icon = "OL"
    node_type = "Manipulator"
    short_description = "What Ollama models can do, and how they are set up."
    description = ("One row per model: its capabilities (completion, vision, tools, thinking, embedding), context and "
                   "embedding length, parameter count, family, quantization, default parameters (temperature, ...), "
                   "system prompt and the first line of its license. Models come from the setting and / or a column "
                   "of the input table (e.g. of Ollama Models).")
    in_ports = [Port(TABLE, "Models", "A table with a model column (optional)", optional=True), LLM_IN]
    out_ports = [Port(TABLE, "Model info")]
    fields = [url_field(), *model_fields()]

    def configure(self, in_specs, s, ctx):
        return _source_configure(INFO)

    suggest_keys = ("models",)

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        if ctx.dry:
            return [pl.DataFrame(schema=INFO)]
        rows = []
        for model in wanted_models(inputs[0], s):
            ctx.check_canceled()
            try:
                info = show(s, model)
            except RuntimeError as e:
                rows.append({"Model": model, "Error": str(e)})
                continue
            mi, d = info.get("model_info") or {}, info.get("details") or {}
            modelfile = info.get("modelfile") or ""
            system = (re.search(r'^SYSTEM\s+"""(.*?)"""', modelfile, re.M | re.S)
                      or re.search(r"^SYSTEM\s+(.+)$", modelfile, re.M))
            rows.append({
                "Model": model, "Capabilities": ", ".join(info.get("capabilities") or []), "Context length": _context(info),
                "Embedding length": next((v for k, v in mi.items() if k.endswith(".embedding_length")), None),
                "Parameter count": mi.get("general.parameter_count"), "Family": d.get("family"),
                "Quantization": d.get("quantization_level"),
                "Parameters": "; ".join(" ".join(line.split()) for line in (info.get("parameters") or "").splitlines() if line.strip()),
                "System prompt": system.group(1).strip() if system else None,
                "License": next((line.strip() for line in (info.get("license") or "").splitlines() if line.strip()), None),
            })
        return [pl.DataFrame(rows, schema=INFO)]


# ----------------------------------------------------------------------
# changing what is loaded, and what is on the server
# ----------------------------------------------------------------------
RESULT = {"Model": pl.String, "Action": pl.String, "Status": pl.String, "Seconds": pl.Float64}
APPLY_HELP = "Off: only lists what would be done (Status: not done). On: changes the Ollama server."


def _result(model, action, status, seconds=None):
    return {"Model": model, "Action": action, "Status": status, "Seconds": seconds}


class _Action(_OllamaNode):
    abstract = True
    icon = "OL"
    sandbox_unsafe = True  # (changes the Ollama server)
    node_type = "Manipulator"
    in_ports = [Port(TABLE, "Models", "A table with a model column (optional)", optional=True), LLM_IN]
    out_ports = [Port(TABLE, "Results", "Model, action, status (OK or the error) and the time it took")]

    def configure(self, in_specs, s, ctx):
        return _source_configure(RESULT)

    suggest_keys = ("models", "base")

    action = ""  # (what the Action column says)

    def run(self, model, s, ctx, i, n) -> str:
        """Do it for model i of n; the status (None: OK). Raise RuntimeError when it fails."""
        raise NotImplementedError

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        if ctx.dry:
            return [pl.DataFrame(schema=RESULT)]
        models = wanted_models(inputs[0], s)
        rows, failed = [], 0
        for i, model in enumerate(models):
            ctx.check_canceled()
            if "apply" in s and not s["apply"]:
                rows.append(_result(model, self.action, "not done (Make the change is off)"))
                continue
            t0 = time.time()
            try:
                status = self.run(model, s, ctx, i, len(models)) or "OK"
            except RuntimeError as e:
                status, failed = str(e), failed + 1
            rows.append(_result(model, self.action, status, time.time() - t0))
            ctx.log(f"{self.action} {model}: {status}")
        if failed:
            ctx.warn(f"{failed} of {len(models)} failed (see Status)")
        return [pl.DataFrame(rows, schema=RESULT)]


class OllamaLoad(_Action):
    factory = "verknode.ollama.Load"
    name = "Ollama Load / Unload"
    category = CAT
    short_description = "Loads models into memory, or unloads them."
    description = ("Loads the models (so the first answer comes quickly) and keeps them loaded for Keep loaded, or "
                   "unloads them to free GPU memory. Run Ollama Running Models after it to see the memory they use.")
    fields = [url_field(), *model_fields(),
              F("mode", "enum", "Action", "Load", options=["Load", "Unload"]),
              F("keep_alive", "string", "Keep loaded", "30m", "How long a loaded model stays in memory without "
                "requests: e.g. 10m, 2h; -1: until unloaded.", show_if={"mode": "Load"})]

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        self.action = s.get("mode") or "Load"
        return super().execute(inputs, s, ctx)

    def run(self, model, s, ctx, i, n):
        unload = s.get("mode") == "Unload"
        keep = 0 if unload else _keep_alive(s.get("keep_alive"))
        try:
            call(s, "POST", "/api/generate", {"model": model, "keep_alive": keep}, timeout=600)
        except RuntimeError as e:
            if "does not support generate" not in str(e):
                raise
            call(s, "POST", "/api/embed", {"model": model, "input": "", "keep_alive": keep}, timeout=600)
        ctx.set_progress((i + 1) / n)
        return "unloaded" if unload else "loaded"


def _keep_alive(text):
    text = (text or "").strip() or "30m"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if not re.fullmatch(r"\d+(\.\d+)?(ms|s|m|h)", text):
        raise InvalidSettingsError(f'Keep loaded: e.g. 10m, 2h or -1 (not "{text}")')
    return text


class OllamaPull(_Action):
    factory = "verknode.ollama.Pull"
    name = "Ollama Download Model"
    category = CAT
    action = "download"
    short_description = "Downloads models to the Ollama server."
    description = ("Downloads (pulls) the models from the Ollama library, e.g. llama3.2 or qwen3:8b, with the progress "
                   "on the node. Models already there are brought up to date. " + APPLY_HELP)
    fields = [url_field(), *model_fields("Models to download"),
              F("apply", "bool", "Make the change", True, APPLY_HELP)]

    def run(self, model, s, ctx, i, n):
        req = urllib.request.Request(base_url(s) + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     method="POST")
        last = ""
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                for line in r:
                    ctx.check_canceled()
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])
                    last = msg.get("status", last)
                    if msg.get("total"):
                        ctx.set_progress((i + (msg.get("completed") or 0) / msg["total"]) / n)
        except urllib.error.HTTPError as e:
            raise RuntimeError(_error(e)) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach Ollama at {base_url(s)}: {e.reason}") from None
        return "OK" if last == "success" else last


class OllamaCreate(_Action):
    factory = "verknode.ollama.Create"
    name = "Ollama Create Model"
    category = CAT
    action = "create"
    short_description = "Makes a model of your own from another one."
    description = ("Creates a model (e.g. my-assistant) from a base model with a system prompt and parameters of its "
                   "own - like a Modelfile with FROM, SYSTEM and PARAMETER. It shares the base model's weights. "
                   + APPLY_HELP)
    in_ports = [LLM_IN]
    fields = [url_field(),
              F("models", "string", "New model", "my-assistant"),
              F("base", "string", "From (base model)", "", "A model of the server, e.g. llama3.2."),
              F("system", "code", "System prompt", "You are a helpful assistant."),
              F("parameters", "records", "Parameters", [], "e.g. temperature 0.2, num_ctx 8192.", fields=[
                  F("name", "string", "Parameter", "temperature"), F("value", "string", "Value", "0.2")]),
              F("apply", "bool", "Make the change", True, APPLY_HELP)]

    def execute(self, inputs, s, ctx):
        s = ollama_settings(s, inputs)
        if not (s.get("base") or "").strip():
            raise InvalidSettingsError("Set From (base model)")
        return super().execute([None, *inputs], s, ctx)

    def run(self, model, s, ctx, i, n):
        params = {}
        for p in s.get("parameters") or []:
            name, value = (p.get("name") or "").strip(), (p.get("value") or "").strip()
            if name:
                try:
                    params[name] = json.loads(value)
                except ValueError:
                    params[name] = value
        body = {"model": model, "from": s["base"].strip(), "stream": False}
        if (s.get("system") or "").strip():
            body["system"] = s["system"].strip()
        if params:
            body["parameters"] = params
        r = call(s, "POST", "/api/create", body, timeout=600)
        return "OK" if r.get("status", "success") == "success" else r.get("status")


class OllamaDelete(_Action):
    factory = "verknode.ollama.Delete"
    name = "Ollama Delete Model"
    category = CAT
    action = "delete"
    short_description = "Deletes models from the Ollama server."
    description = ("Deletes the models from the Ollama server (their files; layers other models share stay). "
                   "Make the change is off at first: turn it on to delete. " + APPLY_HELP)
    fields = [url_field(), *model_fields("Models to delete"),
              F("apply", "bool", "Make the change", False, APPLY_HELP)]

    def run(self, model, s, ctx, i, n):
        call(s, "DELETE", "/api/delete", {"model": model})
        return "deleted"
