"""Helpers shared by node implementations (exported via verknode.node)."""
from __future__ import annotations

import re

from .node import InvalidSettingsError


def selected_columns(sel, schema) -> list:
    """Resolve a twin-list ("columns" field) selection against a schema."""
    sel = sel or {}
    names = list(schema.keys())
    if sel.get("enforce") == "exclusion":
        excl = set(sel.get("exclude", []))
        return [c for c in names if c not in excl]
    incl = set(sel.get("include", []))
    return [c for c in names if c in incl]


def substitute_variables(text: str, variables: dict, sql=False) -> str:
    """Replace $${Sname}$$ flow variable references and (for SQL) $column$ references."""

    def var(m):
        name = m.group(2)
        if name not in variables:
            raise InvalidSettingsError(f'Flow variable "{name}" not found')
        v = variables[name]
        if not sql:
            return str(v)
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return repr(v)
        return "'" + str(v).replace("'", "''") + "'"

    text = re.sub(r"\$\$\{([SIDB])([^}]*)\}\$\$", var, text)
    if sql:
        text = re.sub(r"\$([^$]+)\$", lambda m: '"' + m.group(1).replace('"', '""') + '"', text)
    return text
