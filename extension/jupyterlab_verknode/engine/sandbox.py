"""A file sandbox for test runs (e.g. of the Workflow Agent): the paths nodes use (through
ExecContext.resolve_path) are mapped into a temporary folder. A file or folder is copied in the first
time it is used, so nodes read the real data and write, move or delete only copies.

Nodes that can reach files without resolve_path - scripts, notebooks, custom nodes - and nodes that
change things outside the workflow (NodeModel.sandbox_unsafe) are not run in a sandbox unless it
allows them (allow_unsafe).
"""
from __future__ import annotations

import filecmp
import os
import shutil
import tempfile

from .node import InvalidSettingsError


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for folder, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(folder, f))
            except OSError:
                pass
    return total


class Sandbox:
    def __init__(self, limit_mb: float = 500, allow_unsafe: bool = False):
        self.root = tempfile.mkdtemp(prefix="verknode-sandbox-")
        self.limit = int(limit_mb * 1e6)
        self.used = 0
        self.allow_unsafe = allow_unsafe
        self.mapped: dict[str, str] = {}  # real path -> its place in the sandbox

    def map(self, path: str) -> str:
        """The sandbox place of a real path (copied in the first time, if it exists)."""
        real = os.path.abspath(path)
        if real == self.root or real.startswith(self.root + os.sep):
            return real
        box = os.path.join(self.root, real.lstrip(os.sep))
        if real in self.mapped or os.path.lexists(box):
            return box
        # (a path inside a folder that is copied in already is in the sandbox too)
        for r, b in self.mapped.items():
            if real.startswith(r + os.sep):
                return box
        self.mapped[real] = box
        if os.path.exists(real):
            size = _size(real)
            if self.used + size > self.limit:
                raise InvalidSettingsError(
                    f"{real} is too large for the test sandbox ({(self.used + size) / 1e6:.0f} MB of "
                    f"{self.limit / 1e6:.0f} MB): raise the sandbox size, or test without it")
            os.makedirs(os.path.dirname(box), exist_ok=True)
            if os.path.isdir(real):
                shutil.copytree(real, box, symlinks=True)
            else:
                shutil.copy2(real, box)
            self.used += size
        else:
            os.makedirs(os.path.dirname(box), exist_ok=True)
        return box

    def unmap_output(self, obj):
        """A table a node made in the sandbox, with the real paths where it has sandbox ones (e.g. a file
        listing): what it shows is the real place (a node reading such a path gets the sandbox copy again)."""
        import polars as pl

        if not isinstance(obj, pl.DataFrame) or not obj.height:
            return obj
        cols = [c for c, t in obj.schema.items() if t == pl.String
                and obj[c].str.contains(self.root, literal=True).any()]
        if not cols:
            return obj
        return obj.with_columns(pl.col(c).str.replace_all(self.root, "", literal=True) for c in cols)

    def changes(self) -> list[dict]:
        """What the test run would have done to the real files: [{path, change, bytes}]."""
        out = []
        for real, box in sorted(self.mapped.items()):
            if os.path.isdir(box) or os.path.isdir(real):
                reals = {os.path.relpath(os.path.join(d, f), real) for d, _, fs in os.walk(real) for f in fs} if os.path.isdir(real) else set()
                boxes = {os.path.relpath(os.path.join(d, f), box) for d, _, fs in os.walk(box) for f in fs} if os.path.isdir(box) else set()
                for rel in sorted(reals | boxes):
                    out += self._compare(os.path.join(real, rel), os.path.join(box, rel))
            else:
                out += self._compare(real, box)
        return out

    @staticmethod
    def _compare(real: str, box: str) -> list[dict]:
        if os.path.exists(box) and not os.path.exists(real):
            return [{"path": real, "change": "new", "bytes": os.path.getsize(box)}]
        if os.path.exists(real) and not os.path.exists(box):
            return [{"path": real, "change": "deleted", "bytes": os.path.getsize(real)}]
        if os.path.isfile(real) and os.path.isfile(box) and not filecmp.cmp(real, box, shallow=False):
            return [{"path": real, "change": "modified", "bytes": os.path.getsize(box)}]
        return []

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
