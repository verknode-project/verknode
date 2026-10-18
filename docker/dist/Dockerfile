# verknode without conda: python:slim + pip (the conda-based docker/Dockerfile is the development setup).
#
#   docker build -f docker/dist/Dockerfile -t verknode .                      # everything (EXTRAS=all)
#   docker build -f docker/dist/Dockerfile --build-arg EXTRAS= -t verknode-core .   # the core only
#   docker run --rm -p 8888:8888 -e JUPYTER_TOKEN=... -v "$PWD/work:/home/jovyan/work" verknode
#
# EXTRAS: all, or a comma list of chem, ml, formats, notebooks (empty: the core - polars, numpy). The
# build context is the repository root (docker/dist/Dockerfile.dockerignore keeps out builds and secrets).
ARG PYTHON=3.13

# ---- build: the frontend (Node.js) and the wheel ----
FROM python:${PYTHON}-slim AS build
ARG NODE_VERSION=22.20.0
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
RUN pip install --no-cache-dir "jupyterlab>=4,<5" "hatchling>=1.27" "hatch-nodejs-version>=0.3.2" \
        "hatch-jupyter-builder>=0.5" editables build
COPY extension /src
WORKDIR /src
RUN jlpm install --immutable 2>/dev/null || jlpm install
RUN python -m build --wheel --no-isolation --outdir /wheels

# ---- Ketcher (the Molecule Sketcher's structure editor) ----
FROM python:${PYTHON}-slim AS ketcher
ARG KETCHER_VERSION=3.18.0
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/k.zip \
        "https://github.com/epam/ketcher/releases/download/v${KETCHER_VERSION}/ketcher-standalone-${KETCHER_VERSION}.zip" \
    && mkdir -p /opt/ketcher && unzip -q /tmp/k.zip -d /opt/ketcher && rm /tmp/k.zip

# ---- the image ----
FROM python:${PYTHON}-slim
ARG EXTRAS=all
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VERKNODE_KETCHER_DIR=/opt/ketcher
# tini (signals), git (JupyterLab's file browser is fine without; workflows use it rarely), the system
# libraries RDKit's drawing needs in a slim image
RUN apt-get update && apt-get install -y --no-install-recommends tini libxrender1 libxext6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir "jupyterlab>=4,<5" \
        "$(ls /wheels/*.whl)$( [ -n "$EXTRAS" ] && echo "[$EXTRAS]")" \
    && rm -rf /wheels
COPY --from=ketcher /opt/ketcher /opt/ketcher
RUN useradd --create-home --uid 1000 --shell /bin/bash jovyan
COPY --chown=jovyan notebooks/examples /home/jovyan/examples
USER jovyan
WORKDIR /home/jovyan
EXPOSE 8888
ENTRYPOINT ["tini", "--"]
# (the token: JUPYTER_TOKEN; without one, JupyterLab makes one and prints it)
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--ServerApp.root_dir=/home/jovyan"]
