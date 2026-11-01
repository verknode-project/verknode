"""LLM connector nodes: where a language model is and which one, as a connection on an LLM port
(purple square) for the LLM nodes - instead of each node's Model settings.

- Ollama: the local model runner (or an Ollama server elsewhere).
- Inference Server: an OpenAI-compatible inference server or engine - vLLM, SGLang, llama.cpp's
  llama-server, LM Studio, Hugging Face TGI, LocalAI, llamafile, Jan.
- LLM API: commercial APIs that speak OpenAI's chat API - OpenAI, Google Gemini, Mistral, Groq,
  OpenRouter, Together AI, DeepSeek, xAI, Fireworks.
- Anthropic (Claude): Anthropic's Messages API.
- Local Model (llama.cpp): a GGUF model file run in this process (llama-cpp-python), downloaded
  from Hugging Face if wanted.
- Saved LLM Server: one of the servers saved in Settings › Node defaults › LLM servers.

Keys are never stored: a connection names the environment variable (or the
~/.verknode/llm.env setting) that holds the key, and it is read when a model is called.
"""
from __future__ import annotations

import os
import time
import urllib.request

from verknode.node import F, InvalidSettingsError, LLM, LLMConnectionPortObject, NodeModel, Port

from jupyterlab_verknode.engine.home import LEGACY_CACHE, user_path

from .llm import ANTHROPIC, DEFAULT_URL, LOCAL, default_server_name, list_models, saved_server, saved_servers, setting

CAT = "AI/LLM/Connect"
OUT = [Port(LLM, "LLM", "The model connection: for the LLM ports of LLM, agent and evaluation nodes")]

SERVERS = {  # name: default URL
    "vLLM": "http://localhost:8000/v1",
    "SGLang": "http://localhost:30000/v1",
    "llama.cpp server (llama-server)": "http://localhost:8080/v1",
    "LM Studio": "http://localhost:1234/v1",
    "Hugging Face TGI": "http://localhost:8080/v1",
    "LocalAI": "http://localhost:8080/v1",
    "llamafile": "http://localhost:8080/v1",
    "Jan": "http://localhost:1337/v1",
    "Ollama (OpenAI API)": "http://localhost:11434/v1",
    "Other": "",
}

APIS = {  # name: (base URL, key variable)
    "OpenAI": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "Mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "Groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "Together AI": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "DeepSeek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xAI": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "Fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
}

KEY_HELP = ("The environment variable of the Jupyter server (or setting of ~/.verknode/llm.env) with the "
            "key. Keys are never stored in the workflow.")


def option_fields():
    return [
        F("temperature", "string", "Temperature for all its nodes", "",
          "Empty: each node's own. A number: this connection's nodes use it (e.g. an arm of an evaluation)."),
        F("check", "bool", "Check the connection when it runs", True,
          "Ask the server for its models: a wrong address, key or model fails here, not in the nodes after it."),
    ]


def _t(s) -> str:
    """The label's temperature, if the connection sets one."""
    t = str(s.get("temperature") or "").strip()
    return f" · T{t}" if t else ""


def _options(s) -> dict:
    t = str(s.get("temperature") or "").strip()
    if not t:
        return {}
    try:
        return {"temperature": float(t)}
    except ValueError:
        raise InvalidSettingsError(f"Temperature: a number, not {t!r}") from None


class _Connector(NodeModel):
    """A connector: connection(s) -> LLMConnectionPortObject; running checks it. (Its name starts with
    "_": not a node itself.)"""

    category = CAT
    node_type = "Source"
    in_ports: list = []
    out_ports = OUT

    def connection(self, s) -> LLMConnectionPortObject:
        raise NotImplementedError

    def configure(self, in_specs, s, ctx):
        return [self.connection(s)]

    def dialog_data(self, inputs, s, connected=(), context=None):
        out = {}
        try:
            out["suggest"] = {"model": list_models(self.connection(s).server(s), timeout=4)}
        except Exception as e:  # noqa: BLE001 - the dialog opens anyway
            out["error"] = str(e)
        return out

    def check(self, conn: LLMConnectionPortObject, ctx):
        srv = conn.server()
        if conn.key_env and not srv["key"]:
            raise InvalidSettingsError(f"No API key: set {conn.key_env} in the environment of the Jupyter server "
                                       f"(or {conn.key_env}=... in ~/.verknode/llm.env)")
        try:
            models = list_models(srv, timeout=15)
        except RuntimeError as e:
            raise InvalidSettingsError(str(e)) from None
        if conn.model and models and conn.model not in models and f"{conn.model}:latest" not in models:
            raise InvalidSettingsError(f"{conn.label}: there is no model {conn.model!r} - there are "
                                       + ", ".join(models[:20]) + ("…" if len(models) > 20 else ""))
        ctx.log(f"{conn.label}: {len(models)} model(s) there")

    def execute(self, inputs, s, ctx):
        conn = self.connection(s)
        if not conn.model:
            raise InvalidSettingsError("Choose a model (the dialog lists the server's models)")
        if s.get("check", True) and not ctx.dry:
            self.check(conn, ctx)
        return [conn]


