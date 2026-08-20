# The Playwright image already carries every browser system library Scrapling's
# DynamicFetcher and StealthyFetcher need (libgbm, libnss3, libatk-bridge2.0, ...).
# Installing those onto python:3.12-slim by hand is a long apt list that breaks
# quietly whenever Chromium moves. The cost is image size on the web service, which
# does not need browsers; the single-image APP_TARGET dispatch is worth it.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble AS base

# The environment is an explicit venv, not /usr/local. This base image keeps its
# interpreter elsewhere, so pointing UV_PROJECT_ENVIRONMENT at /usr/local fails the
# build with "not a valid Python environment (no Python executable was found)" --
# and a venv is one directory to copy between stages, which /usr/local is not.
ENV PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PIP_NO_CACHE_DIR=1 \
	UV_LINK_MODE=copy \
	UV_PROJECT_ENVIRONMENT=/opt/venv \
	PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# --- Dependencies -----------------------------------------------------------
# Separate layer so a source-only change does not reinstall the world.
FROM base AS deps
COPY pyproject.toml uv.lock ./
# `--frozen` refuses to re-resolve. Without it a lockfile that has drifted from
# pyproject.toml is silently ignored and the image gets whatever resolves today,
# which is the reproducibility the lockfile exists to provide.
RUN uv venv /opt/venv && uv sync --no-dev --no-install-project --frozen

# --- Runtime ----------------------------------------------------------------
# Must remain the last stage: Railway builds whatever the final stage is.
FROM base AS runtime

COPY --from=deps /opt/venv /opt/venv

# Both browser tiers drive patchright, which ships its own Chromium build and does
# not reuse the base image's. Without this step DynamicFetcher and StealthyFetcher
# raise at runtime, on the worker, for exactly the pages that needed them most.
# The base image is still the right starting point: it carries the system libraries
# (libgbm, libnss3, libatk-bridge2.0, ...) that build links against.
#
# `scrapling[fetchers]` pulls playwright and patchright and nothing else -- notably
# not camoufox, which Scrapling 0.4.14 does not use. Verified in the deployed
# container: both tiers fetch successfully.
#
# PLAYWRIGHT_BROWSERS_PATH is shared rather than per-user because the process runs
# as an unprivileged account that cannot read root's home.
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

# The base image already ships an unprivileged `pwuser` -- it is the account the
# Playwright image intends browsers to run as, and its uid is 1001, so creating our
# own user at that uid fails the build with useradd exit 4. Reusing it also means
# the browser caches are owned by the account that reads them.
RUN chown -R pwuser:pwuser /app /opt/cache
USER pwuser

ENV PORT=3000 \
	APP_TARGET=web
EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
