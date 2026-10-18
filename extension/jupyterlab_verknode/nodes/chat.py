"""Chat in a loop: the conversation goes round a Recursive Loop, growing by a message and an answer
each time:

    conversation -> Recursive Loop Start -> Chat (waits for your message) -> LLM Chat (the answer)
                         ^                                          |              |
                         +--- Recursive Loop End <- Concatenate <----+--------------+

The Chat widget shows the conversation on the canvas and waits, while the loop runs, for the next
message typed into it; End chat stops the loop (its end variable), Cancel too. Chat Loop Start is
the Recursive Loop Start and the Chat in one node:

    Chat Loop Start -> LLM Chat -> Concatenate -> Recursive Loop End
"""
from __future__ import annotations

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE
from jupyterlab_verknode.engine.widgets import OutputWidget

from .flow_extra import RecursiveLoopStart, _state
from .llm import chat, server, server_fields

ROLES = ("user", "assistant", "system")


def _conversation(df: pl.DataFrame) -> list:
    """The rows as messages, with the model and the time of answers (meta) when there."""
    out = []
    for r in df.iter_rows(named=True):
        if r.get("role") not in ROLES:
            continue
        m = {"role": r["role"], "content": r.get("content") or ""}
        meta = " · ".join(p for p in (r.get("model"), f"{r['seconds']:.1f} s" if r.get("seconds") is not None else None) if p)
        if meta and r["role"] == "assistant":
            m["meta"] = meta
        if r.get("confirm") and r["role"] == "assistant":
            m["confirm"] = True  # (e.g. a Workflow Agent waits for an OK: the chat offers Yes / No)
        out.append(m)
    return out


CHAT_FIELDS = [
    F("placeholder", "string", "Placeholder", "Type a message…"),
    F("end_variable", "string", "End variable", "chat_end",
      "A Boolean flow variable: true when End chat is clicked (for the Recursive Loop End)."),
]


def chat_turn(df: pl.DataFrame, s: dict, ctx) -> tuple[pl.DataFrame, bool]:
    """Show the conversation, wait for the next message typed into the widget, and return
    (the conversation with it last, False) - or (the conversation, True) for End chat."""
    end_var = (s.get("end_variable") or "chat_end").strip()
    ctx.push_variable(end_var, False, "BOOLEAN")
    if ctx.dry:
        return df, False
    msgs = _conversation(df)
    base = {"placeholder": s.get("placeholder") or "", "title": s.get("title") or ""}
    ctx.set_display({**base, "messages": msgs, "waiting": True})
    got = ctx.wait_for_input() or {}
    if got.get("end"):
        ctx.set_display({**base, "messages": msgs, "waiting": False, "ended": True})
        ctx.push_variable(end_var, True, "BOOLEAN")
        ctx.log("Chat ended")
        return df, True
    text = str(got.get("text") or "").strip()
    ctx.set_display({**base, "messages": msgs + [{"role": "user", "content": text}], "waiting": False})
    ctx.push_variable("message", text, "STRING")
    return pl.concat([df, pl.DataFrame({"role": ["user"], "content": [text]})], how="diagonal_relaxed"), False


def _check_conversation(spec):
    missing = [c for c in ("role", "content") if c not in spec]
    if missing:
        raise InvalidSettingsError(f"The conversation needs the columns role and content (missing: {', '.join(missing)})")


class Chat(OutputWidget):
    factory = "verknode.widgets.Chat"
    sandbox_unsafe = True  # (waits for the canvas: not in a test run)
    needs_canvas = True  # (not in headless runs: export)
    name = "Chat"
    icon = "💬"
    renderer = "chat"
    width, height = 380, 460
    short_description = "A chat on the canvas, in a loop: shows the conversation, waits for your message."
    description = (
        "Put it in a Recursive Loop that carries the conversation (columns role and content): each time round "
        "it shows the conversation, waits for the message you type into it (Enter sends) and outputs the "
        "conversation with your message last - for an LLM Chat, whose answer is added (Concatenate) before the "
        "conversation goes back to the loop start. So the model sees all that was said. End chat sets the end "
        "variable (for the Recursive Loop End's End loop with variable); Cancel stops it too. The message is "
        "also pushed as the flow variable message. Start the chat by executing the loop (or Start chat on it)."
    )
    in_ports = [Port(TABLE, "Conversation", "role and content: the conversation so far")]
    out_ports = [Port(TABLE, "Conversation", "With your new message last")]
    widget_fields = CHAT_FIELDS

    def configure(self, in_specs, s, ctx):
        _check_conversation(in_specs[0])
        return [in_specs[0]]

    def execute(self, inputs, s, ctx):
        return [chat_turn(inputs[0], s, ctx)[0]]


CONVERSATION = {"role": pl.String, "content": pl.String}


