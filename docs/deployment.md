# Deploying verknode

This page is about the development setup: a JupyterLab container
(`docker/Dockerfile`, based on `quay.io/jupyter/scipy-notebook`) with the
extension in `extension/`. To use verknode, the conda-free image
`docker/dist/Dockerfile` (pip, with all extras or only the core) is smaller
and simpler; see [installing](install.md). The proxy settings below apply
to either.
Everything for the container is in `docker/`: `compose.yml` is the same for
every environment, and everything specific to a machine lives in gitignored
files next to it.

| File in `docker/` (gitignored) | What it holds                                         | Start from                       |
| ------------------------------ | ----------------------------------------------------- | -------------------------------- |
| `credentials_norepo.env`       | `JUPYTER_TOKEN` (the login token), other secrets      | `credentials_norepo.env.example` |
| `.env`                         | values for `compose*.yml` (port, host name ...)       | `.env.example`                   |
| `compose.override.yml`         | changes to the service (proxy, networks, labels ...)  | `deploy/*.yml`                   |

`docker compose` reads `.env` and `compose.override.yml` automatically, so
`make build`, `make up` etc. work the same everywhere. Run the commands
below in `docker/`, or from the repository root as `make -C docker up` and
so on. `make config` prints the resulting configuration. The override examples use
`!reset` / `!override`, which need Docker Compose 2.24 or later.

## Requirements

- Docker with the Compose plugin (2.24+).
- Internet access when building (base image, Node.js, Ketcher, Python packages).
- About 6 GB of disk for the image.

## Local (default)

    cd docker
    make build          # creates credentials_norepo.env from the example if missing
    # set JUPYTER_TOKEN in credentials_norepo.env
    make up

JupyterLab is at http://127.0.0.1:8888/lab?token=<JUPYTER_TOKEN>. To use
another port, set `VERKNODE_PORT` in `.env`.

## On the local network

Set `VERKNODE_BIND=0.0.0.0` in `.env` (or use
`deploy/dev.yml` as the override). The connection is plain
HTTP: keep a strong token, and prefer a reverse proxy with HTTPS beyond a
trusted network.

## Behind Traefik

For a Traefik instance already running on the same Docker host:

    cd docker
    cp deploy/traefik.yml compose.override.yml
    cp .env.example .env    # set VERKNODE_HOST, TRAEFIK_NETWORK, TRAEFIK_ENTRYPOINT, TRAEFIK_CERTRESOLVER
    make up

The override removes the published port and adds the router labels and the
external Traefik network. For an HTTP-only entrypoint, set
`TRAEFIK_ENTRYPOINT=web` and delete the two `tls` labels.

## Behind another reverse proxy

Publish the port locally (the default) and proxy to it. JupyterLab needs
WebSockets; for nginx:

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;
    }

To serve it under a path (e.g. `/verknode/`), add JupyterLab's base URL in
the override:

    services:
        verknode:
            command: ["start-notebook.py", "--ServerApp.base_url=/verknode/"]

and proxy that path without stripping it.

## Without Docker

`docker/Dockerfile` is the reference for a manual install: Python 3.11+, Node.js
(to build the extension), then

    pip install polars rdkit fastexcel xlsxwriter lxml jsonpath-ng pyarrow jupyterlab
    pip install ./extension
    jupyter lab

The Molecule Sketcher's Ketcher editor is optional: unzip the Ketcher
standalone release and point `VERKNODE_KETCHER_DIR` at it (the built-in
sketcher works without it).

## Data and updates

- `notebooks/` is mounted into the container: workflows, notebooks and
  custom nodes live there, on the host. Back up that folder.
- The extension source is mounted too and dev-installed at every container
  start (`docker/before-notebook.d/10-verknode-extension.sh`), so an update
  is `git pull`, then `make ext-build` (frontend) and `make restart`
  (Python side). Rebuild the image (`make build && make up`) when
  `docker/Dockerfile` changes.
- User settings (themes, editor options) are kept in the container's home
  directory: they survive restarts but not a re-created container.

## Security

- Anyone with the token can run code in the container: workflows run
  Python (scripts, custom nodes, notebooks), and `.vkf` files hold pickled
  port objects. Only open workflow files you trust.
- Never publish the port on a public interface without a token, and use
  HTTPS for anything beyond a trusted network.
- Secrets belong in `credentials_norepo.env`; it is gitignored and loaded
  as the container's environment.
