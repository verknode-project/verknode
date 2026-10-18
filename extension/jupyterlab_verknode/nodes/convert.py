"""Convert & Replace nodes, including the expression languages.

String Manipulation and Math Formula expressions are parsed by small
recursive-descent parsers and compiled to polars expressions (user input is
never evaluated as Python).
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import urllib.parse
import warnings

import polars as pl

from verknode.node import infer_type
from verknode.node import F, FLOWVAR, FLOWVARIABLE, InvalidSettingsError, NodeModel, Port, TABLE
from verknode.node import wildcard_to_regex as _wildcard_to_regex
from verknode.node import CELL_DTYPES, CELL_TYPES, dtype_kind, is_numeric_kind, parse_cell
from .manipulation import selected_columns

CAT = "Manipulation/Column/Convert & Replace"
CAT_SPLIT = "Manipulation/Column/Split & Combine"
CAT_VAR = "Flow Control/Flow Variable"
IN = [Port(TABLE, "Input table")]
ALL_COLUMNS = {"include": [], "exclude": [], "enforce": "exclusion"}
NO_COLUMNS = {"include": [], "exclude": [], "enforce": "inclusion"}


class ExpressionError(InvalidSettingsError):
    pass


# ======================================================================
# tokenizer shared by both expression languages
# ======================================================================
_TOKEN = re.compile(
    r"""\s*(?:
      (?P<cur>\$\$CURRENT_?COLUMN\$\$)
     |(?P<special>\$\$ROW(?:INDEX|COUNT)\$\$)
     |(?P<flowvar>\$\$\{(?P<fvtype>[SIDB])(?P<fvname>[^}]*)\}\$\$)
     |(?P<col>\$(?P<colname>[^$]+)\$)
     |(?P<str>"(?:[^"\\]|\\.)*")
     |(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
     |(?P<op>\|\||&&|==|!=|<=|>=|[-+*/%^<>!(),])
     |(?P<ident>[A-Za-z_][A-Za-z_0-9]*)
    )""",
    re.X,
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _tokenize(text: str) -> list:
    pos, out = 0, []
    while True:
        rest = text[pos:]
        if not rest.strip():
            break
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            at = pos + len(rest) - len(rest.lstrip())
            raise ExpressionError(f"Unexpected character {text[at]!r} at position {at + 1}")
        start = m.start(m.lastgroup) if m.lastgroup else pos
        pos = m.end()
        if m.group("cur"):
            out.append(("cur", None, start))
        elif m.group("special"):
            out.append(("special", m.group("special").strip("$"), start))
        elif m.group("flowvar"):
            out.append(("var", m.group("fvname"), start))
        elif m.group("col"):
            out.append(("col", m.group("colname"), start))
        elif m.group("str"):
            raw = m.group("str")[1:-1]
            out.append(("str", re.sub(r"\\(.)", lambda e: _ESCAPES.get(e.group(1), e.group(1)), raw), start))
        elif m.group("num"):
            t = m.group("num")
            out.append(("num", float(t) if any(c in t for c in ".eE") else int(t), start))
        elif m.group("op"):
            out.append(("op", m.group("op"), start))
        else:
            out.append(("ident", m.group("ident"), start))
    return out


class _Parser:
    """Builds an AST of tuples: (kind, ..., position)."""

    def __init__(self, text: str, math_ops: bool):
        self.t = _tokenize(text)
        self.i = 0
        self.math = math_ops
        self.end_pos = len(text) + 1

    def peek(self, kind=None, value=None):
        if self.i >= len(self.t):
            return None
        tok = self.t[self.i]
        if (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            return None
        return tok

    def take(self, kind, value=None, what=None):
        tok = self.peek(kind, value)
        if tok is None:
            raise ExpressionError(f"Expected {what or value or kind} {self._where()}")
        self.i += 1
        return tok

    def _where(self):
        if self.i >= len(self.t):
            return "at end of expression"
        tok = self.t[self.i]
        return f"but found {str(tok[1]) if tok[1] is not None else tok[0]!r} at position {tok[2] + 1}"

    def parse(self):
        if not self.t:
            raise ExpressionError("Expression is empty")
        node = self.or_() if self.math else self.primary()
        if self.i != len(self.t):
            raise ExpressionError(f"Unexpected input {self._where()[10:]}")
        return node

    # --- math precedence levels -----------------------------------------
    def _binary(self, ops, next_level):
        node = next_level()
        while (tok := self.peek("op")) and tok[1] in ops:
            self.i += 1
            node = ("bin", tok[1], node, next_level(), tok[2])
        return node

    def or_(self):
        return self._binary(("||",), self.and_)

    def and_(self):
        return self._binary(("&&",), self.comparison)

    def comparison(self):
        return self._binary(("==", "!=", "<", "<=", ">", ">="), self.additive)

    def additive(self):
        return self._binary(("+", "-"), self.multiplicative)

    def multiplicative(self):
        return self._binary(("*", "/", "%"), self.unary)

    def unary(self):
        tok = self.peek("op")
        if tok and tok[1] in ("-", "+", "!"):
            self.i += 1
            return ("un", tok[1], self.unary(), tok[2])
        return self.power()

    def power(self):
        base = self.primary()
        tok = self.peek("op", "^")
        if tok:
            self.i += 1
            return ("bin", "^", base, self.unary(), tok[2])  # right associative
        return base

    # --- operands --------------------------------------------------------
    def primary(self):
        tok = self.peek()
        if tok is None:
            raise ExpressionError("Unexpected end of expression")
        kind, value, pos = tok
        if kind == "op" and value == "(" and self.math:
            self.i += 1
            node = self.or_()
            self.take("op", ")", "')'")
            return node
        if kind == "op" and value == "-" and not self.math:
            self.i += 1
            num = self.take("num", what="number")
            return ("num", -num[1], pos)
        if kind in ("num", "str", "col", "var", "cur", "special"):
            self.i += 1
            return (kind, value, pos)
        if kind == "ident":
            self.i += 1
            if self.peek("op", "("):
                self.i += 1
                args = []
                if not self.peek("op", ")"):
                    args.append(self.or_() if self.math else self.primary())
                    while self.peek("op", ","):
                        self.i += 1
                        args.append(self.or_() if self.math else self.primary())
                self.take("op", ")", "')' or ','")
                return ("call", value, args, pos)
            return ("ident", value, pos)
        raise ExpressionError(f"Unexpected {value!r} at position {pos + 1}")


# ======================================================================
# compilation helpers
# ======================================================================
_NO = object()


class V:
    """Compiled value: polars expression, plus the Python value if constant."""

    __slots__ = ("expr", "const")

    def __init__(self, expr, const=_NO):
        self.expr = expr
        self.const = const

    @classmethod
    def lit(cls, value, dtype=None):
        return cls(pl.lit(value, dtype=dtype), value)

    @property
    def is_const(self):
        return self.const is not _NO


class Scope:
    def __init__(self, schema=None, variables=None, current: str | None = None):
        self.schema = dict(schema or {})
        self.variables = variables or {}
        self.current = current

    def column(self, name, pos):
        if name not in self.schema:
            names = list(self.schema)
            have = ", ".join(names[:15]) + (", ..." if len(names) > 15 else "")
            raise ExpressionError(f'Column "{name}" at position {pos + 1} does not exist (the columns: {have})')
        return name

    def variable(self, name, pos):
        if name not in self.variables:
            raise ExpressionError(f'Flow variable "{name}" at position {pos + 1} does not exist')
        return self.variables[name]

    def current_column(self, pos):
        if self.current is None:
            raise ExpressionError(f"$$CURRENTCOLUMN$$ (position {pos + 1}) is only available in multi column nodes")
        return self.current


def _python(fn, args: list, dtype):
    """Apply a Python function row-wise (constant-folded when possible)."""
    if all(a.is_const for a in args):
        return V.lit(fn(*[a.const for a in args]), dtype)
    names = [f"a{i}" for i in range(len(args))]
    struct = pl.struct([a.expr.alias(n) for a, n in zip(args, names)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expr = struct.map_elements(lambda d: fn(*[d[n] for n in names]), return_dtype=dtype, skip_nulls=False)
    return V(expr)


def _null_safe(fn):
    def wrapped(first, *rest):
        return None if first is None else fn(first, *rest)

    return wrapped


# ======================================================================
# String Manipulation
# ======================================================================
def _s(v: V) -> pl.Expr:
    return v.expr.cast(pl.String)


def _const(v: V, what: str, func: str, kind=str):
    if not v.is_const or (kind is not None and not isinstance(v.const, kind)):
        raise ExpressionError(f"{func}(): {what} must be a constant {'text' if kind is str else 'value'}")
    return v.const


def _int_const(v: V, what, func):
    if not v.is_const or isinstance(v.const, bool) or not isinstance(v.const, (int, float)):
        raise ExpressionError(f"{func}(): {what} must be a constant number")
    return int(v.const)


def _when_not_null(src: pl.Expr, value: pl.Expr) -> pl.Expr:
    return pl.when(src.is_null()).then(None).otherwise(value)


def _sm_modifiers(args, idx, func) -> str:
    return _const(args[idx], "modifiers", func) if len(args) > idx else ""


def _f_replace(a):
    s, search, repl = a[0], a[1], a[2]
    mods = _sm_modifiers(a, 3, "replace")
    if "i" in mods.lower():
        pattern = "(?i)" + re.escape(_const(search, "search text", "replace"))
        return _s(s).str.replace_all(pattern, _const(repl, "replacement", "replace").replace("$", "$$"))
    return _s(s).str.replace_all(_s(search), _s(repl), literal=True)


def _f_count(a):
    mods = _sm_modifiers(a, 2, "count")
    if "i" in mods.lower():
        pattern = "(?i)" + re.escape(_const(a[1], "search text", "count"))
        return _s(a[0]).str.count_matches(pattern).cast(pl.Int32)
    return _s(a[0]).str.count_matches(_s(a[1]), literal=True).cast(pl.Int32)


def _f_indexof(a):
    if len(a) == 2:
        s = _s(a[0])
        return _when_not_null(s, s.str.find(_s(a[1]), literal=True).fill_null(-1)).cast(pl.Int32)

    def fn(s, needle, start=0, mods=""):
        if s is None or needle is None:
            return None
        if "i" in (mods or "").lower():
            s, needle = s.lower(), needle.lower()
        return s.find(needle, int(start or 0))

    return _python(fn, a, pl.Int32).expr


def _f_lastindexof(a):
    def fn(s, needle, mods=""):
        if s is None or needle is None:
            return None
        if "i" in (mods or "").lower():
            s, needle = s.lower(), needle.lower()
        return s.rfind(needle)

    return _python(fn, [a[0], a[1]] + a[2:3], pl.Int32).expr


def _pad(left):
    def fn(s, length, chars=" "):
        if s is None:
            return None
        n = int(length)
        chars = chars or " "
        if len(s) >= n:
            return s
        fill = (chars * n)[: n - len(s)]
        return fill + s if left else s + fill

    return lambda a: _python(fn, [V(_s(a[0])) if not a[0].is_const else V.lit(str(a[0].const))] + a[1:], pl.String).expr


def _f_replace_chars(a):
    chars = _const(a[1], "characters", "replaceChars")
    repl = _const(a[2], "replacement", "replaceChars") if len(a) > 2 else ""
    table = {ord(c): (repl[i] if i < len(repl) else None) for i, c in enumerate(chars)}
    return _python(_null_safe(lambda s: s.translate(table)), [V(_s(a[0]))], pl.String).expr


def _f_regex_matcher(a):
    pattern = _const(a[1], "regular expression", "regexMatcher")
    s = _s(a[0])
    return _when_not_null(s, pl.when(s.str.contains(f"^(?:{pattern})$")).then(pl.lit("True")).otherwise(pl.lit("False")))


def _f_compare(a):
    x, y = _s(a[0]), _s(a[1])
    return pl.when(x < y).then(pl.lit(-1)).when(x > y).then(pl.lit(1)).when(x == y).then(pl.lit(0)).otherwise(None).cast(pl.Int32)


def _capitalize(s: str) -> str:
    return re.sub(r"(^|\s)(\S)", lambda m: m.group(1) + m.group(2).upper(), s)


def _remove_diacritic(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _py1(fn, dtype=pl.String):
    return lambda a: _python(_null_safe(fn), [V(_s(a[0]))], dtype).expr


def _to_bool(a):
    return _s(a[0]).str.strip_chars().str.to_lowercase() == "true"


# name -> (min args, max args (None = unlimited), implementation(list[V]) -> pl.Expr, description)
STRING_FUNCTIONS = {
    "join": (1, None, lambda a: pl.concat_str([_s(x) for x in a]), "join(str, ...) concatenates strings"),
    "joinSep": (2, None, lambda a: pl.concat_str([_s(x) for x in a[1:]],
                                                 separator=_const(a[0], "separator", "joinSep")),
                "joinSep(sep, str, ...) concatenates with a separator"),
    "upperCase": (1, 1, lambda a: _s(a[0]).str.to_uppercase(), "upperCase(str)"),
    "lowerCase": (1, 1, lambda a: _s(a[0]).str.to_lowercase(), "lowerCase(str)"),
    "capitalize": (1, 1, _py1(_capitalize), "capitalize(str) makes the first letter of each word upper case (the others stay: capitalize(lowerCase(str)) for Title Case)"),
    "substr": (2, 3, lambda a: _s(a[0]).str.slice(a[1].expr.cast(pl.Int64),
                                                  a[2].expr.cast(pl.Int64) if len(a) > 2 else None),
               "substr(str, start[, length]); start is 0-based"),
    "replace": (3, 4, _f_replace, 'replace(str, search, replacement[, "i"])'),
    "replaceChars": (2, 3, _f_replace_chars, "replaceChars(str, chars, replacement)"),
    "regexReplace": (3, 3, lambda a: _s(a[0]).str.replace_all(_s(a[1]), _s(a[2])),
                     "regexReplace(str, regex, replacement) ($1 = group 1)"),
    "regexMatcher": (2, 2, _f_regex_matcher, 'regexMatcher(str, regex) -> "True"/"False"'),
    "strip": (1, 1, lambda a: _s(a[0]).str.strip_chars(), "strip(str) removes leading/trailing whitespace"),
    "stripStart": (1, 1, lambda a: _s(a[0]).str.strip_chars_start(), "stripStart(str)"),
    "stripEnd": (1, 1, lambda a: _s(a[0]).str.strip_chars_end(), "stripEnd(str)"),
    "removeChars": (2, 2, lambda a: _s(a[0]).str.replace_all(
        "[" + "".join(re.escape(c) for c in _const(a[1], "characters", "removeChars")) + "]", "")
        if _const(a[1], "characters", "removeChars") else _s(a[0]), "removeChars(str, chars)"),
    "removeDuplicates": (1, 1, lambda a: _s(a[0]).str.replace_all(" {2,}", " "),
                         "removeDuplicates(str) replaces runs of spaces by one space"),
    "removeSpaces": (1, 1, lambda a: _s(a[0]).str.replace_all(" ", "", literal=True), "removeSpaces(str)"),
    "removeDiacritic": (1, 1, _py1(_remove_diacritic), "removeDiacritic(str)"),
    "length": (1, 1, lambda a: _s(a[0]).str.len_chars().cast(pl.Int32), "length(str)"),
    "indexOf": (2, 4, _f_indexof, 'indexOf(str, search[, start[, "i"]]) or -1'),
    "lastIndexOf": (2, 3, _f_lastindexof, 'lastIndexOf(str, search[, "i"]) or -1'),
    "padLeft": (2, 3, _pad(True), "padLeft(str, length[, chars])"),
    "padRight": (2, 3, _pad(False), "padRight(str, length[, chars])"),
    "reverse": (1, 1, lambda a: _s(a[0]).str.reverse(), "reverse(str)"),
    "count": (2, 3, _f_count, 'count(str, search[, "i"])'),
    "compare": (2, 2, _f_compare, "compare(str1, str2) -> -1, 0, 1"),
    "string": (1, 1, lambda a: _s(a[0]), "string(x) converts to String"),
    "toInt": (1, 1, lambda a: a[0].expr.cast(pl.Float64, strict=False).cast(pl.Int32, strict=False)
              if not a[0].is_const or not isinstance(a[0].const, str)
              else _s(a[0]).str.strip_chars().cast(pl.Float64, strict=False).cast(pl.Int32, strict=False),
              "toInt(x)"),
    "toLong": (1, 1, lambda a: a[0].expr.cast(pl.Float64, strict=False).cast(pl.Int64, strict=False), "toLong(x)"),
    "toDouble": (1, 1, lambda a: a[0].expr.cast(pl.Float64, strict=False), "toDouble(x)"),
    "toBoolean": (1, 1, _to_bool, 'toBoolean(x) ("true" ignoring case)'),
    "toNull": (1, 1, lambda a: pl.when(_s(a[0]) == "").then(None).otherwise(_s(a[0])), 'toNull(str) "" -> missing'),
    "toEmpty": (1, 1, lambda a: _s(a[0]).fill_null(""), 'toEmpty(str) missing -> ""'),
    "urlEncode": (1, 1, _py1(lambda s: urllib.parse.quote_plus(s)), "urlEncode(str)"),
    "urlDecode": (1, 1, _py1(lambda s: urllib.parse.unquote_plus(s)), "urlDecode(str)"),
    "md5Checksum": (1, 1, _py1(lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()), "md5Checksum(str)"),  # noqa: S324
}

STRING_HELP = (
    "Combine functions, columns (<code>$column$</code>), flow variables (<code>$${Sname}$$</code>), "
    'string literals ("text") and numbers, e.g. <code>join(upperCase($first$), " ", $last$)</code>. '
    "Functions: " + ", ".join(f"<code>{f[3]}</code>" for f in STRING_FUNCTIONS.values()) + "."
)


def _const_value(value, pos):
    if isinstance(value, bool):
        return V.lit(value, pl.Boolean)
    if isinstance(value, int):
        return V.lit(value, pl.Int64)
    if isinstance(value, float):
        return V.lit(value, pl.Float64)
    return V.lit(value, pl.String)


def _with_hint(text: str, scope: Scope, compile_):
    """compile_() - its error with a hint at the column syntax when the expression writes columns
    another way ($name without the closing $, a bare column name): what models and people often write."""
    try:
        return compile_()
    except ExpressionError as e:
        cols = list(scope.schema)
        plain = re.sub(r'"(?:[^"\\]|\\.)*"|\$\$\{[^}]*\}\$\$|\$\$[A-Z_]+\$\$', " ", text or "")
        named = [c for c in cols if re.search(rf"\$\s*{re.escape(c)}(?!\$)", plain) or
                 re.search(rf"(?<![$\w]){re.escape(c)}(?![$\w])", re.sub(r"\$[^$]*\$", " ", plain))]
        if named or plain.count("$") % 2:
            example = " * ".join(f"${c}$" for c in (named or cols)[:2])
            raise ExpressionError(f"{e} - columns are written between two dollar signs, $name$"
                                  + (f" (e.g. {example})" if example else "")) from None
        raise


def compile_string(text: str, scope: Scope) -> pl.Expr:
    return _with_hint(text, scope, lambda: _compile_sm(_Parser(text or "", math_ops=False).parse(), scope).expr)


def _compile_sm(node, scope: Scope) -> V:
    kind = node[0]
    if kind in ("num", "str"):
        return _const_value(node[1], node[2])
    if kind == "col":
        return V(pl.col(scope.column(node[1], node[2])))
    if kind == "cur":
        return V(pl.col(scope.current_column(node[2])))
    if kind == "var":
        return _const_value(scope.variable(node[1], node[2]), node[2])
    if kind == "special":
        if node[1] == "ROWINDEX":
            return V(pl.int_range(pl.len(), dtype=pl.Int64))
        return V(pl.len().cast(pl.Int64))
    if kind == "call":
        name, args, pos = node[1], node[2], node[3]
        if name not in STRING_FUNCTIONS:
            raise ExpressionError(f'Unknown function "{name}" at position {pos + 1}')
        lo, hi, impl, desc = STRING_FUNCTIONS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise ExpressionError(f"Wrong number of arguments for {name}() at position {pos + 1}: {desc}")
        return V(impl([_compile_sm(a, scope) for a in args]))
    if kind == "ident":
        raise ExpressionError(f'Unexpected name "{node[1]}" at position {node[2] + 1} (function calls need parentheses)')
    raise ExpressionError(f"Unexpected expression at position {node[-1] + 1}")


# ======================================================================
# Math Formula (JEP syntax)
# ======================================================================
def _f64(v: V) -> pl.Expr:
    return v.expr.cast(pl.Float64)


def _bool(e: pl.Expr) -> pl.Expr:
    return e.cast(pl.Float64)


def _java_mod(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """Remainder with the sign of the dividend (Java / JEP semantics)."""
    return a - b * _trunc(a / b)


def _trunc(e: pl.Expr) -> pl.Expr:
    return pl.when(e >= 0).then(e.floor()).otherwise(e.ceil())


def _round_half_up(e: pl.Expr, digits: pl.Expr | None = None) -> pl.Expr:
    if digits is None:
        return (e + 0.5).floor()
    scale = pl.lit(10.0).pow(digits.cast(pl.Float64))
    return (e * scale + 0.5).floor() / scale


def _null_if_any_null(args, value: pl.Expr) -> pl.Expr:
    cond = pl.any_horizontal([a.is_null() for a in args])
    return pl.when(cond).then(None).otherwise(value)


def _col_agg(fn):
    def impl(args, nodes, scope):
        node = nodes[0]
        if node[0] == "col":
            name = scope.column(node[1], node[2])
        elif node[0] == "cur":
            name = scope.current_column(node[2])
        else:
            raise ExpressionError(f"Column aggregation functions need a column reference (position {node[-1] + 1})")
        _require_numeric(scope, name, node[-1])
        return fn(pl.col(name).cast(pl.Float64))

    return impl


def _binom(n, k):
    if n is None or k is None:
        return None
    return float(math.comb(int(n), int(k)))


# name -> (min, max, impl(list[pl.Expr]) or (args, nodes, scope) for column aggregates)
MATH_FUNCTIONS = {
    "abs": (1, 1, lambda a: a[0].abs()),
    "sqrt": (1, 1, lambda a: a[0].sqrt()),
    "exp": (1, 1, lambda a: a[0].exp()),
    "ln": (1, 1, lambda a: a[0].log()),
    "log": (1, 1, lambda a: a[0].log(10)),
    "log10": (1, 1, lambda a: a[0].log(10)),
    "log2": (1, 1, lambda a: a[0].log(2)),
    "sin": (1, 1, lambda a: a[0].sin()),
    "cos": (1, 1, lambda a: a[0].cos()),
    "tan": (1, 1, lambda a: a[0].tan()),
    "asin": (1, 1, lambda a: a[0].arcsin()),
    "acos": (1, 1, lambda a: a[0].arccos()),
    "atan": (1, 1, lambda a: a[0].arctan()),
    "atan2": (2, 2, lambda a: pl.arctan2(a[0], a[1])),
    "sinh": (1, 1, lambda a: a[0].sinh()),
    "cosh": (1, 1, lambda a: a[0].cosh()),
    "tanh": (1, 1, lambda a: a[0].tanh()),
    "asinh": (1, 1, lambda a: a[0].arcsinh()),
    "acosh": (1, 1, lambda a: a[0].arccosh()),
    "atanh": (1, 1, lambda a: a[0].arctanh()),
    "ceil": (1, 1, lambda a: a[0].ceil()),
    "floor": (1, 1, lambda a: a[0].floor()),
    "round": (1, 2, lambda a: _round_half_up(a[0], a[1] if len(a) > 1 else None)),
    "rint": (1, 1, lambda a: a[0].round(0, mode="half_to_even")),
    "sign": (1, 1, lambda a: a[0].sign().cast(pl.Float64)),
    "min": (1, None, lambda a: _null_if_any_null(a, pl.min_horizontal(a))),
    "max": (1, None, lambda a: _null_if_any_null(a, pl.max_horizontal(a))),
    "mod": (2, 2, lambda a: _java_mod(a[0], a[1])),
    "pow": (2, 2, lambda a: a[0].pow(a[1])),
    "binom": (2, 2, None),  # row-wise Python (math.comb)
    "if": (3, 3, lambda a: pl.when(a[0] != 0).then(a[1]).otherwise(a[2])),
    "isNaN": (1, 1, lambda a: _bool(a[0].is_nan())),
    "COL_MEAN": (1, 1, _col_agg(lambda c: c.mean())),
    "COL_MEDIAN": (1, 1, _col_agg(lambda c: c.median())),
    "COL_SUM": (1, 1, _col_agg(lambda c: c.sum())),
    "COL_MIN": (1, 1, _col_agg(lambda c: c.min())),
    "COL_MAX": (1, 1, _col_agg(lambda c: c.max())),
    "COL_STDDEV": (1, 1, _col_agg(lambda c: c.std())),
    "COL_VAR": (1, 1, _col_agg(lambda c: c.var())),
    "COL_COUNT": (1, 1, _col_agg(lambda c: c.count().cast(pl.Float64))),
}
COLUMN_AGGREGATES = {k for k in MATH_FUNCTIONS if k.startswith("COL_")}
MATH_CONSTANTS = {"e": math.e, "pi": math.pi, "E": math.e, "PI": math.pi}

MATH_HELP = (
    "JEP syntax: <code>+ - * / % ^</code>, comparisons <code>== != &lt; &lt;= &gt; &gt;=</code> "
    "and logic <code>&amp;&amp; || !</code> (true = 1, false = 0), columns <code>$col$</code>, "
    "flow variables <code>$${Dname}$$</code>, constants <code>e</code>, <code>pi</code>, "
    "<code>$$ROWINDEX$$</code>, <code>$$ROWCOUNT$$</code>. Functions: "
    + ", ".join(f"<code>{k}</code>" for k in MATH_FUNCTIONS)
    + ". Example: <code>if($a$ &gt; 0, round($a$ / COL_MAX($a$), 2), 0)</code>."
)


def _require_numeric(scope: Scope, name, pos):
    t = scope.schema.get(name)
    if t is not None and not (is_numeric_kind(dtype_kind(t)) or dtype_kind(t) in ("boolean", "null")):
        raise ExpressionError(f'Column "{name}" at position {pos + 1} is not numeric ({t})')


def compile_math(text: str, scope: Scope) -> pl.Expr:
    """Compile a JEP formula; missing values in referenced columns give a missing result."""
    tree = _with_hint(text, scope, lambda: _Parser(text or "", math_ops=True).parse())
    expr = _with_hint(text, scope, lambda: _compile_math(tree, scope).expr.cast(pl.Float64))
    refs = sorted(_row_references(tree, scope))
    if refs:
        expr = pl.when(pl.any_horizontal([pl.col(c).is_null() for c in refs])).then(None).otherwise(expr)
    return expr


def _row_references(node, scope: Scope) -> set:
    """Columns referenced row-wise (not only inside COL_* aggregates)."""
    kind = node[0]
    if kind == "col":
        return {node[1]}
    if kind == "cur":
        return {scope.current} if scope.current else set()
    if kind == "call":
        if node[1] in COLUMN_AGGREGATES:
            return set()
        return set().union(*[_row_references(a, scope) for a in node[2]]) if node[2] else set()
    if kind == "un":
        return _row_references(node[2], scope)
    if kind == "bin":
        return _row_references(node[2], scope) | _row_references(node[3], scope)
    return set()


def _compile_math(node, scope: Scope) -> V:
    kind = node[0]
    if kind == "num":
        return V.lit(float(node[1]), pl.Float64)
    if kind == "str":
        raise ExpressionError(f"Strings are not supported in Math Formula (position {node[2] + 1})")
    if kind in ("col", "cur"):
        name = scope.column(node[1], node[2]) if kind == "col" else scope.current_column(node[2])
        _require_numeric(scope, name, node[2])
        return V(pl.col(name).cast(pl.Float64))
    if kind == "var":
        value = scope.variable(node[1], node[2])
        try:
            return V.lit(float(value), pl.Float64)
        except (TypeError, ValueError):
            raise ExpressionError(f'Flow variable "{node[1]}" (position {node[2] + 1}) is not numeric') from None
    if kind == "special":
        if node[1] == "ROWINDEX":
            return V(pl.int_range(pl.len(), dtype=pl.Int64).cast(pl.Float64))
        return V(pl.len().cast(pl.Float64))
    if kind == "ident":
        if node[1] in MATH_CONSTANTS:
            return V.lit(MATH_CONSTANTS[node[1]], pl.Float64)
        if node[1] in ("ROWINDEX", "ROWCOUNT"):
            return _compile_math(("special", node[1], node[2]), scope)
        raise ExpressionError(f'Unknown constant "{node[1]}" at position {node[2] + 1}')
    if kind == "un":
        v = _f64(_compile_math(node[2], scope))
        return V({"-": -v, "+": v, "!": _bool(v == 0)}[node[1]])
    if kind == "bin":
        op = node[1]
        a, b = _f64(_compile_math(node[2], scope)), _f64(_compile_math(node[3], scope))
        expr = {
            "+": lambda: a + b,
            "-": lambda: a - b,
            "*": lambda: a * b,
            "/": lambda: a / b,
            "%": lambda: _java_mod(a, b),
            "^": lambda: a.pow(b),
            "==": lambda: _bool(a == b),
            "!=": lambda: _bool(a != b),
            "<": lambda: _bool(a < b),
            "<=": lambda: _bool(a <= b),
            ">": lambda: _bool(a > b),
            ">=": lambda: _bool(a >= b),
            "&&": lambda: _bool((a != 0) & (b != 0)),
            "||": lambda: _bool((a != 0) | (b != 0)),
        }[op]()
        return V(expr)
    if kind == "call":
        name, args, pos = node[1], node[2], node[3]
        if name not in MATH_FUNCTIONS:
            raise ExpressionError(f'Unknown function "{name}" at position {pos + 1}')
        lo, hi, impl = MATH_FUNCTIONS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}" if lo == hi else f"{lo} or more" if hi is None else f"{lo}-{hi}"
            raise ExpressionError(f"{name}() at position {pos + 1} expects {want} argument(s), got {len(args)}")
        if name in COLUMN_AGGREGATES:
            return V(impl(None, args, scope))
        compiled = [_compile_math(a, scope) for a in args]
        if name == "binom":
            return _python(_binom, compiled, pl.Float64)
        return V(impl([_f64(c) for c in compiled]))
    raise ExpressionError(f"Unexpected expression at position {node[-1] + 1}")


def _evaluate_scalar(expr: pl.Expr):
    """Evaluate an expression that only uses constants (flow variable nodes)."""
    return pl.select(expr.alias("result"))["result"][0]


# ======================================================================
# expression nodes
# ======================================================================
OUTPUT_FIELDS = [
    F("mode", "enum", "Result", "Append column", options=["Append column", "Replace column"]),
    F("new_column", "string", "Appended column name", "new column", show_if={"mode": "Append column"}),
    F("replace_column", "column", "Replaced column", None, show_if={"mode": "Replace column"}),
]


def _output_name(df: pl.DataFrame, s: dict) -> str:
    if s["mode"] == "Replace column":
        name = s.get("replace_column")
        if not name:
            raise InvalidSettingsError("No column to replace selected")
        if name not in df.columns:
            raise InvalidSettingsError(f'Column "{name}" not found')
        return name
    name = (s.get("new_column") or "").strip()
    if not name:
        raise InvalidSettingsError("No name for the appended column")
    if name in df.columns:
        raise InvalidSettingsError(f'Column "{name}" already exists')
    return name


def _multi_targets(df, s, kinds=None) -> list:
    cols = selected_columns(s["columns"], df.schema)
    if kinds:
        cols = [c for c in cols if dtype_kind(df.schema[c]) in kinds]
    if not cols:
        raise InvalidSettingsError("No columns selected")
    return cols


def _multi_output(df, s, results: dict) -> pl.DataFrame:
    if s["mode"] == "Replace selected columns":
        return df.with_columns(e.alias(c) for c, e in results.items())
    out = []
    for c, e in results.items():
        name = f"{c}{s['suffix']}"
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists (choose another suffix)')
        out.append(e.alias(name))
    return df.with_columns(out)


MULTI_FIELDS = [
    F("mode", "enum", "Result", "Append selected columns", options=["Append selected columns", "Replace selected columns"]),
    F("suffix", "string", "Suffix of appended columns", " (transformed)", show_if={"mode": "Append selected columns"}),
]


class StringManipulation(NodeModel):
    factory = "verknode.manipulation.StringManipulation"
    name = "String Manipulation"
    category = CAT
    icon = "abc"
    short_description = "Manipulates strings: search and replace, case conversion, padding, concatenation..."
    description = "Evaluates a string expression for each row and appends or replaces a column. " + STRING_HELP
    in_ports = IN
    out_ports = [Port(TABLE, "Table with manipulated column")]
    fields = [
        F("expression", "code", "Expression", 'join($column1$, "")', language="stringmanipulation", insertables=True),
        *OUTPUT_FIELDS,
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        expr = compile_string(s["expression"], Scope(df.schema, ctx.variables()))
        return [df.with_columns(expr.alias(_output_name(df, s)))]


class StringManipulationMultiColumn(NodeModel):
    factory = "verknode.manipulation.StringManipulationMultiColumn"
    name = "String Manipulation (Multi Column)"
    category = CAT
    icon = "abc*"
    short_description = "Applies a string expression to several columns."
    description = (
        "Evaluates the expression once per selected column; <code>$$CURRENTCOLUMN$$</code> refers to the "
        "column being processed. " + STRING_HELP
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with manipulated columns")]
    fields = [
        F("columns", "columns", "Columns", NO_COLUMNS),
        F("expression", "code", "Expression", "upperCase($$CURRENTCOLUMN$$)", language="stringmanipulation",
          insertables=True),
        *MULTI_FIELDS,
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        variables = ctx.variables()
        results = {c: compile_string(s["expression"], Scope(df.schema, variables, c))
                   for c in _multi_targets(df, s)}
        return [_multi_output(df, s, results)]


VAR_OUTPUT_FIELDS = [
    F("mode", "enum", "Result", "Create new variable", options=["Create new variable", "Replace variable"]),
    F("new_variable", "string", "New variable name", "new_variable", show_if={"mode": "Create new variable"}),
    F("replace_variable", "string", "Replaced variable", "", show_if={"mode": "Replace variable"}),
]


def _variable_target(s, ctx) -> str:
    if s["mode"] == "Replace variable":
        name = (s.get("replace_variable") or "").strip()
        if name not in ctx.variables():
            raise InvalidSettingsError(f'Flow variable "{name}" does not exist')
        return name
    name = (s.get("new_variable") or "").strip()
    if not name:
        raise InvalidSettingsError("No name for the new variable")
    return name


def _python_value(value):
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


class StringManipulationVariable(NodeModel):
    factory = "verknode.manipulation.StringManipulationVariable"
    name = "String Manipulation (Variable)"
    category = CAT_VAR
    node_type = "Other"
    icon = "abcV"
    short_description = "Manipulates string flow variables."
    description = "Evaluates a String Manipulation expression on flow variables and pushes the result as a variable. " + STRING_HELP
    in_ports = [Port(FLOWVARIABLE, "Input variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Output variables")]
    fields = [
        F("expression", "code", "Expression", 'join($${Sname}$$, "")', language="stringmanipulation", insertables=True),
        *VAR_OUTPUT_FIELDS,
    ]

    def execute(self, inputs, s, ctx):
        target = _variable_target(s, ctx)
        value = _python_value(_evaluate_scalar(compile_string(s["expression"], Scope({}, ctx.variables()))))
        if value is None:
            raise InvalidSettingsError("Expression evaluated to a missing value")
        ctx.push_variable(target, value, infer_type(value))
        return [FLOWVAR]


class MathFormula(NodeModel):
    factory = "verknode.manipulation.MathFormula"
    name = "Math Formula"
    category = CAT
    icon = "∑x"
    short_description = "Evaluates a mathematical expression for each row."
    description = "Calculates a numeric value for each row and appends or replaces a column. " + MATH_HELP
    in_ports = IN
    out_ports = [Port(TABLE, "Table with computed column")]
    fields = [
        F("expression", "code", "Expression", "$column1$ * 2", language="math", insertables=True),
        *OUTPUT_FIELDS,
        F("convert_to_int", "bool", "Convert to Int", False, "Round the result to an integer column."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        expr = compile_math(s["expression"], Scope(df.schema, ctx.variables()))
        if s["convert_to_int"]:
            expr = _round_half_up(expr).cast(pl.Int32, strict=False)
        return [df.with_columns(expr.alias(_output_name(df, s)))]


class MathFormulaMultiColumn(NodeModel):
    factory = "verknode.manipulation.MathFormulaMultiColumn"
    name = "Math Formula (Multi Column)"
    category = CAT
    icon = "∑x*"
    short_description = "Evaluates a mathematical expression on several columns."
    description = (
        "Evaluates the expression once per selected numeric column; <code>$$CURRENT_COLUMN$$</code> refers "
        "to the column being processed. " + MATH_HELP
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with computed columns")]
    fields = [
        F("columns", "columns", "Columns", NO_COLUMNS, "Only numeric columns are processed."),
        F("expression", "code", "Expression", "$$CURRENT_COLUMN$$ / COL_MAX($$CURRENT_COLUMN$$)", language="math",
          insertables=True),
        *MULTI_FIELDS,
        F("convert_to_int", "bool", "Convert to Int", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        variables = ctx.variables()
        results = {}
        for c in _multi_targets(df, s, kinds=("integer", "long", "double", "boolean")):
            expr = compile_math(s["expression"], Scope(df.schema, variables, c))
            results[c] = _round_half_up(expr).cast(pl.Int32, strict=False) if s["convert_to_int"] else expr
        return [_multi_output(df, s, results)]


class MathFormulaVariable(NodeModel):
    factory = "verknode.manipulation.MathFormulaVariable"
    name = "Math Formula (Variable)"
    category = CAT_VAR
    node_type = "Other"
    icon = "∑V"
    short_description = "Evaluates a mathematical expression on flow variables."
    description = "Calculates a number from flow variables and pushes it as a variable. " + MATH_HELP
    in_ports = [Port(FLOWVARIABLE, "Input variables", optional=True)]
    out_ports = [Port(FLOWVARIABLE, "Output variables")]
    fields = [
        F("expression", "code", "Expression", "$${Dvalue}$$ * 2", language="math", insertables=True),
        *VAR_OUTPUT_FIELDS,
        F("convert_to_int", "bool", "Convert to Int", False),
    ]

    def execute(self, inputs, s, ctx):
        target = _variable_target(s, ctx)
        value = _python_value(_evaluate_scalar(compile_math(s["expression"], Scope({}, ctx.variables()))))
        if value is None:
            raise InvalidSettingsError("Expression evaluated to a missing value")
        if s["convert_to_int"]:
            ctx.push_variable(target, int(math.floor(value + 0.5)), "INTEGER")
        else:
            ctx.push_variable(target, float(value), "DOUBLE")
        return [FLOWVAR]


# ======================================================================
# replace / lookup
# ======================================================================
class StringReplacer(NodeModel):
    factory = "verknode.manipulation.StringReplacer"
    name = "String Replacer"
    category = CAT
    icon = "a→b"
    short_description = "Replaces values in a string column that match a pattern."
    description = (
        "Replaces the whole value or all occurrences of a pattern in a String column. Patterns are "
        "literal text, wildcards (<code>*</code> any characters, <code>?</code> one character) or "
        "regular expressions (the replacement may reference groups as <code>$1</code>)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with replaced values")]
    fields = [
        F("column", "column", "Target column", None, kinds=["string"]),
        F("pattern_type", "enum", "Pattern type", "Literal", options=["Literal", "Wildcard", "Regular expression"]),
        F("pattern", "string", "Pattern", ""),
        F("replacement", "string", "Replacement text", ""),
        F("replace", "enum", "Replace", "All occurrences", options=["Whole string", "All occurrences"]),
        F("case_sensitive", "bool", "Case sensitive", True),
        F("mode", "enum", "Result", "Replace column", options=["Replace column", "Append column"]),
        F("new_column", "string", "Appended column name", "replaced", show_if={"mode": "Append column"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = s.get("column")
        if not col:
            raise InvalidSettingsError("No target column selected")
        if col not in df.columns:
            raise InvalidSettingsError(f'Column "{col}" not found')
        pattern = s["pattern"] or ""
        if not pattern:
            raise InvalidSettingsError("No pattern specified")
        regex = s["pattern_type"] == "Regular expression"
        if s["pattern_type"] == "Wildcard":
            core = _wildcard_to_regex(pattern)[1:-1]
        elif regex:
            core = pattern
        else:
            core = re.escape(pattern)
        flags = "" if s["case_sensitive"] else "(?i)"
        repl = s["replacement"] or ""
        if not regex:
            repl = repl.replace("$", "$$")
        text = pl.col(col).cast(pl.String)
        if s["replace"] == "Whole string":
            whole = f"{flags}^(?:{core})$"
            expr = pl.when(text.str.contains(whole)).then(text.str.replace(whole, repl)).otherwise(text)
        else:
            try:
                re.compile(core)
            except re.error as e:
                raise InvalidSettingsError(f"Invalid regular expression: {e}") from None
            expr = text.str.replace_all(flags + core, repl)
        if s["mode"] == "Append column":
            name = s.get("new_column") or "replaced"
            if name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            return [df.with_columns(expr.alias(name))]
        return [df.with_columns(expr.alias(col))]


def _coalesce(first: pl.Series, second: pl.Series) -> pl.Series:
    """first where present, else second; String if the types cannot be unified."""
    if first.dtype == pl.Null:
        return second
    try:
        return pl.DataFrame({"a": first, "b": second}).select(pl.coalesce("a", "b"))["a"]
    except Exception:  # noqa: BLE001
        return first.cast(pl.String).fill_null(second.cast(pl.String))


def _key_pair(left: pl.Series, right: pl.Series, case_insensitive=False):
    """Join keys of compatible type (String if the types differ)."""
    if left.dtype != right.dtype or case_insensitive:
        left, right = left.cast(pl.String), right.cast(pl.String)
        if case_insensitive:
            left, right = left.str.to_lowercase(), right.str.to_lowercase()
    return left, right


class CellReplacer(NodeModel):
    factory = "verknode.manipulation.CellReplacer"
    name = "Cell Replacer"
    category = CAT
    icon = "▦→"
    short_description = "Replaces cells using a dictionary table."
    description = (
        "Looks up each value of the target column in the input column of the dictionary table (second "
        "input) and replaces it by the corresponding value of the output column."
    )
    in_ports = [Port(TABLE, "Input table"), Port(TABLE, "Dictionary table")]
    out_ports = [Port(TABLE, "Table with replaced values")]
    fields = [
        F("column", "column", "Target column", None, port=1),
        F("lookup", "column", "Dictionary input (lookup) column", None, port=2),
        F("replacement", "column", "Dictionary output (replacement) column", None, port=2),
        F("no_match", "enum", "If no element matches", "Use input",
          options=["Use input", "Insert missing value"]),
        F("mode", "enum", "Result", "Replace column", options=["Replace column", "Append column"]),
        F("new_column", "string", "Appended column name", "replaced", show_if={"mode": "Append column"}),
    ]

    def execute(self, inputs, s, ctx):
        df, dictionary = inputs
        for key, table, label in (("column", df, "target"), ("lookup", dictionary, "lookup"),
                                  ("replacement", dictionary, "replacement")):
            if not s.get(key):
                raise InvalidSettingsError(f"No {label} column selected")
            if s[key] not in table.columns:
                raise InvalidSettingsError(f'Column "{s[key]}" not found')
        col = s["column"]
        lk, rk = _key_pair(df[col], dictionary[s["lookup"]])
        mapping = pl.DataFrame({"__key": rk, "__value": dictionary[s["replacement"]]}) \
            .filter(pl.col("__key").is_not_null()).unique("__key", keep="first", maintain_order=True) \
            .with_columns(pl.lit(True).alias("__hit"))
        joined = pl.DataFrame({"__key": lk}).join(mapping, on="__key", how="left", maintain_order="left")
        value = joined["__value"]
        if s["no_match"] == "Use input":
            original = df[col]
            if value.dtype not in (original.dtype, pl.Null):
                value, original = value.cast(pl.String), original.cast(pl.String)
            value = pl.DataFrame({"m": joined["__hit"].fill_null(False), "v": value, "o": original}).select(
                pl.when("m").then("v").otherwise("o").alias("r"))["r"]
        target = col
        if s["mode"] == "Append column":
            target = s.get("new_column") or "replaced"
            if target in df.columns:
                raise InvalidSettingsError(f'Column "{target}" already exists')
        return [df.with_columns(value.alias(target))]


class ValueLookup(NodeModel):
    factory = "verknode.manipulation.ValueLookup"
    name = "Value Lookup"
    category = CAT
    icon = "⌕"
    short_description = "Looks up values in a dictionary table and appends matching columns."
    description = (
        "For each row the value of the lookup column is searched in the key column of the dictionary "
        "table (second input). Selected dictionary columns of the matching row are appended."
    )
    in_ports = [Port(TABLE, "Data table"), Port(TABLE, "Dictionary table")]
    out_ports = [Port(TABLE, "Data table with appended columns")]
    fields = [
        F("lookup", "column", "Lookup column (data table)", None, port=1),
        F("key", "column", "Key column (dictionary table)", None, port=2),
        F("columns", "columns", "Dictionary columns to append", ALL_COLUMNS, port=2),
        F("case_insensitive", "bool", "Case insensitive (String keys)", False),
        F("multiple", "enum", "If multiple rows match", "Use first match", options=["Use first match", "Use last match"]),
        F("match_column", "string", "Match indicator column (empty: none)", ""),
        F("suffix", "string", "Suffix for duplicate column names", " (dictionary)"),
    ]

    def execute(self, inputs, s, ctx):
        df, dictionary = inputs
        if not s.get("lookup") or s["lookup"] not in df.columns:
            raise InvalidSettingsError("Select an existing lookup column")
        if not s.get("key") or s["key"] not in dictionary.columns:
            raise InvalidSettingsError("Select an existing key column")
        cols = [c for c in selected_columns(s["columns"], dictionary.schema) if c != s["key"]]
        lk, rk = _key_pair(df[s["lookup"]], dictionary[s["key"]], s["case_insensitive"])
        right = dictionary.select(cols).with_columns(rk.alias("__key"), pl.lit(True).alias("__match")) \
            .filter(pl.col("__key").is_not_null()) \
            .unique("__key", keep="first" if s["multiple"] == "Use first match" else "last", maintain_order=True)
        right = right.rename({c: (c if c not in df.columns else f"{c}{s['suffix']}") for c in cols})
        joined = pl.DataFrame({"__key": lk}).join(right, on="__key", how="left", maintain_order="left")
        out = df.with_columns(joined.drop("__key", "__match").get_columns())
        if s["match_column"]:
            out = out.with_columns(joined["__match"].fill_null(False).alias(s["match_column"]))
        return [out]


# ======================================================================
# type conversion
# ======================================================================
class NumberToString(NodeModel):
    factory = "verknode.manipulation.NumberToString"
    name = "Number To String"
    category = CAT
    icon = "1→a"
    short_description = "Converts numeric columns to String columns."
    description = "Converts the selected numeric columns into String columns."
    in_ports = IN
    out_ports = [Port(TABLE, "Transformed input table")]
    fields = [F("columns", "columns", "Columns", ALL_COLUMNS, "Only numeric columns are converted.")]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c in selected_columns(s["columns"], df.schema) if is_numeric_kind(dtype_kind(df.schema[c]))]
        return [df.with_columns(pl.col(c).cast(pl.String) for c in cols)]


class StringToNumber(NodeModel):
    factory = "verknode.manipulation.StringToNumber"
    name = "String To Number"
    category = CAT
    icon = "a→1"
    short_description = "Converts String columns to numbers."
    description = (
        "Parses the selected String columns as numbers, honouring the decimal and thousands separators. "
        "Values that cannot be parsed become missing (or fail the node)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Transformed input table")]
    fields = [
        F("columns", "columns", "Columns", ALL_COLUMNS, "Only String columns are converted."),
        F("type", "enum", "Type", "Number (double)", options=["Number (double)", "Number (integer)", "Number (long)"]),
        F("decimal_separator", "string", "Decimal separator", "."),
        F("thousands_separator", "string", "Thousands separator", ""),
        F("fail_on_error", "bool", "Fail on error", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        dtype = {"Number (double)": pl.Float64, "Number (integer)": pl.Int32, "Number (long)": pl.Int64}[s["type"]]
        cols = [c for c in selected_columns(s["columns"], df.schema) if dtype_kind(df.schema[c]) == "string"]
        exprs = []
        for c in cols:
            e = pl.col(c).str.strip_chars()
            if s["thousands_separator"]:
                e = e.str.replace_all(s["thousands_separator"], "", literal=True)
            if s["decimal_separator"] and s["decimal_separator"] != ".":
                e = e.str.replace_all(s["decimal_separator"], ".", literal=True)
            e = e.cast(pl.Float64, strict=False)
            if dtype != pl.Float64:
                e = e.cast(dtype, strict=False)
            exprs.append(e.alias(c))
        out = df.with_columns(exprs)
        failed = sum(out[c].null_count() - df[c].null_count() for c in cols)
        if failed:
            if s["fail_on_error"]:
                raise ValueError(f"{failed} value(s) could not be converted")
            ctx.warn(f"{failed} value(s) could not be converted and are missing")
        return [out]


class DoubleToInt(NodeModel):
    factory = "verknode.manipulation.DoubleToInt"
    name = "Double To Int"
    category = CAT
    icon = "D→I"
    short_description = "Converts double columns to integer columns."
    description = "Rounds (half up), floors or ceils the selected double columns and converts them to integers."
    in_ports = IN
    out_ports = [Port(TABLE, "Transformed input table")]
    fields = [
        F("columns", "columns", "Columns", ALL_COLUMNS, "Only double columns are converted."),
        F("rounding", "enum", "Rounding type", "round", options=["round", "floor", "ceil"]),
        F("long", "bool", "Create long values", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        dtype = pl.Int64 if s["long"] else pl.Int32
        cols = [c for c in selected_columns(s["columns"], df.schema) if dtype_kind(df.schema[c]) == "double"]
        fn = {"round": _round_half_up, "floor": lambda e: e.floor(), "ceil": lambda e: e.ceil()}[s["rounding"]]
        return [df.with_columns(fn(pl.col(c)).cast(dtype, strict=False).alias(c) for c in cols)]


ROUNDING_MODES = ["HALF_UP", "HALF_DOWN", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR"]


def _round_mode(e: pl.Expr, digits: int, mode: str) -> pl.Expr:
    scale = 10.0 ** digits
    v = (e * scale).round(9)  # tame binary floating point artefacts (2.675 * 100)
    sign = v.sign()
    a = v.abs()
    r = {
        "HALF_UP": lambda: sign * (a + 0.5).floor(),
        "HALF_DOWN": lambda: sign * (a - 0.5).ceil(),
        "HALF_EVEN": lambda: v.round(0, mode="half_to_even"),
        "UP": lambda: sign * a.ceil(),
        "DOWN": lambda: sign * a.floor(),
        "CEILING": lambda: v.ceil(),
        "FLOOR": lambda: v.floor(),
    }[mode]()
    return r / scale


class RoundDouble(NodeModel):
    factory = "verknode.manipulation.RoundDouble"
    name = "Round Double"
    category = CAT
    icon = "≈"
    short_description = "Rounds numeric columns to a precision."
    description = (
        "Rounds the selected numeric columns to the given number of decimal places using the chosen "
        "rounding mode (HALF_UP rounds 0.5 away from zero). The result can be a number or a String."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with rounded values")]
    fields = [
        F("columns", "columns", "Columns", ALL_COLUMNS, "Only numeric columns are rounded."),
        F("precision", "int", "Decimal places", 3),
        F("mode_rounding", "enum", "Rounding mode", "HALF_UP", options=ROUNDING_MODES),
        F("output", "enum", "Output format", "Double", options=["Double", "String"]),
        F("mode", "enum", "Result", "Replace selected columns", options=["Replace selected columns", "Append selected columns"]),
        F("suffix", "string", "Suffix of appended columns", " (rounded)", show_if={"mode": "Append selected columns"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        digits = max(0, s["precision"])
        results = {}
        for c in selected_columns(s["columns"], df.schema):
            kind = dtype_kind(df.schema[c])
            if not is_numeric_kind(kind):
                continue
            e = _round_mode(pl.col(c).cast(pl.Float64), digits, s["mode_rounding"])
            if s["output"] == "String":
                fmt = f"{{:.{digits}f}}"
                e = e.map_elements(fmt.format, return_dtype=pl.String)
            results[c] = e
        if not results:
            raise InvalidSettingsError("No numeric columns selected")
        return [_multi_output(df, s, results)]


_BOOL_STRINGS = {"true": True, "false": False}


def _detect(series: pl.Series, missing: str) -> tuple[pl.Series, str]:
    """Most specific type all present values of a String column parse as."""
    s = series.str.strip_chars()
    s = pl.DataFrame({"v": s}).select(
        pl.when((pl.col("v") == "") | (pl.col("v") == (missing or "\0"))).then(None).otherwise("v").alias("v"))["v"]
    present = s.drop_nulls()
    if present.len() == 0:
        return series, "String"
    for dtype, label in ((pl.Int64, "Number (long)"), (pl.Float64, "Number (double)")):
        if present.cast(dtype, strict=False).null_count() == 0:
            return s.cast(dtype, strict=False), label
    if present.str.to_lowercase().is_in(list(_BOOL_STRINGS)).all():
        return s.str.to_lowercase().replace_strict(_BOOL_STRINGS, default=None, return_dtype=pl.Boolean), "Boolean"
    for label, parse in (("Local Date", lambda x: x.str.to_date(strict=False)),
                         ("Local Date Time", lambda x: x.str.to_datetime(strict=False, time_unit="us"))):
        try:
            if parse(present).null_count() == 0:
                return parse(s), label
        except Exception:  # noqa: BLE001 - format not detectable
            continue
    return series, "String"


class ColumnAutoTypeCast(NodeModel):
    factory = "verknode.manipulation.ColumnAutoTypeCast"
    name = "Column Auto Type Cast"
    category = CAT
    icon = "?→T"
    short_description = "Detects and converts the types of String columns."
    description = (
        "Determines the most specific type (integer, double, boolean, date, date &amp; time) that all values "
        "of a String column can be parsed as, and converts the column. Columns that do not fit stay String."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Type-casted columns")]
    fields = [
        F("columns", "columns", "Columns", ALL_COLUMNS, "Only String columns are inspected."),
        F("missing", "string", "Missing value pattern", "", "Text treated as missing value, e.g. ? or NA."),
    ]

    def configure(self, in_specs, s, ctx):
        return [None]  # result types depend on the data

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        out = df
        for c in selected_columns(s["columns"], df.schema):
            if dtype_kind(df.schema[c]) != "string":
                continue
            series, kind = _detect(df[c], s["missing"])
            out = out.with_columns(series.alias(c))
            ctx.log(f'Column "{c}": {kind}')
        return [out]


class CategoryToNumber(NodeModel):
    factory = "verknode.manipulation.CategoryToNumber"
    name = "Category To Number"
    category = CAT
    icon = "C→1"
    short_description = "Maps categories (String values) to integers."
    description = (
        "Assigns an integer to each distinct value of the selected String columns, in order of appearance. "
        "The mapping is output as a second table (column, value, number)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with numeric columns"), Port(TABLE, "Mapping")]
    fields = [
        F("columns", "columns", "Columns", ALL_COLUMNS, "Only String columns are processed."),
        F("suffix", "string", "Column suffix", " (to number)", "Empty: replace the columns."),
        F("start", "int", "Start value", 0),
        F("increment", "int", "Increment", 1),
        F("max_categories", "int", "Maximum number of categories", 1000),
        F("default", "int", "Value for missing", -1, "Missing values are mapped to this number."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c in selected_columns(s["columns"], df.schema) if dtype_kind(df.schema[c]) == "string"]
        exprs, mapping = [], {"Column": [], "Value": [], "Number": []}
        for c in cols:
            values = df[c].drop_nulls().unique(maintain_order=True).to_list()
            if len(values) > s["max_categories"]:
                raise ValueError(f'Column "{c}" has more than {s["max_categories"]} categories')
            numbers = [s["start"] + i * s["increment"] for i in range(len(values))]
            mapping["Column"] += [c] * len(values)
            mapping["Value"] += values
            mapping["Number"] += numbers
            e = pl.col(c).replace_strict(values, numbers, default=None, return_dtype=pl.Int32).fill_null(s["default"])
            exprs.append(e.alias(f"{c}{s['suffix']}" if s["suffix"] else c))
        for e in exprs:
            name = e.meta.output_name()
            if s["suffix"] and name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
        table = pl.DataFrame({"Column": pl.Series(mapping["Column"], dtype=pl.String),
                              "Value": pl.Series(mapping["Value"], dtype=pl.String),
                              "Number": pl.Series(mapping["Number"], dtype=pl.Int32)})
        return [df.with_columns(exprs), table]


class OneToMany(NodeModel):
    factory = "verknode.manipulation.OneToMany"
    name = "One to Many"
    category = CAT_SPLIT
    icon = "1→n"
    short_description = "Creates one indicator column per distinct value."
    description = (
        "For each distinct value of the selected columns a new Integer column is appended which is 1 if "
        "the row has this value and 0 otherwise (one-hot encoding)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with indicator columns")]
    fields = [
        F("columns", "columns", "Columns", NO_COLUMNS),
        F("remove", "bool", "Remove included columns", False),
    ]

    def configure(self, in_specs, s, ctx):
        if in_specs[0] is not None and not selected_columns(s["columns"], in_specs[0]):
            raise InvalidSettingsError("No columns selected")
        return [None]  # new columns depend on the data

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        new, names = [], set(df.columns)
        for c in cols:
            for v in df[c].drop_nulls().unique(maintain_order=True).to_list():
                name = str(v) if str(v) not in names else f"{v}_{c}"
                names.add(name)
                new.append((pl.col(c) == v).fill_null(False).cast(pl.Int32).alias(name))
        out = df.with_columns(new)
        return [out.drop(cols) if s["remove"] else out]


class ManyToOne(NodeModel):
    factory = "verknode.manipulation.ManyToOne"
    name = "Many to One"
    category = CAT_SPLIT
    icon = "n→1"
    short_description = "Condenses several columns into one category column."
    description = (
        "Creates a column holding the name of the column that matches for each row: the column with value "
        "1 (binary), or the column with the maximum / minimum value. Rows with no or several matches get "
        "a missing value."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with condensed column")]
    fields = [
        F("columns", "columns", "Columns", NO_COLUMNS),
        F("new_column", "string", "Condensed column name", "Condensed Column"),
        F("pattern", "enum", "Includes", "Binary (1 = match)", options=["Binary (1 = match)", "Maximum", "Minimum"]),
        F("remove", "bool", "Remove included columns", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        name = s["new_column"] or "Condensed Column"
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        num = [pl.col(c).cast(pl.Float64, strict=False) for c in cols]
        if s["pattern"].startswith("Binary"):
            hits = [(n == 1).fill_null(False) for n in num]
        else:
            ref = pl.max_horizontal(num) if s["pattern"] == "Maximum" else pl.min_horizontal(num)
            hits = [(n == ref).fill_null(False) for n in num]
        count = pl.sum_horizontal([h.cast(pl.Int32) for h in hits])
        chain = pl.when(count != 1).then(pl.lit(None, dtype=pl.String))
        for c, h in zip(cols, hits):
            chain = chain.when(h).then(pl.lit(c))
        out = df.with_columns(chain.otherwise(pl.lit(None, dtype=pl.String)).alias(name))
        return [out.drop(cols) if s["remove"] else out]


class ConstantValueColumn(NodeModel):
    factory = "verknode.manipulation.ConstantValueColumn"
    name = "Constant Value Column"
    category = CAT
    icon = "=c"
    short_description = "Appends or replaces a column with a constant value."
    description = "Adds a column with the same value in every row (parsed as the chosen type; empty = missing)."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with constant column")]
    fields = [
        F("type", "enum", "Column type", "String", options=CELL_TYPES),
        F("value", "string", "Value", "", "Dates as ISO 8601, e.g. 2024-01-31."),
        *OUTPUT_FIELDS,
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        try:
            value = parse_cell(s["value"], s["type"])
        except (TypeError, ValueError) as e:
            raise InvalidSettingsError(f"Invalid {s['type']} value: {e}") from None
        return [df.with_columns(pl.lit(value, dtype=CELL_DTYPES[s["type"]]).alias(_output_name(df, s)))]


class ColumnMerger(NodeModel):
    factory = "verknode.manipulation.ColumnMerger"
    name = "Column Merger"
    category = CAT_SPLIT
    icon = "⇉"
    short_description = "Merges two columns: primary value, or secondary if missing."
    description = (
        "Creates a column whose value is taken from the primary column, or from the secondary column "
        "where the primary value is missing."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with merged column")]
    fields = [
        F("primary", "column", "Primary column"),
        F("secondary", "column", "Secondary column"),
        F("output", "enum", "Output placement", "Replace primary column",
          options=["Replace primary column", "Replace secondary column", "Replace both columns",
                   "Append as new column"]),
        F("new_column", "string", "New column name", "Merged column", show_if={"output": "Append as new column"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        p, q = s.get("primary"), s.get("secondary")
        for c in (p, q):
            if not c or c not in df.columns:
                raise InvalidSettingsError("Select existing primary and secondary columns")
        merged = _coalesce(df[p], df[q])
        out = s["output"]
        if out == "Append as new column":
            name = s.get("new_column") or "Merged column"
            if name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            return [df.with_columns(merged.alias(name))]
        if out == "Replace secondary column":
            return [df.with_columns(merged.alias(q))]
        result = df.with_columns(merged.alias(p))
        return [result.drop(q) if out == "Replace both columns" and p != q else result]


class ColumnRenameRegex(NodeModel):
    factory = "verknode.manipulation.ColumnRenameRegex"
    name = "Column Rename (Regex)"
    category = CAT
    icon = "A→*"
    short_description = "Renames columns using a regular expression."
    description = (
        "Renames all columns matching the search pattern; the replacement may use groups (<code>$1</code>). "
        "Example: search <code>^(.*)_old$</code>, replace <code>$1_new</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with renamed columns")]
    fields = [
        F("search", "string", "Search string (regexp)", "(.*)"),
        F("replace", "string", "Replacement", "$1"),
        F("case_insensitive", "bool", "Case insensitive", False),
        F("literal", "bool", "Literal (no regular expression)", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if not s["search"]:
            raise InvalidSettingsError("No search pattern")
        pattern = re.escape(s["search"]) if s["literal"] else s["search"]
        try:
            rx = re.compile(pattern, re.IGNORECASE if s["case_insensitive"] else 0)
        except re.error as e:
            raise InvalidSettingsError(f"Invalid regular expression: {e}") from None
        repl = s["replace"] or ""
        repl = repl.replace("\\", "\\\\") if s["literal"] else re.sub(r"\$(\d+)", r"\\g<\1>", repl)
        mapping = {c: rx.sub(repl, c) for c in df.columns}
        renamed = list(mapping.values())
        dupes = {n for n in renamed if renamed.count(n) > 1}
        if dupes:
            raise InvalidSettingsError(f"Renaming creates duplicate column names: {', '.join(sorted(dupes))}")
        if "" in renamed:
            raise InvalidSettingsError("Renaming creates an empty column name")
        changed = sum(1 for k, v in mapping.items() if k != v)
        if not changed:
            ctx.warn("Pattern does not match any column name")
        return [df.rename(mapping)]
