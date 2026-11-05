"""Rule Engine syntax compiled to polars expressions.

    // comment
    $age$ > 30 AND $name$ LIKE "A*" => "senior A"
    $score$ IN (1, 2, 3) => $label$
    MISSING $value$ => "n/a"
    $${Sthreshold}$$ = "x" => "flow"
    TRUE => "default"
    $active$ => "yes"                 a Boolean column is a condition by itself

`==` is read as `=`. In a row filter, a rule without `=> outcome` means `=> TRUE`.

Rules are evaluated top-down; the first matching rule determines the outcome.
"""
from __future__ import annotations

import difflib
import re

import polars as pl

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<flowvar>\$\$\{(?P<fvtype>[SIDB])(?P<fvname>[^}]*)\}\$\$)
     |(?P<col>\$(?P<colname>[^$]+)\$)
     |(?P<str>"(?:[^"\\]|\\.)*")
     |(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
     |(?P<op>=>|==|<=|>=|!=|<>|=|<|>|\(|\)|,|-)
     |(?P<word>[A-Za-z_][A-Za-z_0-9]*)
    )""",
    re.X,
)

KEYWORDS = {"AND", "OR", "XOR", "NOT", "LIKE", "MATCHES", "IN", "MISSING", "TRUE", "FALSE"}


class RuleSyntaxError(ValueError):
    pass


def _tokenize(line: str) -> list[tuple[str, object]]:
    pos, out = 0, []
    while pos < len(line):
        if not line[pos:].strip():
            break
        m = _TOKEN.match(line, pos)
        if not m or m.end() == pos:
            raise RuleSyntaxError(f"Unexpected input at: {line[pos:].strip()[:20]!r}")
        pos = m.end()
        if m.group("flowvar"):
            out.append(("flowvar", m.group("fvname")))
        elif m.group("col"):
            out.append(("col", m.group("colname")))
        elif m.group("str"):
            out.append(("str", re.sub(r"\\(.)", r"\1", m.group("str")[1:-1])))
        elif m.group("num"):
            t = m.group("num")
            out.append(("num", float(t) if any(c in t for c in ".eE") else int(t)))
        elif m.group("op"):
            out.append(("op", "=" if m.group("op") == "==" else m.group("op")))
        else:
            w = m.group("word")
            out.append(("kw", w.upper()) if w.upper() in KEYWORDS else ("word", w))
    return out


def _wildcard_to_regex(pattern: str) -> str:
    return "^" + "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern) + "$"


class _Parser:
    def __init__(self, tokens, columns, variables):
        self.t = tokens
        self.i = 0
        self.columns = columns
        self.variables = variables

    def peek(self, kind=None, value=None):
        if self.i >= len(self.t):
            return None
        tok = self.t[self.i]
        if kind and tok[0] != kind:
            return None
        if value is not None and tok[1] != value:
            return None
        return tok

    def take(self, kind=None, value=None):
        tok = self.peek(kind, value)
        if tok is None:
            want = value or kind or "token"
            got = self.t[self.i][1] if self.i < len(self.t) else "end of rule"
            raise RuleSyntaxError(f"Expected {want} but found {got!r}")
        self.i += 1
        return tok

    def rule(self, outcome=None):
        cond = self.or_()
        if outcome is not None and self.i == len(self.t):
            return cond, pl.lit(outcome)
        self.take("op", "=>")
        outcome = self.operand(literal_only=False)
        if self.i != len(self.t):
            raise RuleSyntaxError(f"Unexpected {self.t[self.i][1]!r} after outcome")
        return cond, outcome

    def or_(self):
        e = self.xor()
        while self.peek("kw", "OR"):
            self.i += 1
            e = e | self.xor()
        return e

    def xor(self):
        e = self.and_()
        while self.peek("kw", "XOR"):
            self.i += 1
            e = e.xor(self.and_())
        return e

    def and_(self):
        e = self.not_()
        while self.peek("kw", "AND"):
            self.i += 1
            e = e & self.not_()
        return e

    def not_(self):
        if self.peek("kw", "NOT"):
            self.i += 1
            return ~self.not_()
        return self.primary()

    def primary(self):
        if self.peek("op", "("):
            self.i += 1
            e = self.or_()
            self.take("op", ")")
            return e
        if self.peek("kw", "MISSING"):
            self.i += 1
            return self.operand().is_null()
        if self.peek("kw", "TRUE") or self.peek("kw", "FALSE"):
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt is None or nxt == ("op", "=>") or nxt[0] == "kw" or nxt == ("op", ")"):
                self.i += 1
                return pl.lit(self.t[self.i - 1][1] == "TRUE")
        left = self.operand()
        tok = self.peek()
        if self.t[self.i - 1][0] == "col" and (tok is None or tok in (("op", "=>"), ("op", ")"))
                                               or tok in (("kw", "AND"), ("kw", "OR"), ("kw", "XOR"))):
            return left.cast(pl.Boolean)
        if tok and tok[0] == "op" and tok[1] in ("=", "!=", "<>", "<", "<=", ">", ">="):
            self.i += 1
            right = self.operand()
            op = tok[1]
            return {
                "=": left == right,
                "!=": left != right,
                "<>": left != right,
                "<": left < right,
                "<=": left <= right,
                ">": left > right,
                ">=": left >= right,
            }[op]
        if tok == ("kw", "LIKE"):
            self.i += 1
            pattern = self.literal_text()
            return left.cast(pl.String).str.contains(_wildcard_to_regex(pattern))
        if tok == ("kw", "MATCHES"):
            self.i += 1
            pattern = self.literal_text()
            return left.cast(pl.String).str.contains(f"^(?:{pattern})$")
        if tok == ("kw", "IN"):
            self.i += 1
            self.take("op", "(")
            values = [self.literal_value()]
            while self.peek("op", ","):
                self.i += 1
                values.append(self.literal_value())
            self.take("op", ")")
            return left.is_in(values)
        raise RuleSyntaxError("Expected comparison operator (=, <, >, LIKE, MATCHES, IN, ...)")

    def literal_value(self):
        tok = self.take()
        if tok[0] in ("str", "num"):
            return tok[1]
        if tok == ("op", "-"):
            return -self.take("num")[1]
        if tok[0] == "flowvar":
            return self._var(tok[1])
        if tok[0] == "kw" and tok[1] in ("TRUE", "FALSE"):
            return tok[1] == "TRUE"
        raise RuleSyntaxError(f"Expected a literal but found {tok[1]!r}")

    def literal_text(self) -> str:
        v = self.literal_value()
        if not isinstance(v, str):
            raise RuleSyntaxError("Pattern must be a string")
        return v

    def _var(self, name):
        if name not in self.variables:
            raise RuleSyntaxError(f'Flow variable "{name}" not found')
        return self.variables[name]

    def operand(self, literal_only=False):
        tok = self.peek()
        if tok and tok[0] == "col":
            self.i += 1
            if tok[1] not in self.columns:
                close = difflib.get_close_matches(tok[1], sorted(self.columns), 3, 0.6)
                raise RuleSyntaxError(f'Column "{tok[1]}" not found' +
                                      (f" - did you mean {', '.join(f'${c}$' for c in close)}?" if close else ""))
            return pl.col(tok[1])
        return pl.lit(self.literal_value())


wildcard_to_regex = _wildcard_to_regex  # public name (LIKE wildcards * and ?)


def _column_hint(line: str, columns) -> str:
    """A hint at the column syntax when the rule writes a column another way ($name without the
    closing $, a bare column name): what models and people often write."""
    plain = re.sub(r'"(?:[^"\\]|\\.)*"|\$\$\{[^}]*\}\$\$', " ", line)
    named = [c for c in columns if re.search(rf"\$\s*{re.escape(c)}(?!\$)", plain) or
             re.search(rf"(?<![$\w]){re.escape(c)}(?![$\w])", re.sub(r"\$[^$]*\$", " ", plain))]
    if not named and plain.count("$") % 2 == 0:
        return ""
    example = "$column$ > 10 => TRUE"
    if named:   # the rule itself with its columns written right (strings left as they are)
        parts = re.split(r'("(?:[^"\\]|\\.)*")', line)
        for c in sorted(named, key=len, reverse=True):
            parts = [x if i % 2 else re.sub(rf"(?<![\w$])\$?\s*{re.escape(c)}(?![\w$])", f"${c}$", x)
                     for i, x in enumerate(parts)]
        example = "".join(parts)
    return f" - columns are written between two dollar signs, $name$ (e.g. {example})"


def compile_rules(text: str, columns, variables: dict, outcome=None) -> list:
    """The rules of text; outcome: the outcome of a rule written without `=> outcome`
    (TRUE in a row filter), None when every rule needs one."""
    rules = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            rules.append(_Parser(_tokenize(line), set(columns), variables).rule(outcome))
        except RuleSyntaxError as e:
            raise RuleSyntaxError(f"Rule {lineno}: {e}{_column_hint(line, columns)}") from None
    if not rules:
        raise RuleSyntaxError("No rules defined")
    return rules


def rules_expression(rules: list, default=None) -> pl.Expr:
    (c, o), rest = rules[0], rules[1:]
    chain = pl.when(c).then(o)
    for c, o in rest:
        chain = chain.when(c).then(o)
    return chain.otherwise(default)
