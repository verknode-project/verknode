"""Shared helpers of this node folder.

Files starting with "_" are not scanned for nodes, but the node files import
them with relative imports (from ._text import ...), and "Reload nodes"
re-imports them too.
"""
import polars as pl


def clean_text(expr: pl.Expr, lower: bool, collapse: bool, ascii_only: bool) -> pl.Expr:
    """Polars expression cleaning a String column."""
    expr = expr.str.strip_chars()
    if collapse:
        expr = expr.str.replace_all(r"\s+", " ")
    if lower:
        expr = expr.str.to_lowercase()
    if ascii_only:
        expr = expr.str.replace_all(r"[^\x00-\x7F]", "")
    return expr
