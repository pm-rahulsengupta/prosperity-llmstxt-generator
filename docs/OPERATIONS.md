# Operations

Written in the style of `geo-tracker/docs/PROSPERITY.md`: every number here was
measured on this deployment, and every workaround records why it exists. If
something below reads as arbitrary, it is because the obvious alternative was tried
and failed in a way worth remembering.

---

## What is deployed

Railway project **llmstxt-generator**, workspace *prosperity-media's Projects*,
project id `a9cb5c54-aa41-4b0d-ad36-40a3707a21af`.

| Service  | `APP_TARGET` | `RUN_MIGRATIONS` | Domain | Notes |
|----------|--------------|------------------|--------|-------|
| web      | `web`        | `true`           | none yet | FastAPI + HTMX. `PORT=3000` pinned. |
| worker   | `worker`     | `false`          | none | procrastinate worker, concurrency 1. Serves `/healthz` only. |
| Postgres | —            | —                | private | Railway plugin. Replaces Snowflake. |

One image, two services, dispatched by `APP_TARGET` in `docker/entrypoint.sh` —
Railway builds only a Dockerfile's final stage and offers no target selection, so
the final stage carries both runtimes. Copied from geo-tracker.

### The web service has no public domain, deliberately

Google SSO is not configured yet, and `Settings.assert_deployable()` only refuses to
boot when `APP_URL` is `https://`. Generating a domain now would put a tool that
crawls arbitrary sites and stores client page content on the public internet with no
authentication at all.

To finish the deployment:

1. Create an OAuth 2.0 Web client in Google Cloud for the Prosperity workspace.
   Authorised redirect URI: `https://<railway-domain>/auth/callback`.
2. `railway variables --service web --set GOOGLE_CLIENT_ID=... --set GOOGLE_CLIENT_SECRET=...`
3. `railway variables --service web --set APP_URL=https://<railway-domain>`
4. Generate the domain in the Railway dashboard, then `railway redeploy --service web`.

Step 3 is what arms the guard: from then on the service refuses to start if the SSO
variables are missing, so it cannot regress to open access silently.

---

## Verified, with numbers

**Local, end to end** (Postgres 16 on this machine, real OpenAI key), against
`prosperitymedia.com.au`:

- Recon: 223 crawlable URLs across 7 sitemaps.
- Size check: Google reports ~192–307 indexed. The figure moves between calls;
  it is an order-of-magnitude signal and is used as one.
- Plan: LLM produced 4 template rules, `index_export` pattern. Approved with a
  60-page cap.
- Crawl: 60 of 60 pages fetched, **all on the cheap HTTP tier, zero browser
  launches**.
- Triage: 60 of 60 placed. Summarise: 60 of 60 link lines written.
- Output: 60 pages in 5 sections, spec-compliant, 4 advisory issues.
- Cost: 7 LLM calls, 31,431 prompt + 5,178 completion tokens, 1 SERP call,
  **zero fallbacks**.

**Deployed**: migrations applied on first boot, `/healthz` green on both services,
and both browser tiers fetch successfully from inside the worker container
(checked with `railway ssh --service worker`).

**Not yet done on Railway**: a full crawl through the deployed UI. That needs the
domain, which needs SSO.

---

## Things that will bite, and why

### `PORT=3000` must be set as a service variable

Railway injects `PORT=8080`, which overrides the Dockerfile's `ENV PORT`, while the
generated domain points at the Dockerfile port. Mismatch is a 502 on every request.
Carried from geo-tracker; set on both services.

### A variable change does not restart the container

`railway redeploy --service <name>` after changing one.

### `RUN_MIGRATIONS=true` on exactly one service

It is on **web** only. Two services with it set race the same Alembic lock.

### The database is private-network only

No `DATABASE_PUBLIC_URL`. For local `psql`:

    railway connect Postgres --tunnel-only --port 55432

which needs a registered key first — `railway ssh keys add` — and fails with no
useful hint otherwise.

### The Dockerfile strips CRLF from the entrypoint

This repo is developed on Windows. Without `sed -i 's/\r$//'` the shebang carries a
trailing `\r` and the container dies with "no such file or directory".

### `UV_PROJECT_ENVIRONMENT` must be a venv, not `/usr/local`

The Playwright base image keeps its interpreter elsewhere, so pointing uv at
`/usr/local` fails the build with *"not a valid Python environment (no Python
executable was found)"*. It builds into `/opt/venv`, which is also one directory to
copy between stages.

