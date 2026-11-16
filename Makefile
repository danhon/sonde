# The deploy guard (reverse-proxy make/checkout-guard.mk): `make deploy` refuses
# off ubuntuplex or with a tracked file uncommitted, then pulls --ff-only.
# Replaced by make/fleet.mk, which includes it, when this app takes fleet.yml.
# Optional include: CI and other machines have no reverse-proxy checkout, and
# a plain include would stop make before any target runs. Without it, deploy
# refuses rather than running unguarded.
GUARD_MK := $(HOME)/dev/reverse-proxy/make/checkout-guard.mk
-include $(GUARD_MK)
ifeq ($(wildcard $(GUARD_MK)),)
_NOGUARD_DEFAULT_GOAL := $(.DEFAULT_GOAL)
.PHONY: deploy-unguarded
deploy-unguarded:
	@echo "deploy refused: $(GUARD_MK) not found (no reverse-proxy checkout here)" >&2; exit 1
deploy: deploy-unguarded
.DEFAULT_GOAL := $(_NOGUARD_DEFAULT_GOAL)
endif

APP    ?= sonde
DOMAIN ?= sgc.rayandhon.com
HOST   ?= $(APP).$(DOMAIN)

.PHONY: deploy preview logs stop test verify

deploy:
	git pull
	COMPOSE_PROJECT_NAME=$(APP) \
	SERVICE_HOST=$(HOST) \
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose up -d --build

preview:
	COMPOSE_PROJECT_NAME=$(APP)-preview \
	SERVICE_HOST=$(APP)-preview.$(DOMAIN) \
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose up -d --build

logs:
	docker compose -p $(APP) logs -f

stop:
	docker compose -p $(APP) down

test:
	uv run pytest -q

# Post-deploy checks. The Host(``) failure mode looks perfectly healthy in
# `docker ps`, so verify the label rather than the container.
verify:
	@docker inspect $(APP)-app-1 --format '{{json .Config.Labels}}' \
		| tr ',' '\n' | grep -E 'routers\.$(APP)(-health|-api)?\.rule' || true
	@echo "--- / (expect 302 to Authelia) ---"
	@curl -sk -o /dev/null -w '%{http_code}\n' https://$(HOST)/
	@echo "--- /healthz (expect 200) ---"
	@curl -sk -o /dev/null -w '%{http_code}\n' https://$(HOST)/healthz
	@echo "--- /api/v1/meta, no token (expect 401) ---"
	@echo "    302 here means the API router is missing and Authelia caught it;"
	@echo "    200 means the token check is not running. Both are failures."
	@curl -sk -o /dev/null -w '%{http_code}\n' https://$(HOST)/api/v1/meta
	@echo "--- /api/status, no token (expect 302 to Authelia) ---"
	@echo "    A 401 here means the API rule was widened to /api and the job"
	@echo "    poller lost its Authelia guard."
	@curl -sk -o /dev/null -w '%{http_code}\n' https://$(HOST)/api/status
	@echo "--- the API router must carry no Authelia ---"
	@docker inspect $(APP)-app-1 --format '{{json .Config.Labels}}' \
		| tr ',' '\n' | grep -E 'routers\.$(APP)-api\.middlewares' || true
