"""verknode packages: sets of nodes, port types, canvas widgets, views and node defaults, distributed as
ordinary Python packages (docs/packages.md).

A package is a Python distribution with an entry point in the group ``verknode.packages`` that names its
``Package`` object::

    # pyproject.toml
    [project.entry-points."verknode.packages"]
    printing3d = "verknode_printing3d:package"

    # verknode_printing3d/__init__.py
    from verknode.package import Package
    package = Package(name="printing3d", title="3D Printing", version=__version__, api=">=1.0,<2",
                      modules=["verknode_printing3d.mesh_nodes"], static="static")

Every NodeModel subclass of its modules becomes a node (as in a node folder). Packages load when the
Jupyter server starts, and when one is installed - without a restart.

Versions:
- ``API_VERSION`` is the version of verknode's node API (verknode.node, verknode.package, widgets, views):
  a minor version adds, a major version may break. A package says which API versions it works with
  (``api``); one that does not match is not loaded, and says why.
- A node's ``version`` and ``migrate(settings, from_version)`` update the settings saved in workflows
  when a package changes a node (engine/persistence.py).

Installing: packages go into their own Python environment in ~/.verknode/packages (it sees the Jupyter
server's packages, so only what is missing is installed; it survives a new container as ~/.verknode does,
and is rebuilt by itself, with its packages, when the server's Python changes). The index - a JSON list of
packages and their versions, by default verknode's own (packages/index.json) - offers the versions that fit
this verknode's API. Updates: the policy (Settings › Node defaults › Packages) is to notify of updates, or
to install the compatible ones by themselves when the server starts.

    python -m jupyterlab_verknode.packages list | available | install NAME | uninstall NAME | update [NAME]
    python -m jupyterlab_verknode.packages new my-nodes        # a package project to start from
    python -m jupyterlab_verknode.packages validate [PATH]     # what verknode's checks check
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from dataclasses import dataclass, field

API_VERSION = "1.2"  # (1.1: Package.examples; 1.2: path kinds and extensions of fields)
GROUP = "verknode.packages"
INDEX_URL = "https://raw.githubusercontent.com/verknode-project/verknode/main/packages/index.json"
WASM = sys.platform == "emscripten"


# ---------------------------------------------------------------------------------------------------
# what a package says about itself
# ---------------------------------------------------------------------------------------------------
@dataclass
class Package:
    """A verknode package (verknode.package.Package)."""

    name: str  # short id: a-z, 0-9, _ (e.g. "printing3d"): URLs, factories, the index
    title: str = ""
    version: str = "0"
    api: str = f">={API_VERSION.split('.')[0]}.0,<{int(API_VERSION.split('.')[0]) + 1}"
    description: str = ""
    modules: list = field(default_factory=list)  # node modules (default: the package's "nodes" subpackage)
    static: str | None = None  # a folder, served at verknode/packages/<name>/ (relative: to the package)
    homepage: str = ""
    examples: str | None = None  # a folder of example workflows (and their data), relative to the package (API 1.1)
    root: str = ""  # (set when loaded: the package's folder)

    def static_url(self, path: str) -> str:
        """The URL of a static file in pages shown in views and widgets (the server's base is put in)."""
        return f"__VERKNODE_BASE__verknode/packages/{self.name}/{path.lstrip('/')}"

    def examples_dir(self) -> str | None:
        if not self.examples:
            return None
        d = self.examples if os.path.isabs(self.examples) else os.path.join(self.root, self.examples)
        return d if os.path.isdir(d) else None

    def static_dir(self) -> str | None:
        if not self.static:
            return None
        return self.static if os.path.isabs(self.static) else os.path.join(self.root, self.static)


def api_compatible(spec: str, version: str = API_VERSION) -> bool:
    """Is the node API `version` in the package's specifier (PEP 440, e.g. ">=1.0,<2")?"""
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        return Version(version) in SpecifierSet(spec or "")
    except ImportError:  # (no packaging: the major version)
        return f"<{int(version.split('.')[0]) + 1}" in (spec or "") or not spec
    except Exception:  # noqa: BLE001 - a broken specifier: not compatible
        return False


def _vkey(v: str):
    try:
        from packaging.version import Version

        return Version(v)
    except Exception:  # noqa: BLE001
        return tuple(int(p) if p.isdigit() else 0 for p in str(v).split("."))


# ---------------------------------------------------------------------------------------------------
# loading into the node registry
# ---------------------------------------------------------------------------------------------------
@dataclass
class Loaded:
    package: Package | None
    dist: str  # the distribution's name (pip)
    dist_version: str
    entry_point: str
    factories: list = field(default_factory=list)
    port_types: list = field(default_factory=list)
    status: str = "loaded"  # loaded | incompatible | error
    error: str = ""

    def to_json(self) -> dict:
        p = self.package
        return {"name": p.name if p else self.entry_point, "title": (p.title or p.name) if p else self.entry_point,
                "version": p.version if p else self.dist_version, "dist": self.dist, "distVersion": self.dist_version,
                "api": p.api if p else "", "description": p.description if p else "",
                "homepage": p.homepage if p else "", "status": self.status, "error": self.error,
                "nodes": len(self.factories), "portTypes": list(self.port_types),
                "examples": bool(p and self.status == "loaded" and p.examples_dir())}


LOADED: dict[str, Loaded] = {}
_lock = threading.RLock()


def _module_names(pkg: Package, top: str) -> list[str]:
    if pkg.modules:
        return list(pkg.modules)
    try:
        nodes = importlib.import_module(f"{top}.nodes")
    except ImportError:
        return [top]
    import pkgutil

    return [f"{top}.nodes.{m.name}" for m in pkgutil.iter_modules(nodes.__path__) if not m.name.startswith("_")]


def _load(ep, registry: dict) -> Loaded:
    from . import node as node_api
    from .custom import apply_defaults, discover

    dist = getattr(ep, "dist", None)
    rec = Loaded(None, dist.metadata["Name"] if dist else "", dist.version if dist else "", ep.name)
    try:
        pkg = ep.load()
        if not isinstance(pkg, Package):
            raise TypeError(f"{ep.value} is not a verknode Package")
        top = ep.value.split(":")[0].split(".")[0]
        pkg.root = os.path.dirname(importlib.import_module(top).__file__)
        rec.package = pkg
        if not api_compatible(pkg.api):
            rec.status = "incompatible"
            rec.error = f"made for verknode's node API {pkg.api}; this verknode has {API_VERSION}"
            return rec
        before = set(node_api.PORT_TYPES)
        classes = []
        for mod_name in _module_names(pkg, top):
            with node_api.collect_registrations() as registered:
                module = importlib.import_module(mod_name)
            for cls in discover(module, registered):
                apply_defaults(cls, mod_name.rsplit(".", 1)[-1], prefix=f"pkg.{pkg.name}")
                classes.append(cls)
        for cls in classes:
            if cls.factory in registry and getattr(registry[cls.factory], "_verknode_package", None) != pkg.name:
                raise ValueError(f"{cls.__name__}: the factory {cls.factory!r} is taken")
        for cls in classes:
            cls._verknode_package = pkg.name
            cls._verknode_package_version = pkg.version
            cls._verknode_hash = f"pkg:{pkg.name}=={pkg.version}"  # (results of another version are not kept)
            registry[cls.factory] = cls
            rec.factories.append(cls.factory)
        rec.port_types = sorted(set(node_api.PORT_TYPES) - before)
    except Exception:  # noqa: BLE001 - one broken package must not stop the others
        rec.status = "error"
        rec.error = traceback.format_exc(limit=6)
    return rec


def _entry_points():
    try:
        return list(importlib.metadata.entry_points(group=GROUP))
    except Exception:  # noqa: BLE001
        return []


def load_installed(registry: dict | None = None) -> dict[str, Loaded]:
    """Load the installed packages that are not loaded yet (at start, and after an install)."""
    from .node import NODE_REGISTRY

    registry = NODE_REGISTRY if registry is None else registry
    with _lock:
        activate()
        importlib.invalidate_caches()
        for ep in _entry_points():
            if any(r.entry_point == ep.name for r in LOADED.values()):
                continue
            rec = _load(ep, registry)
            LOADED[rec.package.name if rec.package else ep.name] = rec
        return dict(LOADED)


def unload(name: str, registry: dict | None = None):
    """Take a package's nodes out of the registry and forget its modules (for an update or uninstall)."""
    from .node import NODE_REGISTRY, PORT_TYPES

    registry = NODE_REGISTRY if registry is None else registry
    with _lock:
        rec = LOADED.pop(name, None)
        if rec is None:
            return
        for f in rec.factories:
            registry.pop(f, None)
        for t in rec.port_types:
            PORT_TYPES.pop(t, None)
        top = rec.entry_point if rec.package is None else None
        mods = {m.split(".")[0] for m in (rec.package.modules if rec.package else [])}
        if rec.package is not None:
            mods.add(os.path.basename(rec.package.root))
        for m in list(sys.modules):
            if m.split(".")[0] in mods or (top and m.split(".")[0] == top):
                del sys.modules[m]


def copy_examples(name: str, root_dir: str, folder: str = "verknode-examples") -> dict:
    """The package's example workflows, copied into the workspace (root_dir/verknode-examples/<name>/): files
    already there are kept (they may have been changed); the workflows' paths, relative to root_dir."""
    import shutil

    rec = LOADED.get(name)
    src = rec.package.examples_dir() if rec and rec.package else None
    if not src:
        raise ValueError(f"{name} has no examples")
    dest = os.path.join(root_dir, folder, name)
    for d, _, files in os.walk(src):
        rel = os.path.relpath(d, src)
        if any(part.startswith((".", "__")) or part == "output" for part in rel.split(os.sep) if part != "."):
            continue
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for f in files:
            target = os.path.join(dest, rel, f)
            if not os.path.exists(target) and not f.endswith((".pyc",)):
                shutil.copy2(os.path.join(d, f), target)
    workflows = sorted(os.path.relpath(os.path.join(d, f), root_dir).replace(os.sep, "/")
                       for d, _, files in os.walk(dest) for f in files if f.endswith(".vkf"))
    return {"folder": os.path.relpath(dest, root_dir).replace(os.sep, "/"), "workflows": workflows}


def find(name: str) -> Loaded | None:
    return LOADED.get(name)


def static_dir(name: str) -> str | None:
    rec = LOADED.get(name)
    return rec.package.static_dir() if rec and rec.package else None


# ---------------------------------------------------------------------------------------------------
# the packages' own environment (~/.verknode/packages)
# ---------------------------------------------------------------------------------------------------
def env_dir() -> str:
    from .home import home

    return os.environ.get("VERKNODE_PACKAGES_ENV", "").strip() or os.path.join(home(), "packages")


def _env_python() -> str:
    d = env_dir()
    return os.path.join(d, "Scripts", "python.exe") if os.name == "nt" else os.path.join(d, "bin", "python")


def _site_packages() -> str | None:
    d = env_dir()
    if os.name == "nt":
        p = os.path.join(d, "Lib", "site-packages")
        return p if os.path.isdir(p) else None
    lib = os.path.join(d, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
    return lib if os.path.isdir(lib) else None


def _manifest_path() -> str:
    return os.path.join(env_dir(), "verknode-packages.json")


def manifest() -> dict:
    try:
        with open(_manifest_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"python": "", "installed": {}}


def _save_manifest(m: dict):
    os.makedirs(env_dir(), exist_ok=True)
    tmp = _manifest_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2)
    os.replace(tmp, _manifest_path())


def _python_id() -> str:
    # (the interpreter itself: python, python3.13 and a symlink of them are the same Python)
    return f"{sys.version_info.major}.{sys.version_info.minor} {os.path.realpath(sys.executable)}"


def activate():
    """Put the environment's site-packages on sys.path (after the server's own: those win)."""
    if WASM:
        return
    sp = _site_packages()
    if sp and sp not in sys.path:
        sys.path.append(sp)


def ensure_env(log=print) -> str:
    """The environment's Python; made (or made again, with its packages, for a new Python) if needed."""
    m = manifest()
    if os.path.exists(_env_python()) and m.get("python") == _python_id():
        return _env_python()
    reinstall = list(m.get("installed", {}).values()) if m.get("python") else []
    if os.path.exists(env_dir()) and m.get("python"):
        log(f"The server's Python changed ({m.get('python')} -> {_python_id()}): making the environment again")
        import shutil

        for entry in os.listdir(env_dir()):
            if entry != "verknode-packages.json":
                shutil.rmtree(os.path.join(env_dir(), entry), ignore_errors=True)
    log(f"Making the package environment in {env_dir()}")
    _run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", env_dir()], log)
    m["python"] = _python_id()
    _save_manifest(m)
    if reinstall:
        _pip(["install", *reinstall], log)
    activate()
    return _env_python()


def _run(cmd: list, log) -> int:
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        log(line.rstrip())
    rc = p.wait()
    if rc:
        raise RuntimeError(f"{os.path.basename(cmd[0])} {cmd[1] if len(cmd) > 1 else ''} failed ({rc})")
    return rc


def _pip(args: list, log):
    """The server's pip, working on the environment (--python): what the server has already counts as
    installed, so only what is missing goes into the environment."""
    return _run([sys.executable, "-m", "pip", "--disable-pip-version-check", "--no-input", "--python", _env_python(),
                 *args], log)


# ---------------------------------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------------------------------
def _settings() -> dict:
    from .defaults import node_defaults

    return node_defaults("packages")


def index_urls() -> list[str]:
    urls = [u.strip() for u in (_settings().get("index_urls") or "").splitlines() if u.strip()]
    return urls or [INDEX_URL]


_index_cache: dict = {"time": 0.0, "data": None, "error": ""}


def index(refresh: bool = False) -> tuple[list[dict], str]:
    """The packages of the indexes ({name, dist, title, description, homepage, versions: [...]}), error."""
    if not refresh and _index_cache["data"] is not None and time.time() - _index_cache["time"] < 600:
        return _index_cache["data"], _index_cache["error"]
    out, errors = {}, []
    for url in index_urls():
        try:
            if url.startswith(("http://", "https://")):
                req = urllib.request.Request(url, headers={"User-Agent": "verknode-packages"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
            else:
                with open(os.path.expanduser(url), encoding="utf-8") as fh:
                    data = json.load(fh)
            for p in data.get("packages", []):
                out.setdefault(p["name"], p)
        except Exception as e:  # noqa: BLE001 - an unreachable index: the others still count
            errors.append(f"{url}: {e}")
    _index_cache.update(time=time.time(), data=list(out.values()), error="; ".join(errors))
    return _index_cache["data"], _index_cache["error"]


def best_version(entry: dict) -> dict | None:
    """The newest version of an index entry that fits this verknode's node API."""
    ok = [v for v in entry.get("versions", []) if api_compatible(v.get("api", ""))]
    return max(ok, key=lambda v: _vkey(v["version"])) if ok else None


# ---------------------------------------------------------------------------------------------------
# install, uninstall, update - as jobs
# ---------------------------------------------------------------------------------------------------
JOBS: dict[str, dict] = {}
ON_CHANGE: list = []  # callbacks after packages changed (the server refreshes its open workflows)


def _changed():
    for fn in ON_CHANGE:
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def install(name_or_spec: str, log=print) -> str:
    """Install a package of the index (by name: its newest compatible version) or a pip requirement."""
    entry = next((p for p in index()[0] if p["name"] == name_or_spec), None)
    if entry is not None:
        v = best_version(entry)
        if v is None:
            raise RuntimeError(f"No version of {name_or_spec} fits this verknode (node API {API_VERSION})")
        spec, name = v["install"], entry["name"]
    else:
        spec, name = name_or_spec, None
    ensure_env(log)
    before = _env_dists()
    _pip(["install", "--upgrade", spec], log)
    importlib.invalidate_caches()
    changed = {d for d, v in _env_dists().items() if before.get(d) != v}
    # the verknode packages among what changed: an update replaces the loaded code
    names = [ep.name for ep in _entry_points() if getattr(ep, "dist", None) is not None
             and _canon(ep.dist.metadata["Name"] or "") in changed]
    names += [n for n, r in LOADED.items() if r.dist and _canon(r.dist) in changed]
    for n in set(names + ([name] if name else [])):
        unload(n)
    loaded_before = set(LOADED)
    load_installed()
    added = sorted(set(LOADED) - loaded_before)
    same = [n for n, r in LOADED.items() if r.dist and _canon(r.dist) in _canon(spec)]  # (already there)
    new = name or (names[0] if names else added[0] if added else same[0] if same else spec)
    m = manifest()
    if new in LOADED:
        m.setdefault("installed", {})[new] = spec
        _save_manifest(m)
    rec = LOADED.get(new)
    if rec is not None:
        j = rec.to_json()
        log(f"{j['title']} {j['version']}: {rec.status} ({len(rec.factories)} nodes)"
            + (" - " + rec.error.strip().splitlines()[-1] if rec.error else ""))
    else:
        log(f"Installed {spec}, but it has no verknode package (no {GROUP} entry point)")
    _changed()
    return new


def _canon(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _env_dists() -> dict:
    """The distributions in the environment's own site-packages: {name: version}."""
    sp = _site_packages()
    if not sp:
        return {}
    return {_canon(d.metadata["Name"]): d.version for d in importlib.metadata.distributions(path=[sp])}


def uninstall(name: str, log=print):
    rec = LOADED.get(name)
    dist = rec.dist if rec else name
    if not dist:
        raise RuntimeError(f"Not an installed package: {name}")
    if not os.path.exists(_env_python()):
        raise RuntimeError("Packages installed elsewhere (not by verknode) are uninstalled with pip")
    if _canon(dist) not in _env_dists():
        raise RuntimeError(f"{name} was not installed by verknode (it is in the server's own environment): use pip")
    _pip(["uninstall", "-y", dist], log)
    unload(name)
    m = manifest()
    for key in [k for k, v in m.get("installed", {}).items() if k == name or _canon(dist) in _canon(str(v))]:
        m["installed"].pop(key, None)
    _save_manifest(m)
    importlib.invalidate_caches()
    _changed()
    log(f"Uninstalled {name}")


def updates() -> list[dict]:
    """Installed packages with a newer compatible version in the index."""
    out = []
    by_name = {p["name"]: p for p in index()[0]}
    for name, rec in LOADED.items():
        entry = by_name.get(name)
        best = best_version(entry) if entry else None
        current = rec.package.version if rec.package else rec.dist_version
        if best and _vkey(best["version"]) > _vkey(current):
            out.append({"name": name, "current": current, "latest": best["version"]})
    return out


def update(names: list[str] | None = None, log=print) -> list[str]:
    done = []
    for u in updates():
        if names is None or u["name"] in names:
            install(u["name"], log)
            done.append(f"{u['name']} {u['current']} -> {u['latest']}")
    if not done:
        log("Everything is up to date")
    return done


def start_job(kind: str, fn, *args) -> str:
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "kind": kind, "args": list(args), "state": "running", "log": [], "started": time.time(),
           "result": None}
    JOBS[jid] = job

    def run():
        try:
            job["result"] = fn(*args, log=lambda line: job["log"].append(str(line)))
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            job["log"].append(f"ERROR: {e}")
            job["state"] = "failed"

    threading.Thread(target=run, daemon=True, name=f"verknode-packages-{kind}").start()
    return jid


def status(refresh: bool = False) -> dict:
    """What the package manager shows: installed, available (index), updates, policy."""
    entries, error = index(refresh)
    installed = [r.to_json() for r in LOADED.values()]
    mine = _env_dists()
    for p in installed:  # (in verknode's own environment: verknode can uninstall it)
        p["managed"] = _canon(p["dist"] or "") in mine
    have = {p["name"] for p in installed}
    available = []
    for e in entries:
        best = best_version(e)
        available.append({"name": e["name"], "title": e.get("title") or e["name"], "description": e.get("description", ""),
                          "homepage": e.get("homepage", ""), "installed": e["name"] in have,
                          "latest": best["version"] if best else None,
                          "newest": max((v["version"] for v in e.get("versions", [])), key=_vkey, default=None)})
    return {"api": API_VERSION, "installed": installed, "available": available, "updates": updates(),
            "indexError": error, "indexUrls": index_urls(), "policy": _settings().get("auto_update", "Notify"),
            "env": env_dir(), "server": not WASM,
            "jobs": [{k: v for k, v in j.items() if k != "log"} | {"log": j["log"][-200:]} for j in JOBS.values()]}


def auto_update(log=print):
    """At the server's start, by the policy: install compatible updates, or only tell of them."""
    policy = _settings().get("auto_update", "Notify")
    if policy == "Off":
        return
    try:
        found = updates()
    except Exception as e:  # noqa: BLE001
        log(f"verknode packages: no update check ({e})")
        return
    if not found:
        return
    if policy == "Install compatible updates":
        start_job("update", update, None)
    else:
        log("verknode packages: updates available - " + ", ".join(f"{u['name']} {u['latest']}" for u in found))


def register_defaults_section():
    from .defaults import register_defaults
    from .node import F

    register_defaults("packages", "Packages", [
        F("auto_update", "enum", "Updates", "Notify", "When the Jupyter server starts: tell of updates of your "
          "packages (Settings › Packages), install the versions that fit this verknode by themselves, or do "
          "nothing.", options=["Notify", "Install compatible updates", "Off"]),
        F("index_urls", "code", "Package indexes", "", f"One URL (or file) per line. Empty: verknode's index, "
          f"{INDEX_URL}."),
    ], description="Where packages come from, and how they are kept up to date.", order=20)


register_defaults_section()
