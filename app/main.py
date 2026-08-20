"""Routes.

The web service does three things: enqueue work, read the database, and render.
It never crawls and never calls a model in the request cycle -- that is what put
the source behind a gateway timeout on any site worth generating a file for.

Progress is HTMX polling a partial. It is not elegant and it needs no websocket,
no Redis and no sticky sessions, which on a two-service Railway deploy is worth
more than elegance.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.auth import SESSION_KEY, User, build_oauth, current_user, require_user, user_from_claims
from app.config import get_settings
from app.core.pipeline import rebuild
from app.core.render import render_combined
from app.db import repo
from app.db.base import get_session
from app.db.models import RunStatus
from app.llm.prompts.plan import CrawlPlan
from app.scrape.discover import normalise_site_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Boot checks, and the queue connection the web service defers jobs through.

    procrastinate refuses to defer from an unopened app, which is a good default --
    it means a job silently going nowhere is not a reachable state -- but it does
    mean the web service has to own the pool's lifetime explicitly.
    """
    settings.assert_deployable()
    if not settings.llm_enabled:
        logger.warning("No OPENAI_API_KEY: all four LLM stages will use the heuristic path.")
    if not settings.firecrawl_enabled:
        logger.info("No FIRECRAWL_API_KEY: the fetch ladder ends at StealthyFetcher.")

    from app.jobs.queue import app as queue_app

    async with queue_app.open_async():
        yield


app = FastAPI(
    title="Prosperity llms.txt Generator",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="llmstxt_session",
    https_only=settings.app_url.startswith("https://"),
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["sso_enabled"] = settings.sso_enabled
templates.env.globals["llm_enabled"] = settings.llm_enabled
templates.env.globals["firecrawl_enabled"] = settings.firecrawl_enabled
templates.env.globals["size_check_enabled"] = settings.size_check_enabled

oauth = build_oauth(settings) if settings.sso_enabled else None


# -- health -----------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# -- auth -------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if not settings.sso_enabled:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    redirect_uri = f"{settings.app_url.rstrip('/')}/auth/callback"
    # `hd` here only pre-filters the account chooser. The domain is enforced from
    # the ID token in `user_from_claims`, never from this hint.
    first_domain = next(iter(sorted(settings.allowed_domains)), None)
    kwargs = {"hd": first_domain} if first_domain else {}
    return await oauth.google.authorize_redirect(request, redirect_uri, **kwargs)


@app.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request):
    if not settings.sso_enabled:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    token = await oauth.google.authorize_access_token(request)
    user = user_from_claims(token.get("userinfo") or {}, settings)
    request.session[SESSION_KEY] = {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
    }
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# -- runs -------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    user = current_user(request)
    if settings.sso_enabled and user is None:
        return templates.TemplateResponse(request, "login.html", {"user": None})
    runs = await repo.list_runs(session, limit=40)
    return templates.TemplateResponse(request, "index.html", {"user": user, "runs": runs})


@app.post("/runs")
async def create_run(
    request: Request,
    site_url: str = Form(...),
    max_pages: int = Form(0),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run = await repo.create_run(session, normalised, created_by=user.email)
    run_id = str(run.id)
    await session.commit()

    from app.jobs.tasks import preflight_task

    await preflight_task.defer_async(run_id=run_id, requested_max_pages=max_pages)
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    plan = CrawlPlan.from_dict(run.plan or {})
    pages = await repo.get_pages(session, run_id)
    events = await repo.recent_events(session, run_id)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "user": user,
            "run": run,
            "plan": plan,
            "pages": pages,
            "events": events,
            "status": RunStatus(run.status),
        },
    )


