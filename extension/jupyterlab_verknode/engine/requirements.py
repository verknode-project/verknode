"""What a node needs: the Python packages its code imports beyond verknode's own (polars), read from
the code - the imports in the class, in the module's functions and in-module base classes it uses, and
those of its module (needed to load it at all). Imports in try / except ImportError are optional (the
node works without them, e.g. RDKit helpers of a Python Script).

requirements(cls) -> [{"package", "module", "installed", "optional"}] - in node descriptions (the editor
marks nodes whose packages are missing), docs/install.md and exports.
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import inspect
import sys

# import name -> package (pip) name, where they differ
PACKAGES = {"sklearn": "scikit-learn", "jsonpath_ng": "jsonpath-ng", "jupyter_client": "jupyter-client",
            "PIL": "pillow", "yaml": "pyyaml", "cv2": "opencv-python", "IPython": "ipython",
            "langchain_core": "langchain-core", "llama_cpp": "llama-cpp-python"}
CORE = {"polars", "jupyterlab_verknode", "verknode", "__future__"}


def _top(name: str) -> str:
    return (name or "").split(".")[0]


def _external(name: str) -> bool:
    top = _top(name)
    return bool(top) and top not in CORE and top not in sys.stdlib_module_names and not name.startswith(".")


@functools.lru_cache(maxsize=None)
def _module_tree(module_name: str):
    mod = sys.modules.get(module_name)
    try:
        return ast.parse(inspect.getsource(mod)) if mod else None
    except (OSError, TypeError, SyntaxError):
        return None


def _imports(node, optional=False):
    """(module, optional) of the imports under an AST node (in try / except ImportError: optional)."""
    out = []
    if isinstance(node, ast.Try):
        catches = {getattr(h.type, "id", None) or getattr(getattr(h.type, "attr", None), "__str__", lambda: None)()
                   for h in node.handlers}
        names = set()
        for h in node.handlers:
            t = h.type
            for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                names.add(getattr(e, "id", None) or getattr(e, "attr", None))
        opt = optional or bool(names & {"ImportError", "ModuleNotFoundError", "Exception", None}) or not catches
        for child in node.body:
            out += _imports(child, opt)
        for child in node.handlers + node.orelse + node.finalbody:
            out += _imports(child, optional)
        return out
    if isinstance(node, ast.Import):
        return [(a.name, optional) for a in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level:
        return [(node.module or "", optional)]
    for child in ast.iter_child_nodes(node):
        out += _imports(child, optional)
    return out


def _names(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


@functools.lru_cache(maxsize=None)
def _analyze(cls) -> tuple:
    tree = _module_tree(cls.__module__)
    if tree is None:
        return ()
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))}
    found: dict[str, bool] = {}

    def add(items):
        for name, opt in items:
            if _external(name):
                top = _top(name)
                found[top] = found.get(top, True) and opt  # (required if any import of it is)

    # the module's own imports (it does not load without them)
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Try, ast.If)):
            add(_imports(stmt))
    # the class, the functions / classes of the module it uses, its in-module bases
    todo, seen = [cls.__name__] + [b.__name__ for b in cls.__mro__[1:] if b.__module__ == cls.__module__], set()
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        add(_imports(defs[name]))
        # (verknode.node.chem is RDKit, None without it: code that uses chem.<...> uses RDKit for molecules -
        # optional, as there are no molecules without it; nodes that make molecules say requires = ["rdkit"])
        if any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "chem"
               for n in ast.walk(defs[name])):
            add([("rdkit", True)])
        todo += [n for n in _names(defs[name]) if n in defs and n not in seen]
    return tuple(sorted(found.items()))


def _installed(package: str) -> bool:
    import importlib.metadata

    try:
        importlib.metadata.distribution(package)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def missing(cls) -> str | None:
    """Why the node cannot run here (its packages are not installed), or None."""
    need = [r["package"] for r in requirements(cls) if not r["installed"] and not r["optional"]]
    if not need:
        return None
    return f"{cls.name} needs {', '.join(need)}, not installed here (pip install {' '.join(need)})"


def requirements(cls) -> list[dict]:
    """The packages a node class needs ({package, module, optional, installed}); a class can also list
    packages its code does not show (requires: pip names, e.g. a notebook's kernel)."""
    out = {}
    for module, optional in _analyze(cls):
        out[module] = {"package": PACKAGES.get(module, module), "module": module, "optional": optional,
                       "installed": importlib.util.find_spec(module) is not None}
    for package in getattr(cls, "requires", ()) or ():
        module = next((m for m, p in PACKAGES.items() if p == package), package.replace("-", "_"))
        out[module] = {"package": package, "module": module, "optional": False, "installed": _installed(package)}
    return sorted(out.values(), key=lambda r: (r["optional"], r["package"].lower()))