class ChatLoopStart(RecursiveLoopStart):
    factory = "verknode.flow.ChatLoopStart"
    closed_by = ["verknode.flow.RecursiveLoopEnd"]
    sandbox_unsafe = True  # (waits for the canvas: not in a test run)
    needs_canvas = True  # (not in headless runs: export)
    name = "Chat Loop Start"
    category = "AI/LLM"
    icon = "⟳💬"
    short_description = "Starts a chat loop: shows the conversation on the canvas and waits for your message."
    description = (
        "A Recursive Loop Start and a Chat in one node: close the loop with a Recursive Loop End whose second "
        "port gets the conversation with the answer added (e.g. LLM Chat -> Concatenate). The first time round "
        "the conversation is the input table (columns role and content) - or, without one, the Greeting; "
        "after that, what the loop end feeds back. Each time it shows the conversation, waits for the message "
        "typed into it (Enter sends) and outputs the conversation with it last. End chat ends the loop (no end "
        "variable needed; it is also pushed); Cancel stops it. When it does not run, Start chat runs it."
    )
    in_ports = [Port(TABLE, "Conversation", "role and content: how it begins (optional: the Greeting)", optional=True)]
    out_ports = [Port(TABLE, "Conversation", "With your new message last")]
    renderer = "chat"
    width, height = 380, 460
    fields = [
        F("title", "string", "Title", "", "Shown above the chat on the canvas. Empty: none."),
        F("greeting", "string", "Greeting", "Hi! What would you like to know?",
          "The first message, from the assistant, when no conversation comes in (empty: none)."),
        *CHAT_FIELDS,
    ]

    def _first(self, df, s):
        if df is not None:
            return df
        greeting = (s.get("greeting") or "").strip()
        return pl.DataFrame({"role": ["assistant"] if greeting else [], "content": [greeting] if greeting else []},
                            schema=CONVERSATION)

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0] if in_specs and in_specs[0] is not None else pl.Schema(CONVERSATION)
        _check_conversation(spec)
        return [spec]

    def execute(self, inputs, s, ctx):
        it = ctx.loop["iteration"] if ctx.loop else 0
        fed_back = _state(ctx).get("recursion") if ctx.loop else None
        df = self._first(inputs[0], s) if it == 0 or fed_back is None else fed_back
        if ctx.loop is not None:
            ctx.loop["last"] = False
        ctx.push_variable("currentIteration", int(it), "INTEGER")
        out, ended = chat_turn(df, s, ctx)
        if ended and ctx.loop is not None:
            ctx.loop["last"] = True  # (the Recursive Loop End stops after this time round)
        return [out]

    # the chat on the canvas (as the Chat's)
    def widget(self, s: dict, display) -> dict:
        return {"type": "output", "renderer": self.renderer, "title": s.get("title") or "", "width": self.width,
                "height": self.height, "data": display, "interactive": False, "selection": None}


def _context_text(df: pl.DataFrame, s: dict) -> str:
    limit = max(1, int(s.get("context_rows") or 200))
    rows = df.head(limit)
    col = s.get("context_column")
    if col and col in rows.columns:
        lines = [str(v) for v in rows[col].to_list() if v is not None]
    else:
        lines = ["; ".join(f"{c}: {v}" for c, v in r.items() if v is not None) for r in rows.iter_rows(named=True)]
    more = f"\n(and {df.height - limit} more rows)" if df.height > limit else ""
    return "\n".join(f"- {line}" for line in lines) + more


class LLMChat(NodeModel):
    factory = "verknode.llm.Chat"
    name = "LLM Chat"
    category = "AI/LLM"
    icon = "LLM"
    node_type = "Manipulator"
    short_description = "The model's answer to a conversation (e.g. of a Chat in a loop)."
    description = (
        "Sends the conversation (a table with role and content columns, e.g. from a Chat) to the model, with a "
        "system prompt, and outputs its answer as one row (role assistant, content, model, seconds, tokens) - "
        "to be added to the conversation (Concatenate). Only the last messages are sent (Memory). An optional "
        "second table is given to the model as information to answer from (one line per row, or the "
        "information column): a chat about your data. The server settings are those of the LLM Prompter "
        "(empty: VERKNODE_LLM_URL / VERKNODE_LLM_MODEL)."
    )
    in_ports = [Port(TABLE, "Conversation", "role and content columns"),
                Port(TABLE, "Information", "Rows the model answers from (optional)", optional=True)]
    out_ports = [Port(TABLE, "Answer", "One row: role, content, model, seconds, tokens")]
    ANSWER = {"role": pl.String, "content": pl.String, "model": pl.String, "seconds": pl.Float64,
              "tokens": pl.Int64}
    fields = [
        F("system", "code", "System prompt", "You are a friendly, helpful assistant. Answer briefly and clearly.",
          "Who the assistant is and how it answers."),
        F("memory", "int", "Memory (messages)", 40, "How many of the last messages are sent: older ones are forgotten."),
        F("context_column", "column", "Information column", None,
          "Of the second table; empty: every column, as name: value.", port=2),
        F("context_rows", "int", "Information rows at most", 200),
        *server_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        missing = [c for c in ("role", "content") if c not in spec]
        if missing:
            raise InvalidSettingsError(f"The conversation needs the columns role and content (missing: {', '.join(missing)})")
        return [pl.Schema(self.ANSWER)]

    def execute(self, inputs, s, ctx):
        conv, info = inputs[0], inputs[1] if len(inputs) > 1 else None
        if ctx.dry:
            return [pl.DataFrame(schema=self.ANSWER)]
        msgs = [{"role": r, "content": c or ""} for r, c in zip(conv["role"].to_list(), conv["content"].to_list())
                if r in ROLES]
        memory = max(1, int(s.get("memory") or 40))
        msgs = msgs[-memory:]
        while msgs and msgs[0]["role"] == "assistant":  # (start with the user: some models insist)
            msgs = msgs[1:]
        if not msgs or msgs[-1]["role"] != "user":
            # (e.g. the chat was ended: nothing to answer - no answer, and no error)
            ctx.log("Nothing to answer: the conversation does not end with a user message")
            return [pl.DataFrame(schema=self.ANSWER)]
        system = (s.get("system") or "").strip()
        if info is not None and info.height:
            system = (system + "\n\n" if system else "") + "Answer from this information:\n" + _context_text(info, s)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        srv = server(s)
        r = chat(srv, msgs, s)
        ctx.log(f"{srv['model']}: {r['tokens_out'] or 0} tokens in {r['seconds']:.1f} s")
        return [pl.DataFrame({"role": ["assistant"], "content": [r["text"].strip()], "model": [srv["model"]],
                              "seconds": [r["seconds"]], "tokens": [r["tokens_out"]]}, schema=self.ANSWER)]