@app.get("/runs/{run_id}/progress", response_class=HTMLResponse)
async def run_progress(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """The HTMX poll target. Cheap: one run row and the last 25 events."""
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    events = await repo.recent_events(session, run_id)
    latest = events[-1] if events else None
    response = templates.TemplateResponse(
        request,
        "partials/progress.html",
        {"run": run, "events": events, "latest": latest, "status": RunStatus(run.status)},
    )
    # Tells HTMX to stop polling once there is nothing left to report.
    if RunStatus(run.status).is_terminal or run.status == RunStatus.AWAITING_REVIEW:
        response.headers["HX-Refresh"] = "true"
    return response


@app.post("/runs/{run_id}/plan")
async def save_plan(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Persist the human's edits to the crawl plan, then start the crawl.

    Excluding a template here is the cheapest decision in the whole tool: it is the
    only point at which pages can be removed before they are paid for.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    if run.status != RunStatus.AWAITING_REVIEW:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is {run.status}, not awaiting review.")

    form = await request.form()
    plan = CrawlPlan.from_dict(run.plan or {})
    included = set(form.getlist("include"))
    for rule in plan.rules:
        rule.action = "include" if rule.template in included else "exclude"
    plan.source = "manual"

    run.plan = plan.to_dict()
    run.plan_source = "manual"
    if (cap := form.get("max_pages")) and str(cap).isdigit() and int(cap) > 0:
        run.max_pages = int(cap)

    await repo.save_site_config(
        session, run.domain, plan.to_dict(), run.max_pages, updated_by=user.email
    )
    await repo.record_event(
        session,
        run_id,
        "plan",
        f"Plan approved by {user.email}: {len(included)} of {len(plan.rules)} templates included",
    )
    await session.commit()

    from app.jobs.tasks import generate_task

    await generate_task.defer_async(run_id=str(run_id))
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/runs/{run_id}/pages")
async def edit_pages(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Include/exclude pages, rename the site, reorder sections, and re-render.

    Re-render, not re-derive. `rebuild` keeps the stored section assignments, which
    is the fix for the source defect where unticking one page collapsed the whole
    grouping back to URL paths.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")

    form = await request.form()
    keep = set(form.getlist("keep"))
    pages = await repo.get_pages(session, run_id)
    excluded = {page.url for page in pages if page.url not in keep}

    for page in pages:
        page.included = page.url not in excluded

    order = [name for name in form.getlist("section_order") if name]
    result = _result_from_rows(run, pages)
    rebuilt = rebuild(
        result,
        excluded_urls=excluded,
        site_name=form.get("site_name") or None,
        site_summary=form.get("site_summary") or None,
        section_order=order or None,
    )
    await repo.store_result(session, run, rebuilt)
    await repo.record_event(
        session, run_id, "edit", f"{user.email} kept {len(keep)} of {len(pages)} pages"
    )
    await session.commit()
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/runs/{run_id}/download/{kind}")
async def download(
    run_id: UUID,
    kind: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")

    bodies = {
        "llms.txt": run.llmstxt,
        "llms-full.txt": run.llms_full,
        "combined.txt": render_combined(run.llmstxt, run.llms_full),
    }
    if kind not in bodies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown file.")
    if not bodies[kind]:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{kind} has not been generated yet.")

    return Response(
        content=bodies[kind],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}"'},
    )


def _result_from_rows(run, pages) -> GenerationResult:  # noqa: F821 -- forward ref for brevity
    """Reconstruct a `GenerationResult` from stored rows, for re-rendering."""
    from app.core.models import GenerationResult, Section

    grouped: dict[str, list] = {}
    optional = []
    for page in sorted(pages, key=lambda p: p.position):
        entry = page.to_entry()
        if page.is_optional:
            optional.append(entry)
        else:
            grouped.setdefault(page.section_name or "Pages", []).append(entry)

    return GenerationResult(
        site_url=run.site_url,
        site_name=run.site_name,
        site_summary=run.site_summary,
        pattern=run.pattern,
        sections=[
            Section(name=name, pages=entries, position=position)
            for position, (name, entries) in enumerate(grouped.items())
        ],
        optional=optional,
        llmstxt=run.llmstxt,
        llms_full=run.llms_full,
        pages_total=len(pages),
    )
