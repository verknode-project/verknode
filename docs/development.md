# Developing verknode

## Extension development

`./extension` is mounted at `/home/jovyan/extension`. On every container
start `docker/before-notebook.d/10-verknode-extension.sh` runs `jlpm install` /
`jlpm build` (first time only), `pip install -e .` and
`jupyter labextension develop`, so the extension is live in JupyterLab.

    make watch      # rebuild on source change, then reload the browser
    make ext-build  # one-off rebuild

`make restart` reloads the Python side (workflow engine / server extension).

## Make targets

    make build      # build the image (creates credentials_norepo.env from the example if missing)
    make up         # start            make down     # stop
    make logs       # follow the logs  make config   # the compose configuration in effect
    make restart    # restart JupyterLab (reloads the Python side)
    make ext-build  # rebuild the extension once    make watch   # rebuild on every change
    make login      # a root shell in the container

## Project layout

    extension/                      the JupyterLab extension (package jupyterlab_verknode)
      src/                          frontend (TypeScript / React): editor, views, widgets, sketcher
      style/                        CSS
      schema/plugin.json            editor settings (JupyterLab Settings Editor)
      jupyterlab_verknode/engine/   workflow engine: nodes, ports, execution, .vkf files
      jupyterlab_verknode/nodes/    the standard nodes (same API as custom nodes)
      jupyterlab_verknode/handlers.py   REST API of the editor
      verknode/                     public Python API (verknode.open, verknode.node)
    notebooks/examples/             example workflows and custom node folders
    docker/, Dockerfile, docker-compose.yml, deploy/   the container (see deployment.md)
