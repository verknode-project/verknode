"""Custom nodes: NodeModel subclasses loaded from Python files in node folders.

A workflow lists its node folders (``workflow.json`` → ``custom_nodes.paths``,
relative to the workflow file). Every top-level ``*.py`` file in a folder is
imported as a module of a private package (so files can share helpers with
relative imports, e.g. ``from .utils import f``); every non-abstract
NodeModel subclass defined in it becomes a node. Reloading re-imports all
files of the folders.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import os
import sys
import traceback
import types

from . import node as node_api
from .node import NodeModel

TEMPLATE = '''"""Custom verknode nodes.

Every NodeModel subclass in the .py files of this folder becomes a node in
the node repository (category "Custom" unless set). Edit this file, then
press "Reload nodes" in the editor (or just execute: changed files are
reloaded automatically). print() output appears in the editor console.

Factory ids default to "custom.<file name>.<class name>"; set `factory`
explicitly to keep saved workflows working when renaming files or classes.
Classes whose name starts with "_" or that set `abstract = True` are skipped.
"""
import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE


class AddConstantColumn(NodeModel):
    name = "Add Constant Column"
    category = "Custom/Examples"
    icon = "+c"
    short_description = "Appends a column with a constant value."
    description = "Example custom node: appends a column holding the same value in every row."
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Table with the new column")]
    fields = [
        F("column", "string", "New column name", "constant"),
        F("value", "float", "Value", 1.0, "Number written into every row."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        if not s["column"]:
            raise InvalidSettingsError("No column name given")
        if s["column"] in df.columns:
            raise InvalidSettingsError(f'Column "{s["column"]}" already exists')
        print(f"adding {s['column']} = {s['value']} to {df.height} rows")
        return [df.with_columns(pl.lit(s["value"]).alias(s["column"]))]
'''


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """Always compile from source: cached bytecode can be stale when a file is
    edited twice within the same second without changing its size."""

    def get_code(self, fullname):
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    """Imports modules of custom node packages (incl. relative imports) from source."""

    folders: dict[str, str] = {}  # package name -> folder

    def find_spec(self, fullname, path=None, target=None):
        for pkg, folder in self.folders.items():
            if not fullname.startswith(pkg + "."):
                continue
            base = os.path.join(folder, *fullname[len(pkg) + 1:].split("."))
            init = os.path.join(base, "__init__.py")
            if os.path.isfile(init):
                return importlib.util.spec_from_file_location(
                    fullname, init, loader=_SourceLoader(fullname, init), submodule_search_locations=[base])
            if os.path.isfile(base + ".py"):
                return importlib.util.spec_from_file_location(
                    fullname, base + ".py", loader=_SourceLoader(fullname, base + ".py"))
        return None


_FINDER = _Finder()
if _FINDER not in sys.meta_path:
    sys.meta_path.insert(0, _FINDER)


def _package_name(folder: str) -> str:
    return "verknode_custom_" + hashlib.sha1(os.path.abspath(folder).encode()).hexdigest()[:12]


def _py_files(folder: str) -> list[str]:
    return sorted(glob.glob(os.path.join(folder, "**", "*.py"), recursive=True))


def _folder_state(folder: str) -> dict:
    out = {}
    for f in _py_files(folder):
        try:
            out[f] = os.stat(f).st_mtime_ns
        except OSError:
            pass
    return out


def _folder_hash(folder: str) -> str:
    h = hashlib.sha1()
    for f in _py_files(folder):
        try:
            with open(f, "rb") as fh:
                h.update(f.encode())
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()


def _is_node_class(obj, module_name: str) -> bool:
    return (
        inspect.isclass(obj)
        and issubclass(obj, NodeModel)
        and obj is not NodeModel
        and obj.__module__ == module_name
        and not obj.__name__.startswith("_")
        and not obj.__dict__.get("abstract", False)
    )


def discover(module, registered=()) -> list:
    """Node classes of an imported module (the node folder convention)."""
    classes = [c for c in vars(module).values() if _is_node_class(c, module.__name__)]
    return classes + [c for c in registered if c not in classes]


def apply_defaults(cls, stem: str, prefix: str = "custom"):
    """Defaults for optional class attributes of a node."""
    if not cls.__dict__.get("factory"):
        cls.factory = f"{prefix}.{stem}.{cls.__name__}"
    if not cls.__dict__.get("name"):
        cls.name = cls.__name__
    # (category and icon may come from a base class, e.g. "Widgets/Displays" of OutputWidget)
    if not getattr(cls, "category", ""):
        cls.category = "Custom" if prefix == "custom" else "Other"
    if not getattr(cls, "icon", ""):
        cls.icon = "Py"


def load_standard_nodes(package: str, folder: str, registry: dict) -> list:
    """Load the standard node folder (same conventions as custom node folders).

    Modules are imported under their real package name (pickled models refer
    to them). Files whose imports fail (e.g. RDKit not installed) are skipped.
    Returns [(file, error)] for skipped files.
    """
    skipped = []
    for path in sorted(glob.glob(os.path.join(folder, "*.py"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem.startswith("_"):
            continue
        try:
            with node_api.collect_registrations() as registered:
                module = importlib.import_module(f"{package}.{stem}")
        except ImportError as e:
            skipped.append((path, str(e)))
            continue
        for cls in discover(module, registered):
            apply_defaults(cls, stem, prefix="verknode")
            if cls.factory in registry and registry[cls.factory] is not cls:
                raise ValueError(f"Duplicate node factory {cls.factory!r} in {path}")
            registry[cls.factory] = cls
    return skipped


class CustomNodes:
    """Custom node classes of one workflow (a registry overlay)."""

    def __init__(self):
        self.registry: dict[str, type] = {}
        self.errors: list[dict] = []
        self.loaded_from: list[str] = []
        self._state: dict = {}
        self._packages: list[str] = []

    # -- loading -------------------------------------------------------------
    def load(self, folders: list[str], builtin: dict) -> dict:
        self.unload()
        for folder in folders:
            if not os.path.isdir(folder):
                self.errors.append({"file": folder, "error": "Node folder not found"})
                continue
            self._load_folder(folder, builtin)
        self.loaded_from = list(folders)
        self._state = {f: _folder_state(f) for f in folders if os.path.isdir(f)}
        return self.report()

    def _load_folder(self, folder: str, builtin: dict):
        pkg = _package_name(folder)
        package = types.ModuleType(pkg)
        package.__path__ = [folder]
        package.__package__ = pkg
        sys.modules[pkg] = package
        _Finder.folders[pkg] = folder
        self._packages.append(pkg)
        version = _folder_hash(folder)
        for path in sorted(glob.glob(os.path.join(folder, "*.py"))):
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem.startswith("_"):
                continue
            module_name = f"{pkg}.{stem}"
            try:
                spec = _FINDER.find_spec(module_name)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                with node_api.collect_registrations() as registered:
                    spec.loader.exec_module(module)
                for cls in discover(module, registered):
                    self._add(cls, stem, path, version, builtin)
            except Exception:  # noqa: BLE001 - report any import error with its traceback
                sys.modules.pop(module_name, None)
                self.errors.append({"file": path, "error": traceback.format_exc()})

    def _add(self, cls, stem: str, path: str, version: str, builtin: dict):
        apply_defaults(cls, stem)
        cls._verknode_custom = True
        cls._verknode_source = path
        cls._verknode_hash = version
        if cls.factory in builtin:
            raise ValueError(f"{cls.__name__}: factory {cls.factory!r} is a built-in node")
        if cls.factory in self.registry:
            raise ValueError(f"{cls.__name__}: duplicate factory {cls.factory!r}")
        self.registry[cls.factory] = cls

    def unload(self):
        for pkg in self._packages:
            _Finder.folders.pop(pkg, None)
            for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
                del sys.modules[name]
        self._packages = []
        self.registry = {}
        self.errors = []

    # -- state -----------------------------------------------------------------
    def changed(self, folders: list[str]) -> bool:
        if list(folders) != self.loaded_from:
            return True
        return any(_folder_state(f) != self._state.get(f, {}) for f in folders if os.path.isdir(f))

    def report(self) -> dict:
        return {
            "loaded": [{"factory": f, "name": c.name, "category": c.category, "file": c._verknode_source}
                       for f, c in self.registry.items()],
            "errors": list(self.errors),
        }


def create_template(folder: str, filename: str = "example_nodes.py") -> str:
    """Create a node folder with an example node file (not overwriting)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(TEMPLATE)
    return path


