# syntax=docker/dockerfile:1.7

# Build/venv stage
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Dependency metadata first so the dep layer caches independently of source.
# README.md is required by hatchling because pyproject declares a readme field.
COPY pyproject.toml uv.lock README.md ./

# Two-step sync, both --frozen, so uv.lock is authoritative for the dependency
# tree AND for the project install. `uv pip install .` would work here but goes
# through the pip-compat shim and resolves outside the lock, which is exactly
# the guarantee --frozen exists to give. uv sync creates /app/.venv itself, so
# no separate `uv venv` step is needed.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --frozen --no-dev --no-install-project

COPY sonde/ ./sonde/
RUN uv sync --frozen --no-dev


# Runtime stage
FROM python:3.12-slim

RUN useradd -m -u 10001 appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/sonde /app/sonde
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml

# The runtime stage carries the venv but not uv, so the console script is
# invoked directly rather than via `uv run`. In a container that is also the
# better call: the environment is already resolved and frozen at build time, so
# re-checking it on every start buys nothing. `uv run` remains correct for local
# development, where the environment can drift.
ENV PATH="/app/.venv/bin:${PATH}"

# Defaults — override in .env or compose.yml
ENV DB_PATH="/data/sonde.db" \
    BACKUP_DIR="/backup" \
    WEB_HOST="0.0.0.0" \
    WEB_PORT="8090"

RUN mkdir -p /data /backup && chown -R appuser:appuser /data /backup

EXPOSE 8090

USER appuser

# Declared last so per-deploy changes don't bust the layers above.
ARG GIT_SHA=dev
ARG BUILD_TIME=
ENV BUILD_SHA="${GIT_SHA}" \
    BUILD_TIME="${BUILD_TIME}"

CMD ["sonde", "--schedule"]
