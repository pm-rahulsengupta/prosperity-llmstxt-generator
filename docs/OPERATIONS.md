# Operations

Written in the style of `geo-tracker/docs/PROSPERITY.md`: every number here was
measured on this deployment, and every workaround records why it exists. If
something below reads as arbitrary, it is because the obvious alternative was tried
and failed in a way worth remembering.

---

## What is deployed

Railway project **llmstxt-generator**, workspace *prosperity-media's Projects*.
(The project id is in the Railway dashboard; it is deliberately not recorded here,
since this repository is public.)

| Service  | `APP_TARGET` | `RUN_MIGRATIONS` | Domain | Notes |
|----------|--------------|------------------|--------|-------|
| web      | `web`        | `true`           | none yet | FastAPI + HTMX. `PORT=3000` pinned. |
| worker   | `worker`     | `false`          | none | procrastinate worker, concurrency 1. Serves `/healthz` only. |
| Postgres | —            | —                | private | Railway plugin. Replaces Snowflake. |

One image, two services, dispatched by `APP_TARGET` in `docker/entrypoint.sh` —
Railway builds only a Dockerfile's final stage and offers no target selection, so
the final stage carries both runtimes. Copied from geo-tracker.

### Authentication: one signup, then closed

Copied from geo-tracker, which runs `DEPLOYMENT_MODE=local` on its public Railway
domain today — email and password, no identity provider. The rule:

> The first person to reach a fresh deployment registers and becomes the admin.
> Every self-service signup after that is refused, forever. Further accounts are
> created by an admin from `/accounts`.

That is what makes a public URL safe without Google. The only window in which a
stranger could take the instance is between the domain being created and the
intended owner signing up, and it is the owner who is handed the link. Anyone
arriving after that gets a 403 — from `curl` exactly as from the form, because the
rule lives in `app/accounts.py` in the write path, not in a template.

One deliberate improvement on the original. geo-tracker's hook reads `countUsers()`
and then inserts, which two simultaneous signups can both pass. `claim_instance`
takes a Postgres advisory lock and re-checks inside it, so the second attempt
blocks and then loses. `tests/test_signup_gate.py` runs both signups concurrently
against a real database and asserts exactly one account exists afterwards.

Google sign-in is still wired up and takes over the moment
`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are set — the two coexist rather than
being a mode switch. `ALLOWED_EMAIL_DOMAINS` applies only to the Google path.

`ALLOW_ANONYMOUS=true` skips auth entirely, for the test suite and for a local run
with no database. `assert_deployable` refuses it on any https deployment, so it
cannot be what leaves a public instance open.

There is no password reset. To change one, an admin re-creates the account. Send
credentials over Vaultwarden, not email.

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

## Migrations and rollback

`RUN_MIGRATIONS=true` belongs on **exactly one service**. Two services racing
`alembic upgrade` on the same database is how a half-applied schema happens.

Downgrades were tested against populated rows and run cleanly, but they are
**schema rollbacks, not data rollbacks**:

| Revision | Drops | Data lost on downgrade |
|---|---|---|
| `b3e91f70c4aa` | `site_configs.brief` | every onboarding answer |
| `d359a525827a` | `site_metrics` | all uploaded and fetched metrics |
| `db6b5dbeeea3` | `site_configs.observed_shape` | the drift baseline |

Each adds a `server_default`, so applying them to populated tables is safe --
a `NOT NULL` column added without one fails on the existing rows, which is the
failure this project has already hit once.

Take a dump before rolling back anything a person typed.
