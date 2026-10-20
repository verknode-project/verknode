# verknode in the browser: a JupyterLite site - static files, no server. The workflow engine runs in a
# Pyodide kernel (WebAssembly Python) in the page; the files are the browser's (see docs/jupyterlite.md).
#
#   docker build -f docker/lite/Dockerfile --target site --output type=local,dest=lite/_output .
#   python3 -m http.server 8000 -d lite/_output        # then http://localhost:8000/lab/index.html
#
# (make lite / make lite-serve in docker/.) The build context is the repository root.
#
# Pyodide 0.27 is the last with polars (1.18): jupyterlite-pyodide-kernel 0.6.1 brings it, with JupyterLite
# 0.6 (JupyterLab 4.4). Pyodide and the packages are in the site (lite/bundle_pyodide.py), and so are
# RDKit's WebAssembly build, Ketcher and the 3D viewers (lite/bundle_chem.py): it loads nothing from a CDN.

# ---- build: the frontend (Node.js) and the wheel ----
FROM python:3.13-slim AS build
ARG NODE_VERSION=22.20.0
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
RUN pip install --no-cache-dir "jupyterlab>=4.4,<4.5" "hatchling>=1.27" "hatch-nodejs-version>=0.3.2" \
        "hatch-jupyter-builder>=0.5" editables build
COPY extension /src
WORKDIR /src
RUN jlpm install --immutable 2>/dev/null || jlpm install
RUN python -m build --wheel --no-isolation --outdir /wheels

# ---- the site ----
FROM python:3.12-slim AS lite
RUN pip install --no-cache-dir "jupyterlite-core==0.6.4" "jupyterlite-pyodide-kernel==0.6.1" "jupyterlab>=4.4,<4.5"
COPY --from=build /wheels /wheels
# (the extension, for the site's JupyterLab; its Python dependencies are not needed to build)
RUN pip install --no-cache-dir --no-deps /wheels/*.whl
COPY lite/jupyter_lite_config.json lite/jupyter-lite.json lite/bundle_pyodide.py /build/
WORKDIR /build
# Pyodide and its packages in the site (static/pyodide): nothing from a CDN, for networks that block them
ARG PYODIDE=0.27.6
RUN python bundle_pyodide.py "$PYODIDE" static/pyodide
# pure-Python packages Pyodide does not have, for the kernel (comm) and the nodes: from the site too
RUN pip download --no-cache-dir --no-deps --only-binary=:all: -d /wheels/extra \
        comm openpyxl et-xmlfile jsonpath-ng ply xlsxwriter
COPY notebooks/examples /build/contents/examples
RUN jupyter lite build --piplite-wheels "$(ls /wheels/*.whl)" \
        $(for w in /wheels/extra/*.whl; do printf -- "--piplite-wheels %s " "$w"; done)
# chemistry: RDKit's WebAssembly build (the chemistry nodes run on it), Ketcher and the 3D structure viewers,
# at the paths the Jupyter server serves them from (verknode/...) - lite/bundle_chem.py
ARG RDKIT_JS=2026.9.1
ARG KETCHER_VERSION=3.18.0
COPY lite/bundle_chem.py /build/
RUN python bundle_chem.py /site/verknode "$RDKIT_JS" "$KETCHER_VERSION"

FROM scratch AS site
COPY --from=lite /site /
