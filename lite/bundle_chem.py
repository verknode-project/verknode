"""Chemistry in the JupyterLite site, at the paths a Jupyter server serves them from, so the same URLs work:

    verknode/rdkit/     RDKit's WebAssembly build (MinimalLib, @rdkit/rdkit): the chemistry nodes run on it
                        through RDKit's Python API (jupyterlab_verknode/lite_rdkit)
    verknode/ketcher/   Ketcher (EPAM), the Molecule Sketcher's structure editor: its standalone build
    verknode/vendor/    the 3D structure viewers (NGL, Mol*) of verknode's wheel

    python lite/bundle_chem.py SITE/verknode 2026.9.1 3.18.0
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "verknode-lite-build"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def rdkit_js(out: str, version: str):
    data = fetch(f"https://registry.npmjs.org/@rdkit/rdkit/-/rdkit-{version}.tgz")
    os.makedirs(out, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for name in ("RDKit_minimal.js", "RDKit_minimal.wasm"):
            with open(os.path.join(out, name), "wb") as f:
                f.write(t.extractfile(f"package/dist/{name}").read())
            print(f"  rdkit/{name} {os.path.getsize(os.path.join(out, name)) / 1e6:.1f} MB")


def ketcher(out: str, version: str):
    data = fetch(f"https://github.com/epam/ketcher/releases/download/v{version}/ketcher-standalone-{version}.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(out)
    # (one app is enough: index.html - the popup, closable and duo variants are the same editor again)
    for root, _, files in os.walk(out):
        for f in files:
            if f.split(".")[0] in ("popup", "closable", "duo"):
                os.remove(os.path.join(root, f))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(out) for f in fs)
    print(f"  ketcher/ {size / 1e6:.0f} MB")


def vendor(out: str):
    import jupyterlab_verknode

    src = os.path.join(os.path.dirname(jupyterlab_verknode.__file__), "vendor")
    if os.path.isdir(src):
        shutil.copytree(src, out, dirs_exist_ok=True)
        print(f"  vendor/ {', '.join(sorted(os.listdir(out)))}")


def main():
    site, rdkit_version, ketcher_version = sys.argv[1], sys.argv[2], sys.argv[3]
    rdkit_js(os.path.join(site, "rdkit"), rdkit_version)
    ketcher(os.path.join(site, "ketcher"), ketcher_version)
    vendor(os.path.join(site, "vendor"))


if __name__ == "__main__":
    main()
