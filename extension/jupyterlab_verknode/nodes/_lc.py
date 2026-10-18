"""LangChain for the agent nodes: a chat model that calls verknode's chat() / chat_tools() - so
LangGraph and Deep Agents run on any model connection (Ollama, inference servers, APIs, Anthropic,
llama.cpp in-process) - and tools made of Python functions with JSON-schema arguments.

(A module of helpers, not of nodes: its name starts with "_". It needs langchain-core, which comes
with langgraph and deepagents.)
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from .llm import chat, chat_tools, tool_message


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # (content blocks)
        return "".join(b if isinstance(b, str) else (b.get("text") or "") for b in content
                       if isinstance(b, str) or b.get("type") in (None, "text"))
    return str(content or "")


def to_messages(srv: dict, messages: list[BaseMessage]) -> list[dict]:
    """LangChain messages as verknode's (OpenAI form; Ollama's own for tool calls and results)."""
    out, names = [], {}
    for m in messages:
        kind = m.type
        if kind == "system":
            out.append({"role": "system", "content": _text(m.content)})
        elif kind == "human":
            out.append({"role": "user", "content": _text(m.content)})
        elif kind == "ai":
            msg = {"role": "assistant", "content": _text(m.content)}
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                names.update({c["id"]: c["name"] for c in calls})
                if srv["provider"] == "Ollama":
                    msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("args") or {}}} for c in calls]
                else:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}} for c in calls]
            out.append(msg)
        elif kind == "tool":
            call = {"id": m.tool_call_id, "name": m.name or names.get(m.tool_call_id) or "tool"}
            out.append(tool_message(srv, call, _text(m.content)))
    return out


class VerknodeChatModel(BaseChatModel):
    """A LangChain chat model on a verknode model connection (server dict) and generation settings."""

    srv: dict
    settings: dict = {}
    tool_specs: list = []
    usage: dict = {}  # (shared by its copies: tokens and calls of a run)

    @property
    def _llm_type(self) -> str:
        return "verknode"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ARG002 - (the model chooses)
        specs = []
        for t in tools:
            f = convert_to_openai_tool(t)["function"]
            specs.append({"name": f["name"], "description": f.get("description") or "",
                          "parameters": f.get("parameters") or {"type": "object", "properties": {}}})
        return self.model_copy(update={"tool_specs": specs})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        msgs = to_messages(self.srv, messages)
        if self.tool_specs:
            r = chat_tools(self.srv, msgs, self.settings, self.tool_specs)
        else:
            r = {**chat(self.srv, msgs, self.settings), "calls": []}
        u = self.usage
        u["calls"] = u.get("calls", 0) + 1
        u["tokens_in"] = u.get("tokens_in", 0) + int(r.get("tokens_in") or 0)
        u["tokens_out"] = u.get("tokens_out", 0) + int(r.get("tokens_out") or 0)
        ai = AIMessage(content=r.get("text") or "",
                       tool_calls=[{"id": c["id"], "name": c["name"], "args": c["args"], "type": "tool_call"} for c in r["calls"]])
        return ChatResult(generations=[ChatGeneration(message=ai)])


def make_tool(name: str, description: str, parameters: dict, call) -> StructuredTool:
    """A LangChain tool: call(name, args) -> text."""
    schema = parameters if parameters.get("properties") else {"type": "object", "properties": {}}
    return StructuredTool(name=name, description=description, args_schema=schema, func=lambda **kw: call(name, kw))


def message_log(messages: list) -> list[dict]:
    """A short log of a LangGraph run: role, text, tool calls."""
    out = []
    for m in messages:
        e: dict[str, Any] = {"role": m.type, "content": _text(m.content)[:2000]}
        if getattr(m, "tool_calls", None):
            e["calls"] = [{"name": c["name"], "args": c.get("args")} for c in m.tool_calls]
        if isinstance(m, ToolMessage):
            e["tool"] = m.name
        out.append(e)
    return out
