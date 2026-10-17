# Container tasks; run them here or from the repository root with `make -C docker <target>`.

build:
	test -f credentials_norepo.env || cp credentials_norepo.env.example credentials_norepo.env
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

# restart JupyterLab (reloads the Python server extension / workflow engine)
restart:
	docker compose restart verknode

logs:
	docker compose logs -f

# the configuration in effect here (compose.yml + .env + compose.override.yml)
config:
	docker compose config

# rebuild the extension once (reload the browser afterwards)
ext-build:
	docker compose exec -w /home/jovyan/extension verknode jlpm build

# rebuild the extension continuously on source changes
watch:
	docker compose exec -w /home/jovyan/extension verknode jlpm watch

login:
	docker compose exec -u root verknode bash
