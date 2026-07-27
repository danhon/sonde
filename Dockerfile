# syntax=docker/dockerfile:1.7

# Build/venv stage
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Dependency metadata first so the dep layer caches independently of source.
# README.md is required by hatchling because pyproject declares a readme field.
COPY pyproject.toml uv.lock README.md ./

RUN uv venv /app/.venv \
  && uv sync --frozen --no-dev --no-install-project

COPY sonde/ ./sonde/
RUN uv pip install .


# Runtime stage
FROM python:3.12-slim

RUN useradd -m -u 10001 appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/sonde /app/sonde
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml

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
