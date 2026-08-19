# Prosperity llms.txt Generator

Generates spec-compliant `llms.txt` and `llms-full.txt` for client sites.

Internal Prosperity tool. Ported from a Streamlit/Flask generator built at Pattern,
rebuilt on the current stack: **Python 3.12 · FastAPI · Scrapling · Postgres ·
Railway**. Firecrawl, Bifrost, Snowflake and Forklift are all gone.

Driven by the Gumtree Group SOW, which commits Prosperity to llms.txt across
CarsGuide, Gumtree and Autotrader plus ongoing maintenance.

## Status

| Phase | State |
|---|---|
| 1. Scaffold, env registry, Docker, Railway config | done |
| 2. Core engine ported, 32 tests green | done |
| 3. Scrapling crawl layer | not started |
| 4. Postgres + job queue | not started |
| 5. LLM stages (plan / triage / summarise / QA) | not started |
| 6. Web UI | not started |
| 7. Google SSO | not started |
| 8. Railway deploy | not started |

The core generates a valid file today from a Screaming Frog export, with no
network access and no API key.

## Quick start

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
pytest -q
```

Regenerate the golden fixture deliberately, never casually:

```bash
UPDATE_GOLDEN=1 pytest tests/test_pipeline.py::test_golden_output_is_stable
```

## Architecture

```
0  Recon        robots.txt + sitemaps; cluster URLs into path templates   no LLM
1  Plan         LLM proposes include/exclude rules and priorities         LLM
   -- human reviews and edits the plan before anything is crawled --
2  Crawl        Scrapling applies the plan; escalation ladder per page
3  Triage       heuristic score is the prior; LLM assigns sections        LLM
4  Summarise    site summary + per-page title and description             LLM
5  Assemble     llms.txt + llms-full.txt
6  QA           deterministic validators, then an LLM spec review         LLM
7  Edit/export  include/exclude, rename, reorder, download
```

Every LLM stage is optional. With no `OPENAI_API_KEY` the pipeline falls back to
deterministic heuristics and still produces a valid file — and says so, rather than
failing silently the way the source did.

### Layout

| Path | What |
|---|---|
| `app/core/` | The engine. Pure, no I/O, no vendor. This is where the value is. |
| `app/core/ranking.py` | Importance score, Optional classification, section templates |
| `app/core/render.py` | llms.txt / llms-full.txt assembly |
| `app/core/validate.py` | Spec validators |
| `app/core/pipeline.py` | `generate()` and `rebuild()` |
| `app/config.py` | Env registry — every variable is declared here and nowhere else |
| `app/scrape/` | Scrapling layer and Screaming Frog import |
| `app/llm/` | The four LLM stages |
| `app/jobs/` | Background workers |

### Fetch escalation

Cheapest first, because memory is the binding constraint. Scrapling's
`StealthyFetcher` instantiates full Playwright at 800MB+ per concurrent session
against ~40MB for plain HTTP:

`Fetcher` (HTTP) → thin or blocked → `DynamicFetcher` → still blocked →
`StealthyFetcher`, capped by `MAX_BROWSER_CONCURRENCY`.

### Why Screaming Frog import stays

`Link Score`, `Unique Inlinks` and `Crawl Depth` carry 90% of the importance
weighting and no crawler can produce them. Screaming Frog is the only source. Both
inputs normalise into one `PageEntry`.

## Deployment

Railway, `prosperity-media` workspace. One image, two services, dispatched by
`APP_TARGET` — Railway builds only a Dockerfile's final stage and cannot select a
target.

| Service | `APP_TARGET` | `RUN_MIGRATIONS` | Memory |
|---|---|---|---|
| `web` | `web` | `true` | 512 MB |
| `worker` | `worker` | `false` | 2 GB |
| Postgres | — | — | — |

Inherited from `geo-tracker/docs/PROSPERITY.md`, learned the hard way there:

- **Pin `PORT=3000` as a service variable.** Railway injects `PORT=8080`, which
  overrides the Dockerfile and leaves the domain pointing at the wrong port — a 502
  on every request.
- **A variable change does not restart the container.** Run `railway redeploy`.
- **`RUN_MIGRATIONS=true` on exactly one service**, or two migrators race.
- **The database is private-network only.** Local `psql` needs
  `railway connect Postgres --tunnel-only --port 55432`, which needs a key
  registered via `railway ssh keys add`.

## Conventions

- Every new env var goes in `app/config.py` and `.env.example`. Nowhere else.
- Ruff for lint and format. Line length 100.
- No client data in this repo. It belongs in Postgres and the vault.
