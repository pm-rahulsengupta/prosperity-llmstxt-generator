# The Playwright image already carries every browser system library Scrapling's
# DynamicFetcher and StealthyFetcher need (libgbm, libnss3, libatk-bridge2.0, ...).
# Installing those onto python:3.12-slim by hand is a long apt list that breaks
# quietly whenever Chromium moves. The cost is image size on the web service, which
# does not need browsers; the single-image APP_TARGET dispatch is worth it.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble AS base

ENV PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PIP_NO_CACHE_DIR=1 \
	UV_LINK_MODE=copy \
	UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# --- Dependencies -----------------------------------------------------------
# Separate layer so a source-only change does not reinstall the world.
FROM base AS deps
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

# --- Runtime ----------------------------------------------------------------
# Must remain the last stage: Railway builds whatever the final stage is.
FROM base AS runtime

COPY --from=deps /usr/local /usr/local

# Scrapling's browser tiers do NOT run on the base image's browsers. DynamicFetcher
# drives patchright and StealthyFetcher drives camoufox, and each ships its own
# build; without this the ladder's top two rungs raise at runtime, on the worker,
# only for the pages that needed them most. The base image is still the right
# starting point -- it carries the system libraries (libgbm, libnss3,
# libatk-bridge2.0, ...) that these builds link against.
#
# Caches go to a shared path rather than root's home, because the process runs as
# an unprivileged user and cannot read /root/.cache.
ENV XDG_CACHE_HOME=/opt/cache \
	PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /opt/cache && scrapling install && chmod -R a+rX /opt/cache /ms-playwright

COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh

# This repo is developed on Windows. Without the CRLF strip the shebang carries a
# trailing \r and the container dies with "no such file or directory".
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

RUN useradd --create-home --uid 1001 llmstxt && chown -R llmstxt:llmstxt /app
USER llmstxt

ENV PORT=3000 \
	APP_TARGET=web
EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