class OllamaConnector(_Connector):
    factory = "verknode.llm.OllamaConnector"
    name = "Ollama Connector"
    icon = "🦙"
    short_description = "A model of Ollama, the local model runner (on this machine or a server)."
    description = (
        "Connects to an Ollama server - the default runs on this machine at http://localhost:11434 - and a model "
        "of it (the dialog lists them). Connect its LLM port to LLM, agent and evaluation nodes: they then use "
        "this model instead of their own Model settings. Ollama Models / Pull Model manage the models."
    )
    fields = [
        F("url", "string", "Ollama URL", "", "Empty: VERKNODE_LLM_URL of the Jupyter server, else " + DEFAULT_URL),
        F("model", "string", "Model", "", "e.g. gemma4:26b, qwen2.5-coder:32b. Empty: VERKNODE_LLM_MODEL."),
        F("api_key_env", "string", "API key variable", "", "For an Ollama behind a proxy that wants a key. " + KEY_HELP),
        *option_fields(),
    ]

    def connection(self, s):
        url = (s.get("url") or "").strip() or ("${VERKNODE_LLM_URL}" if setting("VERKNODE_LLM_URL") else DEFAULT_URL)
        if "/v1" in url.rstrip("/")[-3:]:
            raise InvalidSettingsError("That is Ollama's OpenAI API (/v1): use Inference Server, or leave /v1 out")
        model = (s.get("model") or "").strip() or setting("VERKNODE_LLM_MODEL")
        return LLMConnectionPortObject("Ollama", url, model, (s.get("api_key_env") or "").strip(),
                                       f"Ollama · {model}{_t(s)}", _options(s))


class InferenceServerConnector(_Connector):
    factory = "verknode.llm.InferenceServerConnector"
    name = "Inference Server Connector"
    icon = "⚙AI"
    short_description = "An OpenAI-compatible inference server or engine: vLLM, SGLang, llama-server, LM Studio, TGI..."
    description = (
        "Connects to a model served with OpenAI's chat API (/v1/chat/completions): inference engines such as vLLM "
        "and SGLang, llama.cpp's llama-server, LM Studio, Hugging Face Text Generation Inference, LocalAI, "
        "llamafile, Jan - or Ollama's OpenAI API. The server presets fill in their usual address; the dialog lists "
        "the served models. Tool calls need a server and model that support them (vLLM: --enable-auto-tool-choice)."
    )
    fields = [
        F("server", "enum", "Server", "vLLM", options=list(SERVERS)),
        F("url", "string", "URL", "", "Empty: the preset's (e.g. http://localhost:8000/v1 for vLLM; Ollama: VERKNODE_LLM_URL/v1)."),
        F("model", "string", "Model", "", "The served model's name (the dialog lists them)."),
        F("api_key_env", "string", "API key variable", "", "If the server wants a key (vLLM --api-key). " + KEY_HELP),
        *option_fields(),
    ]

    def connection(self, s):
        name = s.get("server") or "vLLM"
        url = (s.get("url") or "").strip()
        if not url and name.startswith("Ollama") and setting("VERKNODE_LLM_URL"):
            url = "${VERKNODE_LLM_URL}/v1"  # (the Ollama the server is set up with, read when it is used)
        url = url or SERVERS.get(name, "")
        if not url:
            raise InvalidSettingsError("Give the server's URL (e.g. http://localhost:8000/v1)")
        model = (s.get("model") or "").strip()
        opts = _options(s)
        if name.startswith("Ollama"):  # (its thinking models think unless told not to)
            opts["extra_body"] = {"reasoning_effort": "none"}
        return LLMConnectionPortObject("OpenAI-compatible", url, model, (s.get("api_key_env") or "").strip(),
                                       f"{name} · {model}{_t(s)}", opts)


