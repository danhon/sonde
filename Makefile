# The fleet's gate, on the box this runs on: the include below brings in its
# deploy targets from outside this repo. Elsewhere the include finds nothing,
# so `make test` and `make run` work and `make deploy` refuses.
FLEET_MK := $(HOME)/dev/reverse-proxy/make/fleet.mk
-include $(FLEET_MK)
ifeq ($(wildcard $(FLEET_MK)),)
_FLEET_SAVED_GOAL := $(.DEFAULT_GOAL)
.PHONY: fleet-missing
fleet-missing:
	@echo "deploy: there is no deployment here; run it yourself with make run (README, Running it yourself)" >&2; exit 1
deploy install-units: fleet-missing
.DEFAULT_GOAL := $(_FLEET_SAVED_GOAL)
endif

.PHONY: test run css css-check

test:
	uv run pytest -q

# The stylesheet, compiled from sonde/web/tailwind.css and committed (sonde#6).
# Run css after changing a class in a template; CI runs css-check.
css:
	scripts/tailwind.sh

css-check:
	scripts/tailwind.sh --check

# Run it yourself, in Docker: http://127.0.0.1:8090 (compose.override.yml).
# Needs a .env (cp .env.example .env, then set BLUESKY_ACTOR). Its own compose
# project, with only this repo's compose files, so it can't take over a
# deployment's container on the same host. The container runs as uid 10001:
# an ACL lets it write where setfacl works (Linux), and Docker Desktop maps
# ownership itself, so elsewhere the folders are left as they are.
run:
	mkdir -p backups logs
	chmod 700 backups logs
	setfacl -m u:10001:rwx backups logs 2>/dev/null || true
	env -u COMPOSE_FILE \
	COMPOSE_PROJECT_NAME=sonde-local \
	BACKUP_HOST_DIR=./backups SONDE_LOG_DIR=./logs \
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose up --build
