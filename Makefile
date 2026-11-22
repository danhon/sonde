# The fleet's gate (reverse-proxy make/fleet.mk, which includes the checkout
# guard): `make deploy` runs checkout-guard and fleet-check against this repo's
# fleet.yml first, and `make install-units` links deploy units (UNIT-1).
# Optional include: CI and other machines have no reverse-proxy checkout, and a
# plain include would stop make before any target runs. Without it, deploy
# refuses rather than running ungated.
FLEET_MK := $(HOME)/dev/reverse-proxy/make/fleet.mk
-include $(FLEET_MK)
ifeq ($(wildcard $(FLEET_MK)),)
_FLEET_SAVED_GOAL := $(.DEFAULT_GOAL)
.PHONY: fleet-missing
fleet-missing:
	@echo "deploy refused: $(FLEET_MK) not found (no reverse-proxy checkout here)" >&2; exit 1
deploy install-units: fleet-missing
.DEFAULT_GOAL := $(_FLEET_SAVED_GOAL)
endif

APP    ?= sonde
DOMAIN ?= sgc.rayandhon.com
HOST   ?= $(APP).$(DOMAIN)
LOG_DIR ?= $(HOME)/$(APP)-logs

.PHONY: deploy deploy-pulled preview logs stop test verify

deploy:
	git pull
	@# A fresh make, so the rest runs the recipe just pulled, not this one
	@# (reverse-proxy#78): make reads the Makefile before the pull.
	@$(MAKE) --no-print-directory deploy-pulled

# The logs directory (LOG-1), created here rather than by Docker, which would
# make it root's. An ACL instead of a chown, which would need root: uid 10001
# may write, the directory stays the host user's.
.PHONY: log-dir
log-dir:
	mkdir -p $(LOG_DIR)
	setfacl -m u:10001:rwx $(LOG_DIR)

# The part of deploy after the pull. Run `make deploy`, not this.
deploy-pulled: log-dir
	COMPOSE_PROJECT_NAME=$(APP) \
	SERVICE_HOST=$(HOST) \
	SONDE_LOG_DIR=$(LOG_DIR) \
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
	docker compose up -d --build --wait
	@$(FLEET_PROBE)

preview:
	$(MAKE) --no-print-directory log-dir LOG_DIR=$(LOG_DIR)-preview
	COMPOSE_PROJECT_NAME=$(APP)-preview \
	SERVICE_HOST=$(APP)-preview.$(DOMAIN) \
	SONDE_LOG_DIR=$(LOG_DIR)-preview \
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
