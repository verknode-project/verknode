"""Bundle Pyodide into the JupyterLite site, so it loads nothing from a CDN (networks that block
cdn.jsdelivr.net): Pyodide's core runtime and the packages the kernel and verknode load - with their
dependencies - into lite/static/pyodide, where jupyterlite-pyodide-kernel serves Pyodide from.

    python lite/bundle_pyodide.py 0.27.6 lite/static/pyodide            # the packages below
    python lite/bundle_pyodide.py 0.27.6 lite/static/pyodide --dry-run  # only list them, with sizes

The lock file is cut to the bundled packages, so a package that is not there fails at once (with the
PyPI fallback off, see jupyter_lite_config.json) instead of waiting for a CDN.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import tarfile
import urllib.request

# what the Pyodide kernel installs when it starts (jupyterlite-pyodide-kernel's worker)
KERNEL = ["micropip", "packaging", "ssl", "sqlite3", "comm", "jedi", "ipython"]
# verknode's engine (src/lite.ts) and what its nodes load on demand (jupyterlab_verknode/lite.py)
VERKNODE = ["numpy", "polars", "pyarrow", "pandas", "pyodide-http", "pyodide-unix-timezones",
            "scikit-learn", "scipy", "statsmodels", "matplotlib", "lxml", "openpyxl", "requests", "pytz",
            "python-dateutil"]


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "verknode-lite-build"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def closure(lock: dict, roots: list[str]) -> list[str]:
    pkgs = {canon(k): k for k in lock["packages"]}
    out, todo = [], [canon(r) for r in roots]
    while todo:
        name = todo.pop()
        if name in out:
            continue
        if name not in pkgs:
            print(f"  (not in Pyodide: {name})")
            continue
        out.append(name)
        todo += [canon(d) for d in lock["packages"][pkgs[name]].get("depends", [])]
    return sorted(pkgs[n] for n in out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("version")
    p.add_argument("out")
    p.add_argument("--extra", nargs="*", default=[], help="more Pyodide packages to bundle")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    v = a.version
    cdn = f"https://cdn.jsdelivr.net/pyodide/v{v}/full/"
    print(f"Pyodide {v}: the core runtime")
    core = fetch(f"https://github.com/pyodide/pyodide/releases/download/{v}/pyodide-core-{v}.tar.bz2")
    lock = json.loads(fetch(cdn + "pyodide-lock.json"))
    names = closure(lock, KERNEL + VERKNODE + a.extra)
    total = 0
    if not a.dry_run:
        os.makedirs(a.out, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(core), mode="r:bz2") as t:
            for m in t.getmembers():
                if m.isfile():
                    rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                    dest = os.path.join(a.out, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(t.extractfile(m).read())
    for n in names:
        info = lock["packages"][n]
        dest = os.path.join(a.out, info["file_name"])
        if a.dry_run:
            req = urllib.request.Request(cdn + info["file_name"], method="HEAD")
            with urllib.request.urlopen(req, timeout=60) as r:
                size = int(r.headers.get("Content-Length") or 0)
        else:
            if not os.path.isfile(dest):
                data = fetch(cdn + info["file_name"])
                if hashlib.sha256(data).hexdigest() != info["sha256"]:
                    raise SystemExit(f"{info['file_name']}: the checksum does not match the lock file")
                with open(dest, "wb") as f:
                    f.write(data)
            size = os.path.getsize(dest)
        total += size
        print(f"  {n:28} {size / 1e6:6.1f} MB")
    print(f"{len(names)} packages, {total / 1e6:.0f} MB")
    if not a.dry_run:
        keep = {n: lock["packages"][n] for n in names}
        with open(os.path.join(a.out, "pyodide-lock.json"), "w") as f:
            json.dump({**lock, "packages": keep}, f)


if __name__ == "__main__":
    main()
