FROM quay.io/jupyter/scipy-notebook:latest

USER root

# install applications
RUN apt-get update && apt-get install -y --no-install-recommends \
        zip unzip vim tmux \
    && rm -rf /var/lib/apt/lists/*

# nodejs for building the JupyterLab (federated) extension
ARG NODE_VERSION=22.20.0
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1 \
            --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md

# Ketcher (EPAM) chemical structure editor, standalone build (Indigo in the browser);
# served by the verknode server extension at <base_url>/verknode/ketcher/ for the Molecule Sketcher
ARG KETCHER_VERSION=3.18.0
RUN curl -fsSL -o /tmp/ketcher.zip \
        "https://github.com/epam/ketcher/releases/download/v${KETCHER_VERSION}/ketcher-standalone-${KETCHER_VERSION}.zip" \
    && mkdir -p /opt/ketcher && unzip -q /tmp/ketcher.zip -d /opt/ketcher && rm /tmp/ketcher.zip
ENV VERKNODE_KETCHER_DIR=/opt/ketcher

# dev-installs the bind-mounted extension source on every container start
COPY docker/before-notebook.d/ /usr/local/bin/before-notebook.d/

USER ${NB_UID}

# workflow engine data libraries (polars, RDKit) + JupyterLab extension build backend
RUN pip install --no-cache-dir \
        polars \
        rdkit \
        fastexcel \
        xlsxwriter \
        lxml \
        jsonpath-ng \
        pyarrow \
        "hatchling>=1.5.0" \
        editables \
        "hatch-nodejs-version>=0.3.2" \
        "hatch-jupyter-builder>=0.5"

CMD ["start-notebook.py"]