`uv sync` runs with `--frozen`: a lockfile that has drifted from `pyproject.toml`
fails the build instead of being silently ignored.

### Do not create a user at uid 1001

The base image already has `pwuser` there and `useradd` fails with exit 4. The
container runs as `pwuser`, which is also the account the browser caches belong to.

### `scrapling install` is required

Both browser tiers drive patchright, which ships its own Chromium and does not reuse
the base image's. Skipping this step fails only at runtime, on the worker, for
exactly the pages that needed a browser.

`scrapling[fetchers]` pulls playwright and patchright and **not** camoufox —
Scrapling 0.4.14 does not use it. Verified in the deployed container.

### procrastinate's schema is an Alembic migration

`procrastinate schema --apply` is a separate, non-idempotent command. Revision
`a1c4f9d2e701` applies a pinned copy of the SQL instead, so one migrate step
creates the whole schema and running it twice is safe. The SQL is a file, not a call
to `SchemaManager.get_schema()`: reading it at runtime would mean the revision
produces different output after a procrastinate upgrade.

### Windows-only: the event loop

psycopg 3 refuses to run async on `ProactorEventLoop`, which is Python's default on
Windows, and `uvicorn.run()` reinstates that loop even after the policy is changed.
Hence `app/runtime.py` and the two launcher modules (`app.web`, `app.jobs.worker`)
rather than a bare `uvicorn app.main:app`. On Linux these paths are equivalent, so
none of this affects Railway — it only makes local development possible.

### The worker answers a healthcheck

`railway.json` is shared by every service in the repo, so the healthcheck it
declares applies to the worker too, and a worker with no HTTP server never goes
healthy. It serves `/healthz` on `PORT`. The check reports that the process is
alive, not that the queue is — a check that queried Postgres would fail the service
during a database blip the worker would otherwise ride out.

---

## Cost control

Three separate budgets, in the order they bind:

1. **The size check** runs before anything else and costs one DataForSEO SERP call
   per run. The result is cached per domain in `site_configs` for 30 days.
2. **The review gate.** The crawl does not start until a person approves the plan.
   Excluding a template here is the cheapest decision in the tool.
3. **The fetch ladder** keeps pages on plain HTTP (~40MB) and escalates only on
   demonstrated failure. On the measured run, 60 of 60 pages never left the cheap
   tier.

Firecrawl is a fourth rung, off unless `FIRECRAWL_API_KEY` is set, and reached only
after all three free tiers have failed on a page. **No Firecrawl key exists in
Prosperity.** The only Firecrawl access in the building is the hosted
`prosperity_firecrawl_scrape` MCP tool, which an agent session can reach and a
deployed container cannot. The adapter is built and tested but dormant until a key
is provisioned. A 401 or 402 disables the rung for the rest of a run rather than
paying the same account-level failure once per page.

Worker memory is not padding. A patchright session measures 800MB+ against ~40MB for
plain HTTP, so `MAX_BROWSER_CONCURRENCY=2` and `--disable-dev-shm-usage` are load
bearing: Docker caps `/dev/shm` at 64MB and Chromium crashes without the flag.

---

## Secrets

Same handling as geo-tracker: a gitignored `.env` locally, Railway service variables
in deploy. No Vaultwarden integration, no secrets in the repo.

`OPENAI_API_KEY`, `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` are the same
credentials geo-tracker uses, copied from its `.env`. If those are rotated, both
tools need updating.

`Settings` treats a placeholder as absent — `your_api_key_here`, `change-me`, empty.
A placeholder that counts as "configured" wins provider selection and then fails at
call time; blanking it means the stage degrades to its heuristic path instead, which
is the behaviour actually wanted.

---

## Running it locally

    uv pip install -e ".[dev]"
    cp .env.example .env          # then fill in the secrets
    alembic upgrade head
    python -m app.jobs.worker     # one terminal
    python -m app.web             # another

Needs a Postgres on `DATABASE_URL`. There is one installed via scoop on this
machine; start it with

    pg_ctl -D "C:/Users/rahul/scoop/persist/postgresql16/data" -l "C:/.../pg.log" start

The test suite needs none of this: 122 tests, no network, no key, no database.
`tests/conftest.py` cuts `.env` and the matching shell variables for every test, so
the suite behaves the same here and in CI.