class _ThreadStdout:
    """sys.stdout that each thread can send elsewhere (capture_stdout): nodes running at the same
    time (parallel chunks) capture their own print() output without swapping the process's stdout,
    which interleaved swaps would leave pointing at one node's buffer."""

    def __init__(self, base):
        import threading

        self._base = base
        self._local = threading.local()

    def _target(self):
        return getattr(self._local, "buf", None) or self._base

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


@contextlib.contextmanager
def capture_stdout(buf):
    """Send this thread's print() output (and other writes to sys.stdout) to `buf` meanwhile."""
    import sys

    if not isinstance(sys.stdout, _ThreadStdout):
        sys.stdout = _ThreadStdout(sys.stdout)
    proxy = sys.stdout
    previous = getattr(proxy._local, "buf", None)
    proxy._local.buf = buf
    try:
        yield buf
    finally:
        proxy._local.buf = previous


@contextlib.contextmanager
def discard_print():
    """Silence print() of custom node code during configure dry runs."""
    import io

    with capture_stdout(io.StringIO()):
        yield


@contextlib.contextmanager
def capture_print(ctx):
    """Send print() output of custom node code to the node's console log."""
    import io

    buf = io.StringIO()
    try:
        with capture_stdout(buf):
            yield
    finally:
        for line in buf.getvalue().splitlines():
            ctx.log(line)
