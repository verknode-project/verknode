"""verknode's folder for user-specific setup: ~/.verknode (another one: VERKNODE_HOME).

    ~/.verknode/
        llm.env          LLM settings (KEY=VALUE lines: VERKNODE_LLM_URL, API keys ...), see nodes/llm.py
        defaults.json    the node defaults (Settings › Node defaults: saved LLM servers ...), engine/defaults.py
        nodes/           your custom nodes: loaded in every workflow, next to the workflow's own node folders
        models/          downloaded model files (Local Model (llama.cpp))

In the browser (JupyterLite) it is .verknode in the browser's files. Files of older versions in
~/.config/verknode (llm.env, defaults.json) and ~/.cache/verknode/models are still read when the new place
has none.
"""
from __future__ import annotations

import os

LEGACY_CONFIG = os.path.join("~", ".config", "verknode")
LEGACY_CACHE = os.path.join("~", ".cache", "verknode")


def home() -> str:
    """The folder (it may not exist yet)."""
    return os.path.abspath(os.path.expanduser(os.environ.get("VERKNODE_HOME", "").strip() or os.path.join("~", ".verknode")))


def user_path(*parts: str, legacy: str | None = None) -> str:
    """A path in the folder; `legacy` (an older place of the same file) when only that exists."""
    p = os.path.join(home(), *parts)
    if legacy and not os.path.exists(p) and not os.environ.get("VERKNODE_HOME"):
        old = os.path.expanduser(legacy)
        if os.path.exists(old):
            return old
    return p


def user_nodes() -> str | None:
    """The user's node folder (~/.verknode/nodes), if it exists."""
    p = os.path.join(home(), "nodes")
    return p if os.path.isdir(p) else None


def display(path: str) -> str:
    """A path as the user knows it (~ for the home folder)."""
    h = os.path.expanduser("~")
    return "~" + path[len(h):] if path == h or path.startswith(h + os.sep) else path
