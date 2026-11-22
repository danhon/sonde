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

.PHONY: test run

test:
	uv run pytest -q

# Run it yourself, in Docker: http://127.0.0.1:8090 (compose.override.yml).
# Needs a .env (cp .env.example .env, then set BLUESKY_ACTOR).
run:
	mkdir -p backups logs
	chmod a+rwx backups logs   # the container runs as uid 10001
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose up --build
