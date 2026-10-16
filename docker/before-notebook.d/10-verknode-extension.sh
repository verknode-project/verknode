#!/bin/bash
# Sourced by the docker-stacks start.sh before JupyterLab launches.
# Installs the bind-mounted extension source (/home/jovyan/extension) in
# development mode so `make watch` rebuilds are picked up on page reload.
# Runs in a subshell so a failure never prevents JupyterLab from starting.
VERKNODE_EXT_DIR=/home/jovyan/extension
if [ -f "${VERKNODE_EXT_DIR}/pyproject.toml" ]; then
    (
        set -e
        cd "${VERKNODE_EXT_DIR}"
        [ -d node_modules ] || jlpm install
        [ -f jupyterlab_verknode/labextension/package.json ] || jlpm build
        # (re)install only when the packaging changed; the editable install
        # survives container restarts
        stamp=/home/jovyan/.cache/verknode-install.sha
        sum=$(sha1sum pyproject.toml | cut -d' ' -f1)
        if [ "$(cat "$stamp" 2>/dev/null)" != "$sum" ] || ! pip show -q jupyterlab_verknode >/dev/null 2>&1; then
            pip install --quiet --no-build-isolation --no-deps -e .
            mkdir -p "$(dirname "$stamp")" && echo "$sum" > "$stamp"
        fi
        jupyter labextension develop . --overwrite
        jupyter server extension enable jupyterlab_verknode --sys-prefix
    ) || echo "[verknode] WARNING: extension dev install failed; starting JupyterLab without it"
fi
