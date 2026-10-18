# Developing verknode

## Extension development

Files and folders verknode itself puts into the JupyterLab file browser have a
`vkn_` prefix, so users can tell them from their own work and leave them alone
unless they work on verknode:
- `vkn_extension/` - the extension source (this repository's `extension/`);
- `vkn_nodes/` - the node folders made by *Create example node file*, and the
  examples' `vkn_custom_nodes/` and `vkn_widget_nodes/`;
- `vkn_runtime/` - the parts of verknode inside an export folder.

`./extension` is mounted at `/home/jovyan/vkn_extension`. On every container
start `docker/before-notebook.d/10-verknode-extension.sh` runs `jlpm install` /
`jlpm build` (first time only), `pip install -e .` and
`jupyter labextension develop`, so the extension is live in JupyterLab.

    make watch      # rebuild on source change, then reload the browser
    make ext-build  # one-off rebuild

`make restart` reloads the Python side (workflow engine / server extension).

## Make targets

In `docker/` (or from the repository root as `make -C docker <target>`):

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
    docker/                         the container: Dockerfile, compose.yml, Makefile,
                                    deploy/ examples, start-up script (see deployment.md)
    docs/                           documentation; assets/ logos, images/ screenshots
