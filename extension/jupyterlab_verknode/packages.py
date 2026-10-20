"""verknode's package tool (docs/packages.md):

    python -m jupyterlab_verknode.packages list                 # installed packages
    python -m jupyterlab_verknode.packages available            # the index's, and their versions for this verknode
    python -m jupyterlab_verknode.packages install NAME|SPEC    # from the index, or a pip requirement
    python -m jupyterlab_verknode.packages uninstall NAME
    python -m jupyterlab_verknode.packages update [NAME ...]    # to the newest versions that fit this verknode
    python -m jupyterlab_verknode.packages api                  # this verknode's node API version
    python -m jupyterlab_verknode.packages new NAME [--dir D]   # a package project to start from
    python -m jupyterlab_verknode.packages validate [PATH]      # the checks verknode's CI runs on packages
    python -m jupyterlab_verknode.packages bump-api [PATH]      # widen a package's API range to this verknode's
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap

from .engine import packages as pk


# ---------------------------------------------------------------------------------------------------
# validate: what a package must do
# ---------------------------------------------------------------------------------------------------
def _pyproject(path: str) -> dict:
    import tomllib

    with open(os.path.join(path, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)


def validate(path: str = ".") -> int:
    """Check a package project (or an installed package by name): problems are errors, advice warnings."""
    import importlib

    errors, warnings = [], []
    path = os.path.abspath(path)
    meta = _pyproject(path) if os.path.isfile(os.path.join(path, "pyproject.toml")) else None
    eps = {}
    if meta:
        eps = meta.get("project", {}).get("entry-points", {}).get(pk.GROUP, {})
        if not eps:
            errors.append(f'pyproject.toml: no entry point in [project.entry-points."{pk.GROUP}"]')
        tool_api = meta.get("tool", {}).get("verknode", {}).get("api")
        if not tool_api:
            errors.append("pyproject.toml: no [tool.verknode] api (the node API versions it works with)")
        for d in ("src", ""):
            if os.path.isdir(os.path.join(path, d)) and os.path.join(path, d) not in sys.path:
                sys.path.insert(0, os.path.join(path, d))
    else:
        tool_api = None
        rec = pk.find(os.path.basename(path)) or pk.find(path)
        if rec is None:
            errors.append(f"No package project (pyproject.toml) at {path}, and no installed package of that name")
        else:
            eps = {rec.entry_point: f"{os.path.basename(rec.package.root)}:package"} if rec.package else {}
    from .engine.node import NODE_REGISTRY, NodeModel
    from .engine.custom import apply_defaults, discover
    from .engine import node as node_api

    for ep_name, target in eps.items():
        mod_name, _, attr = target.partition(":")
        try:
            mod = importlib.import_module(mod_name)
            pkg = getattr(mod, attr or "package")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{target}: cannot be imported ({e})")
            continue
        if not isinstance(pkg, pk.Package):
            errors.append(f"{target} is not a verknode Package")
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", pkg.name):
            errors.append(f"Package name {pkg.name!r}: lower case letters, digits and _ only")
        if ep_name != pkg.name:
            warnings.append(f"The entry point is named {ep_name!r}, the package {pkg.name!r}: name them the same")
        if not pk.api_compatible(pkg.api):
            errors.append(f"api {pkg.api!r} does not include this verknode's node API {pk.API_VERSION} "
                          "(bump-api widens it once the package works with it)")
        if tool_api and tool_api != pkg.api:
            errors.append(f"[tool.verknode] api {tool_api!r} and the Package's api {pkg.api!r} differ")
        version = (meta or {}).get("project", {}).get("version")
        if version and pkg.version not in (version, "0.0.0"):
            warnings.append(f"Package version {pkg.version} is not the project's {version} (install it again)")
        pkg.root = os.path.dirname(mod.__file__)
        if pkg.static and not os.path.isdir(pkg.static_dir()):
            errors.append(f"static folder {pkg.static!r} is not there")
        factories, n = {}, 0
        modules = pkg.modules or pk._module_names(pkg, mod_name.split(".")[0])
        for m in modules:
            try:
                with node_api.collect_registrations() as registered:
                    module = importlib.import_module(m)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{m}: cannot be imported ({e})")
                continue
            for cls in discover(module, registered):
                apply_defaults(cls, m.rsplit(".", 1)[-1], prefix=f"pkg.{pkg.name}")
                n += 1
                where = f"{cls.__module__}.{cls.__name__}"
                if not cls.__dict__.get("factory"):
                    warnings.append(f"{where}: set factory (e.g. '{pkg.name}.{cls.__name__}'): saved workflows refer "
                                    "to nodes by it, and the default changes when the class moves")
                elif not cls.factory.startswith(f"{pkg.name}."):
                    warnings.append(f"{where}: factory {cls.factory!r} - start it with '{pkg.name}.'")
                if cls.factory in factories:
                    errors.append(f"{where}: factory {cls.factory!r} is used twice")
                if cls.factory in NODE_REGISTRY and getattr(NODE_REGISTRY[cls.factory], "_verknode_package", None) \
                        != pkg.name:
                    errors.append(f"{where}: factory {cls.factory!r} is verknode's or another package's")
                factories[cls.factory] = cls
                if not isinstance(getattr(cls, "version", 1), int) or cls.version < 1:
                    errors.append(f"{where}: version must be a whole number from 1")
                if cls.version > 1 and cls.migrate is NodeModel.migrate:
                    warnings.append(f"{where}: version {cls.version} without migrate(): settings of older workflows "
                                    "are taken as they are")
                try:
                    json.dumps(cls.describe())
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{where}: describe() is not JSON ({e})")
        if n == 0:
            errors.append(f"{pkg.name}: no nodes in {', '.join(modules)}")
        print(f"{pkg.title or pkg.name} {pkg.version}: {n} nodes, node API {pkg.api} (this verknode: {pk.API_VERSION})")
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("error:", e)
    print("OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------------------------------------------
# bump-api: a package made to work with a new major version of the node API (CI does this by itself)
# ---------------------------------------------------------------------------------------------------
def bump_api(path: str = ".") -> int:
    """Widen [tool.verknode] api and the API constant of the package's __init__ to include this verknode's
    node API (the upper bound to the next major version). Prints the new range; no change: exit code 2."""
    major = int(pk.API_VERSION.split(".")[0])
    meta = _pyproject(path)
    old = meta.get("tool", {}).get("verknode", {}).get("api", "")
    if pk.api_compatible(old):
        print(f"{old} already includes {pk.API_VERSION}")
        return 2
    lower = re.search(r">=\s*([\d.]+)", old)
    new = f">={lower.group(1) if lower else '1.0'},<{major + 1}"
    files = [os.path.join(path, "pyproject.toml")]
    for root, _, names in os.walk(os.path.join(path, "src") if os.path.isdir(os.path.join(path, "src")) else path):
        files += [os.path.join(root, n) for n in names if n == "__init__.py"]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        if old and old in text:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(text.replace(f'"{old}"', f'"{new}"'))
    print(new)
    return 0


# ---------------------------------------------------------------------------------------------------
# new: a package project
# ---------------------------------------------------------------------------------------------------
def _ws(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def new(name: str, folder: str | None = None, author: str = "") -> str:
    short = re.sub(r"[^a-z0-9_]", "_", name.lower().removeprefix("verknode-").removeprefix("verknode_"))
    dist = f"verknode-{short.replace('_', '-')}"
    mod = f"verknode_{short}"
    root = os.path.abspath(folder or dist)
    api = f">={pk.API_VERSION.split('.')[0]}.0,<{int(pk.API_VERSION.split('.')[0]) + 1}"
    files = {
        "pyproject.toml": f'''
            [build-system]
            requires = ["hatchling>=1.24"]
            build-backend = "hatchling.build"

            [project]
            name = "{dist}"
            version = "0.1.0"
            description = "{short} nodes for verknode"
            readme = "README.md"
            license = "Apache-2.0"
            requires-python = ">=3.10"
            authors = [{{ name = "{author}" }}]
            dependencies = ["polars>=1.0"]

            [project.optional-dependencies]
            test = ["pytest"]

            # verknode finds the package by this entry point
            [project.entry-points."verknode.packages"]
            {short} = "{mod}:package"

            # the versions of verknode's node API it works with (the same as API in {mod}/__init__.py)
            [tool.verknode]
            api = "{api}"

            [tool.hatch.build.targets.wheel]
            packages = ["src/{mod}"]
        ''',
        f"src/{mod}/__init__.py": f'''
            """{dist}: a verknode package."""
            from importlib.metadata import PackageNotFoundError, version as _version

            from verknode.package import Package

            try:
                __version__ = _version("{dist}")
            except PackageNotFoundError:
                __version__ = "0.0.0"

            API = "{api}"  # (also [tool.verknode] api in pyproject.toml)

            package = Package(name="{short}", title="{short.replace('_', ' ').title()}", version=__version__, api=API,
                              modules=["{mod}.nodes"], static=None)
        ''',
        f"src/{mod}/nodes.py": f'''
            """The nodes of the package: every NodeModel subclass here is a node (help(verknode.node))."""
            import polars as pl

            from verknode.node import F, NodeModel, Port, TABLE


            class Double(NodeModel):
                factory = "{short}.Double"  # stable: saved workflows refer to the node by it
                name = "Double Column"
                category = "{short.replace('_', ' ').title()}"
                version = 1  # raise it when the settings change, and write migrate()
                in_ports = [Port(TABLE, "Input table")]
                out_ports = [Port(TABLE, "Output table")]
                fields = [F("column", "column", "Column")]

                def execute(self, inputs, s, ctx):
                    return [inputs[0].with_columns(pl.col(s["column"]) * 2)]
        ''',
        "tests/test_nodes.py": f'''
            import polars as pl

            from verknode.node import ExecContext
            from {mod}.nodes import Double


            def test_double():
                out = Double().execute([pl.DataFrame({{"a": [1, 2]}})], {{"column": "a"}}, ExecContext({{}}, "."))[0]
                assert out["a"].to_list() == [2, 4]
        ''',
        "README.md": f'''
            # {dist}

            A [verknode](https://github.com/verknode-project/verknode) package.

            Install it in verknode: Settings › Packages, or

                python -m jupyterlab_verknode.packages install "{dist} @ git+https://github.com/OWNER/{dist}"

            Release: push a tag `vX.Y.Z` (the release workflow builds the wheel); verknode's index picks new
            releases up by itself once the package is listed in it (packages/index.json, by a pull request).
        ''',
        ".github/workflows/ci.yml": '''
            # verknode's checks of a package, against the newest verknode (and kept up to date by verknode)
            name: CI
            on: [push, pull_request, workflow_dispatch]
            jobs:
              check:
                uses: verknode-project/verknode/.github/workflows/package-check.yml@main
        ''',
        ".github/workflows/compat.yml": '''
            # every week: does the package work with the newest verknode? If a new node API came out and the
            # checks pass with it, a pull request widens the package's API range (automated updates)
            name: Compatibility
            on:
              schedule: [{cron: "17 4 * * 1"}]
              workflow_dispatch:
            permissions: {contents: write, pull-requests: write}
            jobs:
              compat:
                uses: verknode-project/verknode/.github/workflows/package-compat.yml@main
        ''',
        ".github/workflows/release.yml": '''
            # a tag vX.Y.Z: the wheel, on a GitHub release (verknode's index finds it there)
            name: Release
            on: {push: {tags: ["v*"]}}
            permissions: {contents: write}
            jobs:
              release:
                uses: verknode-project/verknode/.github/workflows/package-release.yml@main
        ''',
        ".gitignore": "__pycache__/\n*.egg-info/\ndist/\nbuild/\n.pytest_cache/\n",
    }
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(_ws(text))
    print(f"Made {root}: pip install -e '{root}[test]', then restart Jupyter (or Settings › Packages)")
    return root


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m jupyterlab_verknode.packages", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("available")
    sub.add_parser("api")
    i = sub.add_parser("install")
    i.add_argument("name")
    u = sub.add_parser("uninstall")
    u.add_argument("name")
    up = sub.add_parser("update")
    up.add_argument("names", nargs="*")
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--dir")
    n.add_argument("--author", default="")
    v = sub.add_parser("validate")
    v.add_argument("path", nargs="?", default=".")
    b = sub.add_parser("bump-api")
    b.add_argument("path", nargs="?", default=".")
    a = p.parse_args(argv)
    if a.cmd == "api":
        print(pk.API_VERSION)
        return 0
    if a.cmd == "new":
        new(a.name, a.dir, a.author)
        return 0
    if a.cmd == "bump-api":
        return bump_api(a.path)
    from . import nodes  # noqa: F401 - the standard nodes and the installed packages

    if a.cmd == "validate":
        return validate(a.path)
    if a.cmd == "list":
        for r in pk.LOADED.values():
            j = r.to_json()
            print(f"{j['name']:20} {j['version']:10} {j['status']:12} {j['nodes']:3} nodes  {j['error'][:80]}")
        return 0
    if a.cmd == "available":
        st = pk.status(refresh=True)
        for e in st["available"]:
            print(f"{e['name']:20} {e['latest'] or '(none for this verknode)':12} {'installed' if e['installed'] else '':10}"
                  f" {e['title']}")
        if st["indexError"]:
            print("index:", st["indexError"])
        return 0
    if a.cmd == "install":
        pk.install(a.name)
    elif a.cmd == "uninstall":
        pk.uninstall(a.name)
    elif a.cmd == "update":
        pk.update(a.names or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