class LLMAPIConnector(_Connector):
    factory = "verknode.llm.LLMAPIConnector"
    name = "LLM API Connector"
    icon = "API"
    short_description = "A commercial LLM API: OpenAI, Google Gemini, Mistral, Groq, OpenRouter, Together, DeepSeek, xAI..."
    description = (
        "Connects to a model of a commercial API that speaks OpenAI's chat API: OpenAI, Google Gemini (its OpenAI "
        "endpoint), Mistral, Groq, OpenRouter, Together AI, DeepSeek, xAI, Fireworks. The key comes from an "
        "environment variable of the Jupyter server (OPENAI_API_KEY, GEMINI_API_KEY, ...), never from the "
        "workflow. Requests are billed by the provider. For Claude, use Anthropic Connector."
    )
    fields = [
        F("provider", "enum", "Provider", "OpenAI", options=list(APIS)),
        F("model", "string", "Model", "", "The dialog lists the provider's models (once the key is set)."),
        F("api_key_env", "string", "API key variable", "", "Empty: the provider's usual one (OPENAI_API_KEY, ...). " + KEY_HELP),
        F("url", "string", "Base URL", "", "Empty: the provider's (e.g. a proxy or a region instead)."),
        *option_fields(),
    ]

    def connection(self, s):
        name = s.get("provider") or "OpenAI"
        base, key = APIS.get(name, APIS["OpenAI"])
        model = (s.get("model") or "").strip()
        return LLMConnectionPortObject("OpenAI-compatible", (s.get("url") or "").strip() or base, model,
                                       (s.get("api_key_env") or "").strip() or key, f"{name} · {model}{_t(s)}", _options(s))


class AnthropicConnector(_Connector):
    factory = "verknode.llm.AnthropicConnector"
    name = "Anthropic Connector"
    icon = "A\\"
    short_description = "A Claude model of Anthropic's API (Messages API, with tool use)."
    description = (
        "Connects to a Claude model through Anthropic's Messages API - with tool use, so agents and the workflow "
        "agent work with it. The key comes from ANTHROPIC_API_KEY (or the variable you name) of the Jupyter "
        "server, never from the workflow. Requests are billed by Anthropic."
    )
    fields = [
        F("model", "string", "Model", "claude-sonnet-5-5", "e.g. claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5."),
        F("api_key_env", "string", "API key variable", "ANTHROPIC_API_KEY", KEY_HELP),
        F("url", "string", "Base URL", "", "Empty: https://api.anthropic.com"),
        *option_fields(),
    ]

    def connection(self, s):
        model = (s.get("model") or "").strip()
        return LLMConnectionPortObject(ANTHROPIC, (s.get("url") or "").strip() or "https://api.anthropic.com", model,
                                       (s.get("api_key_env") or "").strip() or "ANTHROPIC_API_KEY",
                                       f"Anthropic · {model}{_t(s)}", _options(s))


class SavedServerConnector(_Connector):
    factory = "verknode.llm.SavedServerConnector"
    name = "Saved LLM Server"
    icon = "★AI"
    short_description = "One of your saved LLM servers (Settings › Node defaults › LLM servers), by name."
    description = (
        "Passes a server you saved once in Settings › Node defaults › LLM servers - Ollama, an OpenAI-compatible "
        "server or Anthropic, with its address, model and key variable - to the LLM ports of other nodes. The "
        "workflow keeps only the server's name: its address is read from your node defaults when a model is "
        "called, so the workflow runs on another machine whose saved server of that name points elsewhere."
    )
    fields = [
        F("server", "enum", "Server", "", "Empty: the default saved server.", options=[""]),
        F("model", "string", "Model", "", "Empty: the saved server's model."),
        *option_fields(),
    ]

    def connection(self, s):
        name = (s.get("server") or "").strip() or default_server_name()
        if not name:
            raise InvalidSettingsError("No saved LLM server: add one in Settings › Node defaults › LLM servers")
        srv = saved_server(name, (s.get("model") or "").strip())
        return LLMConnectionPortObject(srv["provider"], "${saved:%s}" % name, srv["model"], srv["key_env"],
                                       f"{name} · {srv['model']}{_t(s)}", _options(s))

    def dialog_data(self, inputs, s, connected=(), context=None):
        names = list(saved_servers())
        current = (s.get("server") or "").strip()
        out = super().dialog_data(inputs, s, connected, context) if (names or current) else {
            "error": "No saved LLM servers yet: add them in Settings › Node defaults › LLM servers"}
        out["options"] = {"server": ["", *names, *([current] if current and current not in names else [])]}
        return out


MODELS_DIR = "~/.verknode/models"  # (shown; engine/home.py: the folder)
LEGACY_MODELS = os.path.join(LEGACY_CACHE, "models")


