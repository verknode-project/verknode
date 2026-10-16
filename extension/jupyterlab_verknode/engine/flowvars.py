"""Flow variables: typed name/value pairs travelling along connections."""
from __future__ import annotations

VAR_TYPES = ("STRING", "INTEGER", "DOUBLE", "BOOLEAN")


class FlowVariable:
    __slots__ = ("name", "type", "value")

    def __init__(self, name: str, vtype: str, value):
        if vtype not in VAR_TYPES:
            raise ValueError(f"Unknown flow variable type: {vtype}")
        self.name = name
        self.type = vtype
        self.value = coerce(value, vtype)

    def to_json(self) -> dict:
        return {"name": self.name, "type": self.type, "value": self.value}

    @classmethod
    def from_json(cls, d: dict) -> "FlowVariable":
        return cls(d["name"], d["type"], d["value"])

    def __repr__(self):
        return f"FlowVariable({self.name!r}, {self.type}, {self.value!r})"


def infer_type(value) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"


def coerce(value, vtype: str):
    if vtype == "STRING":
        return "" if value is None else str(value)
    if vtype == "BOOLEAN":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y")
        return bool(value)
    if vtype == "INTEGER":
        if isinstance(value, str):
            value = value.strip() or 0
        return int(float(value)) if not isinstance(value, int) else int(value)
    if vtype == "DOUBLE":
        if isinstance(value, str):
            value = value.strip() or 0.0
        return float(value)
    raise ValueError(f"Unknown flow variable type: {vtype}")


def make(name: str, value, vtype: str | None = None) -> FlowVariable:
    return FlowVariable(name, vtype or infer_type(value), value)


def stack_to_json(stack: dict) -> list:
    # top of stack (most recently pushed) first
    return [v.to_json() for v in reversed(list(stack.values()))]


def stack_from_json(items: list) -> dict:
    stack = {}
    for d in reversed(items or []):
        v = FlowVariable.from_json(d)
        stack[v.name] = v
    return stack
