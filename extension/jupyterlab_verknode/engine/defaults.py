"""Global defaults: setup that nodes share across workflows, kept per user - not in the workflows.

A node module registers a section of settings (dialog fields) once::

    from verknode.node import F, register_defaults, node_defaults

    register_defaults("my_service", "My service", [
        F("url", "string", "Service URL", "https://example.org/api"),
        F("key_env", "string", "API key variable", "MY_SERVICE_KEY"),
    ], description="Where the My Service nodes connect to.")

    class MyNode(NodeModel):
        def execute(self, inputs, s, ctx):
            url = s.get("url") or node_defaults("my_service")["url"]

The editor's Settings › Node defaults tab edits every registered section; the values are saved in
~/.verknode/defaults.json (another file: VERKNODE_DEFAULTS; in the browser: .verknode/defaults.json in its
files; see engine/home.py). Store names of environment variables there, never keys: the file is plain JSON.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from typing import Callable

from .home import LEGACY_CONFIG, user_path


@dataclass
class DefaultsSection:
    key: str
    title: str
    fields: list
    description: str = ""
    order: int = 100
    check: Callable[[dict], None] | None = None  # values -> raises ValueError if they cannot be saved

    def describe(self, values: dict) -> dict:
        return {"key": self.key, "title": self.title, "description": self.description,
                "fields": [f.to_json() for f in self.fields], "values": values}


SECTIONS: dict[str, DefaultsSection] = {}
_lock = threading.Lock()
_cache: dict = {"mtime": None, "path": None, "data": {}}


def register_defaults(key: str, title: str, fields: list, description: str = "", order: int = 100,
                      check: Callable[[dict], None] | None = None) -> DefaultsSection:
    """Register (or replace, e.g. on a reload of custom nodes) a section of global defaults. (An enum field
    can offer the names of a list in the section: options_from={"field": "servers", "key": "name"}.)
    `check(values)` raises ValueError for values that cannot be saved."""
    sec = DefaultsSection(key, title, list(fields), description, order, check)
    SECTIONS[key] = sec
    return sec


def path(write: bool = False) -> str:
    """The file (to read: also an older ~/.config/verknode/defaults.json, until the first save here)."""
    env = os.environ.get("VERKNODE_DEFAULTS", "").strip()
    if env:
        return os.path.expanduser(env)
    return user_path("defaults.json", legacy=None if write else os.path.join(LEGACY_CONFIG, "defaults.json"))


def _load() -> dict:
    p = path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return {}
    with _lock:
        if _cache["path"] != p or _cache["mtime"] != mtime:
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            _cache.update(path=p, mtime=mtime, data=data if isinstance(data, dict) else {})
        return _cache["data"]


def node_defaults(section: str) -> dict:
    """The values of a section: what was saved, else each field's default."""
    sec = SECTIONS.get(section)
    saved = _load().get(section) or {}
    out = {f.key: copy.deepcopy(f.default) for f in sec.fields} if sec else {}
    out.update({k: v for k, v in saved.items() if not sec or any(f.key == k for f in sec.fields)})
    return out


def describe() -> list[dict]:
    """The sections for the editor, with their values."""
    secs = sorted(SECTIONS.values(), key=lambda s: (s.order, s.title))
    return [s.describe(node_defaults(s.key)) for s in secs]


def save(values: dict) -> list[dict]:
    """Save the values of some sections ({section: {key: value}}); other sections stay as they are."""
    data = dict(_load())
    for key, vals in (values or {}).items():
        sec = SECTIONS.get(key)
        if sec is None:
            raise ValueError(f"No node defaults {key!r}")
        known = {f.key for f in sec.fields}
        data[key] = {k: v for k, v in (vals or {}).items() if k in known}
        if sec.check:
            sec.check(data[key])
    p = path(write=True)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, p)
    with _lock:
        _cache.update(path=None, mtime=None)
    return describe()