class LocalModelConnector(_Connector):
    factory = "verknode.llm.LocalModelConnector"
    name = "Local Model (llama.cpp)"
    icon = "GGUF"
    requires = ["llama-cpp-python"]
    short_description = "A GGUF model file run in this process with llama.cpp - downloaded from Hugging Face if wanted."
    description = (
        "Runs a model in the Jupyter server's process with llama.cpp (llama-cpp-python): no server needed. The "
        "model is a GGUF file - one of yours, or downloaded once from a Hugging Face repository into the models "
        "folder (e.g. Qwen/Qwen2.5-1.5B-Instruct-GGUF, qwen2.5-1.5b-instruct-q4_k_m.gguf). Tool calls work by "
        "constrained JSON output (a grammar), so agents work with small models too - slowly on a CPU. Models stay "
        "loaded while the server runs; one request at a time per model."
    )
    fields = [
        F("source", "enum", "Model", "Download from Hugging Face", options=["Download from Hugging Face", "Model file"]),
        F("repo", "string", "Hugging Face repository", "Qwen/Qwen2.5-1.5B-Instruct-GGUF"),
        F("file", "string", "File in the repository", "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        F("folder", "string", "Models folder", MODELS_DIR, "Where downloads go.", path="folder"),
        F("path", "string", "Model file (GGUF)", "", "Model file: its path.", path=True, extensions=[".gguf"]),
        F("n_ctx", "int", "Context length (tokens)", 8192),
        F("n_gpu_layers", "int", "Layers on the GPU", 0, "0: CPU only; -1: all (a llama-cpp-python built with GPU support)."),
        F("n_threads", "int", "CPU threads", 0, "0: llama.cpp's choice."),
        *option_fields(),
    ]

    def _path(self, s, ctx=None) -> str:
        if (s.get("source") or "") == "Model file":
            p = (s.get("path") or "").strip()
            if not p:
                raise InvalidSettingsError("Give the model file (.gguf)")
            return ctx.resolve_path(p) if ctx is not None else os.path.expanduser(p)
        repo, file = (s.get("repo") or "").strip(), (s.get("file") or "").strip()
        if not repo or not file:
            raise InvalidSettingsError("Give the Hugging Face repository and the GGUF file in it")
        folder = (s.get("folder") or "").strip()
        rel = os.path.join(repo.replace("/", "__"), file)
        if not folder or folder == MODELS_DIR:  # (the verknode folder's models; a file downloaded before: there)
            return user_path("models", rel, legacy=os.path.join(LEGACY_MODELS, rel))
        return os.path.join(os.path.expanduser(folder), rel)

    def connection(self, s, ctx=None):
        path = self._path(s, ctx)
        opts = {**_options(s), "n_ctx": int(s.get("n_ctx") or 8192), "n_gpu_layers": int(s.get("n_gpu_layers") or 0),
                "n_threads": int(s.get("n_threads") or 0)}
        return LLMConnectionPortObject(LOCAL, path, os.path.basename(path), "", f"llama.cpp · {os.path.basename(path)}", opts)

    def configure(self, in_specs, s, ctx):
        return [self.connection(s, ctx)]

    def dialog_data(self, inputs, s, connected=(), context=None):
        return {}

    def _download(self, s, path, ctx):
        from ._http import user_agent

        url = f"https://huggingface.co/{s['repo'].strip()}/resolve/main/{s['file'].strip()}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = path + ".part"
        ctx.log(f"Downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
        t0, got = time.time(), 0
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(1 << 20):
                ctx.check_canceled()
                f.write(chunk)
                got += len(chunk)
                if total:
                    ctx.set_progress(got / total)
        os.replace(part, path)
        ctx.log(f"Downloaded {got / 1e6:.0f} MB in {time.time() - t0:.0f} s to {path}")

    def execute(self, inputs, s, ctx):
        conn = self.connection(s, ctx)
        if ctx.dry:
            return [conn]
        if not os.path.isfile(conn.url):
            if (s.get("source") or "") == "Model file":
                raise InvalidSettingsError(f"There is no model file {conn.url}")
            try:
                self._download(s, conn.url, ctx)
            except OSError as e:
                raise InvalidSettingsError(f"The download failed: {e}") from None
        if s.get("check", True):
            from .llm import _local_model

            t0 = time.time()
            _local_model(conn.server())
            ctx.log(f"Loaded {conn.model} in {time.time() - t0:.1f} s")
        return [conn]
