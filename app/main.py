"""Routes.

The web service does three things: enqueue work, read the database, and render.
It never crawls and never calls a model in the request cycle -- that is what put
the source behind a gateway timeout on any site worth generating a file for.

Progress is HTMX polling a partial. It is not elegant and it needs no websocket,
no Redis and no sticky sessions, which on a two-service Railway deploy is worth
more than elegance.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app import accounts
from app.auth import (
    User,
    build_oauth,
    current_user,
    require_admin,
    require_admin_or_404,
    require_user,
    sign_in,
    user_from_claims,
)
from app.config import get_settings
from app.core.agents_doc import (
    Capability,
    build_agents_doc,
    links_from_pages,
    profile_for,
)
from app.core.agents_render import render_agents_liquid, render_agents_md
from app.core.ai_catalog import CONTENT_TYPE as CATALOG_TYPE
from app.core.ai_catalog import build_catalog, render_catalog
from app.core.bundle import (
    EFFORT_LABELS,
    EFFORT_OWNERS,
    build_bundle,
    verify_declared,
)
from app.core.components import (
    FAMILY_BLURBS,
    FAMILY_LABELS,
    ComponentState,
    Family,
    SiteType,
    by_key,
)
from app.core.edits import EditTarget, apply_operations
from app.core.evidence import JUDGED_BY, reports_for
from app.core.metrics import DateRange
from app.core.onboarding import QUESTIONS, SiteBrief, brief_from_answers
from app.core.pipeline import rebuild
from app.core.pricing import SERP_CALL_USD, cost_of, rate_for, totals_of, usd
from app.core.ranking import (
    PATTERN_AGENCY,
    PATTERN_LABELS,
    PATTERN_TEMPLATES,
)
from app.core.render import render_combined
from app.core.site_state import derive, manually_markable
from app.core.snapshot import (
    declared_from_list,
    declared_to_list,
    probe_from_dict,
    probe_to_dict,
    readiness_from_dict,
    readiness_to_dict,
    tech_from_dict,
    tech_to_dict,
)
from app.core.templates_lib import build_templates
from app.db import repo
from app.db.base import get_session, session_scope
from app.db.models import ChatMessage, DocumentRevision, RunStatus
from app.llm.client import LLMClient, LLMUsage
from app.llm.prompts.plan import CrawlPlan
from app.llm.stages import apply_chat_turn, suggest_brief
from app.metrics.gsc_csv import parse_gsc_export
from app.nav import build_nav
from app.scrape.agents_probe import probe_site
from app.scrape.discover import discover, normalise_site_url
from app.scrape.extract import extract
from app.scrape.readiness import audit_readiness, sample_from_sources
from app.scrape.tech_probe import probe_tech

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
# Python's mimetypes table has no woff2 on a stock Windows install, so StaticFiles
# serves it as application/octet-stream -- which makes the browser discard the
# `<link rel=preload as=font>` hint and fetch the file a second time.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["usd"] = usd
# Called from base.html on every render. A global rather than a context key each
# route must remember: sixteen routes render templates, and the one that forgot
# would 500 on a page that has nothing to do with navigation.
templates.env.globals["build_nav"] = build_nav


def _asset_version(name: str) -> str:
    """A content hash for a static file, so the cache-buster cannot go stale.

    It was a hand-written `?v=3` and it stayed at 3 through four rewrites of the
    stylesheet. Anyone whose browser had cached v=3 kept being served CSS from
    before the sidebar existed -- the markup shipped, the styles did not, and the
    feature looked missing rather than broken.

    Hashing the file removes the step a person has to remember. Read once at
    import: the file cannot change without a redeploy, and a redeploy re-imports.
    """
    path = ROOT / "static" / name
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        # Never fatal. A missing hash costs cache-busting, not the page.
        return "dev"


templates.env.globals["asset_version"] = _asset_version
templates.env.globals["effort_labels"] = EFFORT_LABELS
templates.env.globals["effort_owners"] = EFFORT_OWNERS
templates.env.globals["pattern_labels"] = PATTERN_LABELS
templates.env.globals["pattern_sections"] = PATTERN_TEMPLATES
templates.env.globals["sso_enabled"] = settings.sso_enabled
templates.env.globals["allow_anonymous"] = settings.allow_anonymous
templates.env.globals["llm_enabled"] = settings.llm_enabled
templates.env.globals["firecrawl_enabled"] = settings.firecrawl_enabled
templates.env.globals["size_check_enabled"] = settings.size_check_enabled

oauth = build_oauth(settings) if settings.sso_enabled else None


# -- health -----------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# -- auth -------------------------------------------------------------------


@app.get("/login/google", include_in_schema=False)
async def login_google(request: Request):
    if not settings.sso_enabled:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
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
    sign_in(request, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, session: AsyncSession = Depends(get_session)):
    """Sign in, or -- on a brand-new instance -- claim it.

    A fresh deployment has no accounts, so this redirects to /signup. That window
    is the only time a stranger could take the instance, which is why the person
    given the URL should be the person who opens it first.
    """
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    if not await accounts.is_bootstrapped(session):
        return RedirectResponse("/signup", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    row = await accounts.authenticate(session, email, password)
    if row is None:
        # One message for both halves: a wrong address and a wrong password are
        # indistinguishable, so the form cannot be used to enumerate accounts.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "That email and password do not match an account."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    sign_in(request, User(email=row.email, name=row.name, is_admin=row.is_admin))
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request, session: AsyncSession = Depends(get_session)):
    if await accounts.is_bootstrapped(session):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "signup.html", {"user": None, "error": None})


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Claim the instance. Succeeds exactly once, for anyone, ever.

    The rule is enforced in `accounts.claim_instance` under an advisory lock, so
    this endpoint answers a curl the same way it answers the form, and two
    simultaneous attempts cannot both win.
    """
    try:
        row = await accounts.claim_instance(session, email, password, name)
    except (accounts.SignupClosed, accounts.WeakPassword) as exc:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"user": None, "error": str(exc)},
            status_code=status.HTTP_403_FORBIDDEN
            if isinstance(exc, accounts.SignupClosed)
            else status.HTTP_400_BAD_REQUEST,
        )
    await session.commit()
    sign_in(request, User(email=row.email, name=row.name, is_admin=row.is_admin))
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = await accounts.list_users(session)
    return templates.TemplateResponse(
        request, "accounts.html", {"user": user, "accounts": rows, "error": None}
    )


@app.post("/accounts", response_class=HTMLResponse)
async def create_account(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """The operator path: how the second and subsequent accounts come to exist."""
    admin = await accounts.find_by_email(session, user.email)
    if admin is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account no longer exists.")
    try:
        await accounts.create_teammate(session, admin, email, password, name)
    except (ValueError, PermissionError) as exc:
        rows = await accounts.list_users(session)
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {"user": user, "accounts": rows, "error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    await session.commit()
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# -- runs -------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    """The llms.txt run starter, and recent runs.

    This used to be the whole front door, which is why the tool read as being
    about runs rather than about clients: a run was the only object with a list,
    so finding a client meant finding one of their runs. `/clients` is the front
    door now and this is one of the things you do to a client.
    """
    user = current_user(request)
    if user is None and not settings.allow_anonymous:
        # /login sends a brand-new instance on to /signup.
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
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
    run.max_pages = max_pages
    run_id = str(run.id)

    existing = await repo.load_site_config(session, run.domain)
    await session.commit()

    # A domain nobody has onboarded stops here once. `brief` is `{}` only when the
    # form has never been submitted -- skipping writes an empty answered brief, so
    # a deliberate skip is not mistaken for an unanswered question next time.
    if existing is None or not existing.brief:
        return RedirectResponse(
            f"/sites/{run.domain}/brief?run={run_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    return await _start_preflight(session, run_id)


def _brief_form_values(brief: SiteBrief) -> dict[str, str]:
    """Render a stored brief back into the textareas it came from."""
    facts = "\n".join(
        f"{name} = {fact.value}" + (f" = {fact.source}" if fact.source != "operator" else "")
        for name, fact in sorted(brief.facts.items())
    )
    return {
        "primary_action": brief.primary_action.value,
        "ai_bot_policy": brief.ai_bot_policy.value,
        "mcp_server_url": "\n".join(brief.mcp_server_url),
        "a2a_agent_url": "\n".join(brief.a2a_agent_url),
        "openapi_url": "\n".join(brief.openapi_url),
        "found_for": brief.found_for,
        "audience": brief.audience,
        "rate_limit_note": brief.rate_limit_note,
        "valuable": "\n".join(brief.valuable),
        "noise": "\n".join(brief.noise),
        "must_appear": "\n".join(sorted(brief.must_appear)),
        "embargoed": "\n".join(brief.embargoed),
        "facts": facts,
    }


def _parse_facts(raw: str) -> dict[str, dict[str, str]]:
    """`name = value` or `name = value = source`, one per line.

    A structured editor would be better and is not worth building before anyone
    has used this. What matters is that a value keeps its provenance, so the
    third field is preserved when given rather than being dropped into a note.
    """
    facts: dict[str, dict[str, str]] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        name, _, rest = line.partition("=")
        value, _, source = rest.partition("=")
        if not (name := name.strip()) or not (value := value.strip()):
            continue
        facts[name] = {"value": value, "source": source.strip() or "operator"}
    return facts


@app.get("/sites/{domain}/brief", response_class=HTMLResponse)
async def brief_form(
    request: Request,
    domain: str,
    run: str | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    stored = await repo.load_brief(session, domain)
    return templates.TemplateResponse(
        request,
        "brief.html",
        {
            "user": user,
            "domain": domain,
            "questions": QUESTIONS,
            "answers": _brief_form_values(stored),
            "run_id": run,
            "drift_reason": request.query_params.get("drift"),
            "metrics": await repo.metrics_summary(session, domain),
            "imported": request.query_params.get("imported"),
            "import_notes": request.query_params.get("notes"),
            "gsc_enabled": settings.gsc_enabled,
            "suggested": [],
            "reasoning": "",
            "llm_used": False,
            "dropped": [],
            "readiness": None,
        },
    )


@app.post("/sites/{domain}/brief/suggest", response_class=HTMLResponse)
async def suggest_brief_route(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Analyse the site and pre-fill the brief for review.

    The whole reason the wizard exists: thirteen empty fields is a form nobody
    finishes, and reviewing a wrong answer takes seconds where writing a right one
    from scratch takes minutes. Nothing is saved here -- the suggestions are
    rendered into the same form the operator would have filled in by hand, and the
    save button is still theirs to press.
    """
    settings = get_settings()
    site_url = f"https://{domain}"

    recon = await discover(site_url, settings.crawl_user_agent)
    tech = await probe_tech(site_url, settings.crawl_user_agent)

    homepage = ""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20, headers={"User-Agent": settings.crawl_user_agent}
        ) as client:
            response = await client.get(site_url)
            # The reader, not the raw HTML. A model given 490KB of inlined CSS
            # spends its context on stylesheets and reasons from whatever text
            # survived, which on a WP Rocket site is very little of it.
            homepage = extract(response.text, site_url).markdown[:4000]
    except (httpx.HTTPError, ValueError):
        # A site we cannot read still has sitemap groups to reason from, which is
        # most of the signal; failing the whole suggestion over the homepage
        # would send the operator back to an empty form for no reason.
        homepage = ""

    # The readiness audit runs here too. The wizard is already reading the site,
    # and an operator filling in a brief is exactly the person who wants to know
    # what the site is missing -- telling them later, on a different page, is
    # telling them once they have stopped looking.
    # One page per sitemap group, so the page checks see the templates rather than
    # only the homepage -- which is the least representative page most sites have.
    readiness = await audit_readiness(
        site_url,
        settings.crawl_user_agent,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
        sample_urls=sample_from_sources(recon.url_sources),
    )

    stored = await repo.load_brief(session, domain)
    usage = LLMUsage()
    suggestion = await suggest_brief(
        LLMClient(settings, usage),
        site_url,
        homepage[:400],
        tech.platform.value,
        recon.sitemap_groups(),
        recon.urls[:40],
        homepage,
        known_urls=recon.urls,
    )

    # Suggestions fill only what the operator has not already answered. Overwriting
    # a considered answer with a guess is the one way this feature could do harm.
    answers = _brief_form_values(stored)
    filled = []
    for key, value in suggestion.items():
        if key.startswith("_") or not value:
            continue
        if not answers.get(key):
            answers[key] = value
            filled.append(key)

    return templates.TemplateResponse(
        request,
        "brief.html",
        {
            "user": user,
            "domain": domain,
            "questions": QUESTIONS,
            "answers": answers,
            "run_id": request.query_params.get("run"),
            "drift_reason": None,
            "metrics": await repo.metrics_summary(session, domain),
            "imported": None,
            "import_notes": None,
            "gsc_enabled": settings.gsc_enabled,
            "suggested": filled,
            "reasoning": suggestion.get("_reasoning", ""),
            "llm_used": bool(suggestion),
            "dropped": suggestion.get("_dropped", []),
            "readiness": readiness,
        },
    )


@app.post("/sites/{domain}/brief")
async def save_brief(
    request: Request,
    domain: str,
    run: str | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    answers = {q.key: str(form.get(q.key) or "") for q in QUESTIONS}
    answers["facts"] = _parse_facts(answers.get("facts", ""))
    # The baseline is whatever the site looked like at the last preflight. Stamping
    # it here, at the moment a person answers, is what makes drift mean "the site
    # moved since you told us about it" rather than "the site moved at some point".
    # On a first run there is nothing observed yet and the baseline is empty, which
    # `detect_drift` reads as "no baseline" rather than as "everything is new".
    observed = await repo.load_observed_shape(session, domain)
    brief = brief_from_answers(answers, answered_by=user.email, shape=observed)
    await repo.save_brief(session, domain, brief)
    # Retroactive by design: an operator adding an embargo is normally reacting to
    # something already crawled, so a forward-only guarantee would miss the exact
    # pages that prompted the answer.
    purge = await repo.purge_embargoed(session, domain, brief.embargoed)
    await session.commit()
    if purge.anything:
        # Logged with the URLs, not just a count. "Three pages were removed" is
        # not enough to confirm the right three went, and an embargo that removed
        # the wrong thing needs to be answerable without a database session.
        logger.info(
            "embargo purge for %s by %s: %s | urls=%s",
            domain,
            user.email,
            purge.summary(),
            list(purge.urls),
        )

    if run:
        return await _start_preflight(session, run)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/sites/{domain}/brief/skip", include_in_schema=False)
async def skip_brief(
    domain: str,
    run: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Start the run unbriefed.

    Recorded as an empty answered brief rather than left null, so the next run on
    this domain does not stop and ask again. Skipping is a decision; it should
    only have to be made once.
    """
    await repo.save_brief(session, domain, SiteBrief(answered_by=user.email))
    await session.commit()
    return await _start_preflight(session, run)


async def _start_preflight(session: AsyncSession, run_id: str) -> RedirectResponse:
    run = await repo.get_run(session, UUID(run_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    from app.jobs.tasks import preflight_task

    await preflight_task.defer_async(run_id=run_id, requested_max_pages=run.max_pages or 0)
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


MAX_METRICS_UPLOAD = 25 * 1024 * 1024


@app.post("/sites/{domain}/metrics")
async def upload_metrics(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Take a Search Console Pages export and use it as this domain's metrics.

    The service-account path needs the client to grant access, which takes days
    and is sometimes refused outright. This path works the same afternoon and on
    properties we will never be granted -- which, on the agency side, is most of
    them.
    """
    form = await request.form()
    upload = form.get("export")
    if upload is None or not getattr(upload, "filename", ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file was chosen.")

    payload = await upload.read()
    if len(payload) > MAX_METRICS_UPLOAD:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That file is {len(payload) / 1_048_576:.0f} MB; the limit is 25 MB.",
        )

    window = None
    start, end = str(form.get("window_start") or ""), str(form.get("window_end") or "")
    if start and end:
        try:
            window = DateRange(date.fromisoformat(start), date.fromisoformat(end))
        except ValueError:
            window = None

    try:
        imported = parse_gsc_export(payload, window=window)
    except (ValueError, zipfile.BadZipFile) as exc:
        # The parser's messages name the actual problem -- wrong export tab, no URL
        # column -- so they are shown rather than replaced with a generic failure.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    written = await repo.replace_site_metrics(
        session, domain, imported.metrics, source="gsc-upload", uploaded_by=user.email
    )
    await session.commit()

    notes = "; ".join(imported.notes)
    return RedirectResponse(
        f"/sites/{domain}/brief?imported={written}" + (f"&notes={quote(notes)}" if notes else ""),
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def probe_site_live(session: AsyncSession, normalised: str):
    """Every network call the tool makes about a site. **Never from a GET.**

    Roughly thirty requests to one host: fourteen probing agent surfaces and the
    stack, fourteen to eighteen auditing readiness, one per declared endpoint,
    plus the sitemaps. Nine of those are strictly sequential, so a slow host can
    hold this open for minutes.

    It used to run on every page render. Opening a client meant probing them, and
    clicking between the six family tabs probed them six more times -- against a
    thirty-second healthcheck, and at a stranger's host for any domain someone
    typed into the URL bar. Now it runs in a job and writes one row, and the
    pages read that row.

    `test_no_get_route_probes_a_clients_site` asserts the "never from a GET" part
    rather than trusting this docstring.
    """
    settings = get_settings()
    # Both probes together: one asks what agent-facing files exist, the other what
    # the site is built on and what machine-readable surfaces answer. Concurrent
    # because neither depends on the other and the page waits on the slower.
    # Sequential, not concurrent. Together these are fourteen requests to one
    # host; issued at once they were refused often enough that a live agency site
    # reported every agent surface as unreachable. Each is fast on its own and
    # the page is not waiting on a crawl.
    probe = await probe_site(normalised, settings.crawl_user_agent)
    tech = await probe_tech(normalised, settings.crawl_user_agent)
    domain = urlparse(normalised).netloc
    brief = await repo.load_brief(session, domain)

    # Links come from a completed llms.txt run for the same domain. The two files
    # describe one site, so pages that crawl already fetched are pages this one
    # can cite -- the evidence rule met by a different means rather than waived.
    # Endpoints the tech probe confirmed are citable on the same terms as anything
    # else here: each answered with the right content type, so each is a capability
    # rather than a convention someone expects to exist.
    # The readiness audit runs after the probes, not alongside. It is nine more
    # requests to the same host, and firing them concurrently with the other two
    # is how a small site starts refusing us and we report our own impatience as
    # its shortcomings.
    #
    # It samples one page per sitemap group, by the same rule the onboarding
    # wizard uses. Until now this route read the homepage alone: the same site
    # scored 42 in the wizard and 53 here, and nothing on either page said the
    # two numbers came from different samples. The sitemaps are re-read rather
    # than recovered from the crawl because the crawl does not record which
    # sitemap a page came from, and on a flat WordPress site the URL path
    # carries no distinction to fall back on.
    try:
        recon = await discover(normalised, settings.crawl_user_agent)
        page_sample = sample_from_sources(recon.url_sources)
    except (httpx.HTTPError, ValueError):
        # A site whose sitemaps we cannot read still gets a homepage audit, and
        # `report.sampled` will say that is all it was.
        page_sample = []

    readiness = await audit_readiness(
        normalised,
        settings.crawl_user_agent,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
        sample_urls=page_sample,
    )

    # Declared endpoints are verified here rather than at render, because each is
    # its own network call -- exactly the cost this split exists to remove from
    # the request path. `verified` is stored, and the codec reading it back
    # defaults to False, so a malformed row fails closed.
    declared = await verify_declared(brief, settings.crawl_user_agent)
    return probe, tech, readiness, declared


def _assemble(
    normalised: str,
    domain: str,
    probe,
    tech,
    readiness,
    declared,
    brief,
    config,
    run,
    pages,
):
    """Build the document, catalog and bundle. No network, no database.

    Everything here is a pure function of what the probe already established plus
    what the database already holds, which is what makes it safe to run on a GET.
    Kept as one function shared by the page and the download so both produce the
    same file -- an operator downloading something other than what they reviewed
    is the kind of divergence nobody notices until a client does.
    """
    # Links come from a completed llms.txt run for the same domain. The two files
    # describe one site, so pages that crawl already fetched are pages this one
    # can cite -- the evidence rule met by a different means rather than waived.
    # Endpoints the tech probe confirmed are citable on the same terms as anything
    # else here: each answered with the right content type, so each is a capability
    # rather than a convention someone expects to exist.
    read_only: list = [
        Capability(label=d.name, url=d.url, evidence=f"answered {d.evidence}")
        for d in tech.endpoints
    ]
    policies: list = []
    contact = ""
    if run is not None and pages:
        crawled, policies, contact = links_from_pages(
            [(p.url, p.title or "") for p in pages if p.included]
        )
        # Machine-readable endpoints first: an agent can parse those, and the
        # crawled pages are the human-readable fallback behind them.
        read_only = read_only + crawled

    # The profile comes from that same run when there is one: agents.md and
    # llms.txt describe the same site, and disagreeing about its shape would be
    # its own defect. Absent that, the agency profile wins, which cannot transact.
    # The operator's stated goal first, then the profile a completed llms.txt run
    # settled on, then the detected platform. A stated goal outranks a detected
    # plugin because the plugin is a fact about the build and the goal is a fact
    # about the business, and only the second belongs in an instruction file.
    profile = profile_for(
        brief.primary_action.value,
        detected=(config.plan or {}).get("site_pattern", "") if config else "",
        platform_sells=tech.sells,
    )

    doc = build_agents_doc(
        probe,
        profile or PATTERN_AGENCY,
        site_name=(config.label if config and config.label else domain),
        read_only=read_only,
        policies=policies,
        contact_url=contact,
        rate_limit_note=brief.rate_limit_note,
    )
    doc.summary = f"> {brief.found_for}" if brief.found_for else ""
    doc.agent_guidance = f"Canonical site: {normalised}"
    doc.platform = tech.platform.value
    doc.notes.extend(tech.notes)
    catalog = build_catalog(probe, tech, site_name=doc.site_name)
    rendered_catalog = render_catalog(catalog) if catalog.worth_publishing else ""
    bundle = build_bundle(
        normalised,
        brief,
        declared=declared,
        llms_txt=(run.llmstxt if run else ""),
        llms_full=(run.llms_full if run else ""),
        agents_md=render_agents_md(doc),
        ai_catalog=rendered_catalog,
        sitemap_url=f"{normalised}/sitemap.xml",
        platform=tech.platform.value,
    )
    return doc, catalog, bundle


def _ago(when: datetime) -> str:
    """How long ago, in words a reader can act on.

    Deliberately coarse. Claiming "3 minutes ago" from a row written 200 seconds
    back is precision the reader cannot use and the tool has not earned.

    One definition, used by both `SiteView.checked_ago` and the client list. Two
    would drift, and a list saying "2 hours ago" beside a page saying "yesterday"
    for the same row is the kind of small disagreement that makes an operator
    stop believing either number.
    """
    seconds = max(0, int((datetime.now(UTC) - when).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86_400:
        hours = seconds // 3600
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = seconds // 86_400
    return f"{days} day{'' if days == 1 else 's'} ago"


@dataclass(frozen=True, slots=True)
class SiteView:
    """One client's stored probe, assembled for rendering.

    Carries `fetched_at` as a field rather than leaving it in the row, because
    every page that shows a figure from this object must also show when it was
    taken. A cached number presented as a live one is the single failure this
    caching could introduce, and making the timestamp travel with the data is
    what stops a template forgetting it.
    """

    domain: str
    site_url: str
    probe: object
    doc: object
    tech: object
    catalog: object
    readiness: object
    bundle: object
    fetched_at: datetime
    fetched_by: str = ""
    duration_ms: int = 0
    # URLs a completed crawl fetched. Carried here because `agents.md` cites them
    # and AGT-004 has to agree that they are evidence -- see `app/core/evidence.py`.
    crawled_urls: tuple[str, ...] = ()

    @property
    def age(self) -> timedelta:
        return datetime.now(UTC) - self.fetched_at

    @property
    def checked_ago(self) -> str:
        """Plain words, because "2026-08-21T05:12:44Z" is not an age."""
        return _ago(self.fetched_at)

    @property
    def is_stale(self) -> bool:
        """A day old. Not an error -- a prompt to refresh before quoting it."""
        return self.age > timedelta(days=1)


async def _from_snapshot(session: AsyncSession, domain: str):
    """Everything the site pages render, from the stored probe. No network.

    Returns `None` when the domain has never been checked. `None` means "not
    checked", and the pages say exactly that rather than probing to find out --
    the distinction between absent evidence and negative evidence, applied to the
    tool's own state rather than to a client's site.
    """
    snapshot = await repo.load_snapshot(session, domain)
    if snapshot is None:
        return None

    normalised = f"https://{domain}"
    probe = probe_from_dict(snapshot.probe)
    tech = tech_from_dict(snapshot.tech)
    readiness = readiness_from_dict(snapshot.readiness)
    declared = declared_from_list(snapshot.probe.get("declared"))

    config = await repo.load_site_config(session, domain)
    brief = await repo.load_brief(session, domain)
    run = await repo.latest_complete_run(session, domain)
    pages = await repo.get_pages(session, run.id) if run is not None else []

    doc, catalog, bundle = _assemble(
        normalised, domain, probe, tech, readiness, declared, brief, config, run, pages
    )
    return SiteView(
        domain=domain,
        site_url=normalised,
        probe=probe,
        doc=doc,
        tech=tech,
        catalog=catalog,
        readiness=readiness,
        bundle=bundle,
        fetched_at=snapshot.fetched_at,
        fetched_by=snapshot.fetched_by,
        duration_ms=snapshot.duration_ms,
        crawled_urls=tuple(p.url for p in pages if p.included),
    )


# -- clients ----------------------------------------------------------------
#
# The section that did not exist. `app/nav.py` described site-scoped links as
# pointing at "the picker" and there was no picker: the only route to a client
# was finding one of their runs among the last forty on the index and clicking
# it, so a client whose runs had scrolled off was reachable only by typing a URL.


@app.get("/clients", response_class=HTMLResponse)
async def clients_page(
    request: Request,
    deleted: str = "",
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Every client, with enough state to decide which one needs attention.

    Reads only stored rows -- no client's server is touched by loading this,
    however many of them there are.
    """
    rows = []
    for config in await repo.list_site_configs(session):
        snapshot = await repo.load_snapshot(session, config.domain)
        summary = None
        if snapshot is not None:
            summary = {
                "score": readiness_from_dict(snapshot.readiness).score,
                "checked_ago": _ago(snapshot.fetched_at),
                "is_stale": datetime.now(UTC) - snapshot.fetched_at > timedelta(days=1),
            }
        rows.append(
            {
                "domain": config.domain,
                "label": config.label,
                "onboarded": bool(config.brief),
                "snapshot": summary,
            }
        )

    return templates.TemplateResponse(
        request, "clients.html", {"user": user, "rows": rows, "deleted": deleted}
    )


@app.get("/clients/new", response_class=HTMLResponse)
async def new_client_page(request: Request, user: User = Depends(require_user)):
    """Onboarding as a destination rather than an interstitial.

    The brief and its wizard already existed and were good; they could only be
    reached by starting a crawl, and only ever once per domain. This is the door.
    """
    return templates.TemplateResponse(request, "client_new.html", {"user": user, "error": None})


@app.post("/clients/new")
async def create_client(
    request: Request,
    site_url: str = Form(...),
    label: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "client_new.html",
            {"user": user, "error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    domain = urlparse(normalised).netloc
    existing = await repo.load_site_config(session, domain)
    await repo.save_site_config(
        session,
        domain,
        plan=(existing.plan or {}) if existing else {},
        max_pages=existing.max_pages if existing else 0,
        updated_by=user.email,
        label=label.strip(),
    )
    await session.commit()
    # Straight into the brief, which is where onboarding actually happens.
    return RedirectResponse(f"/sites/{domain}/brief", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/sites/{domain}", response_class=HTMLResponse)
async def client_home(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """One client, whole.

    Everything `SiteConfig` holds plus the run history, the marks and the search
    metrics -- which between them lived on four different pages, or on none.
    """
    config = await repo.load_site_config(session, domain)
    site_status, view = await _site_status(session, domain)
    if site_status is None:
        return _unchecked(request, user, domain, "Overview")

    runs = [r for r in await repo.list_runs(session, limit=200) if r.domain == domain][:10]

    return templates.TemplateResponse(
        request,
        "client_home.html",
        {
            **_component_context(request, user, domain, site_status, view),
            "label": (config.label if config and config.label else ""),
            "readiness": view.readiness,
            "probe": view.probe,
            "status": site_status,
            "family_rows": site_status.family_counts(),
            "client_count": len(site_status.for_client()),
            "dev_count": len(site_status.for_developer()),
            "runs": runs,
            "brief": await repo.load_brief(session, domain),
            "onboarded": bool(config.brief) if config else False,
            "mark_count": len(await repo.load_marks(session, domain)),
            "metrics": await repo.metrics_summary(session, domain),
        },
    )


@app.post("/sites/{domain}/refresh")
async def refresh_client(
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Re-probe the site and store it. Synchronous, because someone clicked it.

    The worker runs at concurrency 1, so a queued refresh can sit behind an
    in-flight crawl for minutes. For a button an operator is watching, waiting on
    the probe beats waiting on a queue with no way to tell which is happening.
    """
    await _check_and_store(session, f"https://{domain}", domain, user.email)
    await session.commit()
    return RedirectResponse(f"/sites/{domain}", status_code=status.HTTP_303_SEE_OTHER)


async def _settings_context(session: AsyncSession, user: User, domain: str, error: str | None):
    config = await repo.load_site_config(session, domain)
    return {
        "user": user,
        "domain": domain,
        "label": config.label if config else "",
        "exists": config is not None,
        "going": await repo.preview_client_deletion(session, domain),
        "error": error,
    }


@app.get("/sites/{domain}/settings", response_class=HTMLResponse)
async def client_settings(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "client_settings.html", await _settings_context(session, user, domain, None)
    )


@app.post("/sites/{domain}/settings")
async def save_client_settings(
    domain: str,
    label: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    config = await repo.load_site_config(session, domain)
    await repo.save_site_config(
        session,
        domain,
        plan=(config.plan or {}) if config else {},
        max_pages=config.max_pages if config else 0,
        updated_by=user.email,
        label=label.strip(),
    )
    await session.commit()
    return RedirectResponse(f"/sites/{domain}/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/sites/{domain}/delete")
async def delete_client_route(
    request: Request,
    domain: str,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Remove a client and everything keyed to its domain.

    Admin-only with a typed confirmation, matching the run delete it sits beside.
    The confirmation is the domain itself, so an operator with several clients
    open cannot destroy the wrong one by muscle memory.
    """
    form = await request.form()
    if str(form.get("confirm") or "").strip().lower() != domain.lower():
        context = await _settings_context(
            session, user, domain, f"Type {domain} to confirm. Nothing was deleted."
        )
        return templates.TemplateResponse(
            request, "client_settings.html", context, status_code=status.HTTP_400_BAD_REQUEST
        )

    removed = await repo.delete_client(session, domain)
    await session.commit()
    return RedirectResponse(
        "/clients?deleted=" + quote(f"{domain}: {removed.summary()}"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, user: User = Depends(require_user)):
    """The "check a site" form, for a domain that is not on file yet."""
    return templates.TemplateResponse(request, "agents.html", {"user": user, "site_url": ""})


@app.post("/agents", response_class=HTMLResponse)
async def agents_generate(
    request: Request,
    site_url: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Probe the site once, store the result, then show the client.

    A POST rather than a GET because it costs a client's server roughly thirty
    requests. That is fine as a thing someone asked for and wrong as a thing that
    happens because they clicked a tab.

    Still synchronous: an operator who has just typed a URL is waiting for this
    specific answer, and a job plus a status page would be slower for them than
    the probe. The background job exists for refreshing a client already on file,
    where nobody is watching.
    """
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    domain = urlparse(normalised).netloc
    await _check_and_store(session, normalised, domain, user.email)

    # Checking a site makes it a known client. Without this the snapshot exists,
    # `/sites/{domain}` renders, and `/clients` -- which lists configs -- never
    # shows it: a working page nothing links to.
    if await repo.load_site_config(session, domain) is None:
        await repo.save_site_config(session, domain, plan={}, max_pages=0, updated_by=user.email)
    await session.commit()
    return RedirectResponse(f"/sites/{domain}", status_code=status.HTTP_303_SEE_OTHER)


async def _check_and_store(
    session: AsyncSession, normalised: str, domain: str, checked_by: str
) -> None:
    """Run the live probe and write the snapshot. The only writer of that row.

    One writer rather than one per caller, so the page, the refresh button and
    the background job cannot store subtly different shapes -- which the codec
    would then read back as a plausible object with the wrong values in it.
    """
    started = datetime.now(UTC)
    probe, tech, readiness, declared = await probe_site_live(session, normalised)
    elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)

    stored_probe = probe_to_dict(probe)
    # Carried inside the probe blob rather than in its own column: it is part of
    # what one check established, and a column would have to be migrated the next
    # time an operator can declare one more kind of endpoint.
    stored_probe["declared"] = declared_to_list(declared)

    await repo.save_snapshot(
        session,
        domain,
        probe=stored_probe,
        readiness=readiness_to_dict(readiness),
        tech=tech_to_dict(tech),
        fetched_by=checked_by,
        duration_ms=elapsed,
    )


@app.get("/agents/download")
async def agents_download(
    site: str,
    kind: str = "md",
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve the file built from the snapshot the operator was just looking at.

    This used to re-probe on every download, arguing that a remembered document
    could name an endpoint the site had since withdrawn. The argument was right
    about staleness and wrong about the remedy: re-probing meant the downloaded
    file could differ from the one on screen, which is the divergence
    `_assemble` exists to prevent, and it put thirty requests on a GET.

    The staleness is now handled where it belongs -- every page shows how old its
    snapshot is and offers Refresh, so an operator downloads a file they have
    seen the age of.
    """
    normalised = normalise_site_url(site)
    domain = urlparse(normalised).netloc
    view = await _from_snapshot(session, domain)
    if view is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{domain} has not been checked yet, so there is no file to download.",
        )
    doc, catalog, bundle = view.doc, view.catalog, view.bundle

    # Any file the bundle produced, by name. Checked first because the family
    # tabs link here by artefact name -- without it every one of those links fell
    # through to the default and returned agents.md instead, which is a download
    # that looks like it worked.
    if (artifact := bundle.get(kind)) is not None:
        return PlainTextResponse(
            artifact.body,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
            media_type=f"{artifact.media_type}; charset=utf-8",
        )

    if kind == "liquid":
        body, filename, media = render_agents_liquid(doc), "agents.md.liquid", "text/markdown"
    elif kind == "catalog":
        # Refused rather than emitted empty. A catalog listing one document is
        # noise wearing a standard's clothes, and the operator needs to know that
        # rather than receive a file that looks finished.
        if not catalog.worth_publishing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                catalog.notes[0] if catalog.notes else "Nothing verified to catalogue.",
            )
        body, filename, media = render_catalog(catalog), "ai-catalog.json", CATALOG_TYPE
    else:
        body, filename, media = render_agents_md(doc), "agents.md", "text/markdown"

    return PlainTextResponse(
        body,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type=f"{media}; charset=utf-8",
    )


async def _site_status(session: AsyncSession, domain: str):
    """Everything the site pages render, derived once from the stored probe.

    One derivation shared by every page is what stops the family tabs, the client
    checklist and the developer handover disagreeing about what is done.

    Returns `(None, None)` for a domain that has never been checked. Callers show
    the "not checked yet" state; none of them probes to find out, which is what
    keeps a GET free and stops a typo'd domain in the URL bar firing thirty
    requests at somebody's server.
    """
    view = await _from_snapshot(session, domain)
    if view is None:
        return None, None
    return await _derive_state(
        session, domain, view.doc, view.tech, view.readiness, view.bundle
    ), view


async def _derive_state(session: AsyncSession, domain: str, doc, tech, readiness, bundle):
    """The four states, from pieces a caller already has."""
    site_url = f"https://{domain}"
    artifacts = {a.name: a.body for a in bundle.artifacts}
    templates_for_site = build_templates(site_url, doc.site_name or domain, tech.platform.value)
    marks = await repo.load_marks(session, domain)

    status = derive(
        site_url,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
        readiness=readiness,
        artifacts=artifacts,
        templates=templates_for_site,
        marks=marks,
    )
    return status


def _component_context(request, user, domain, status, view) -> dict:
    """The context every component-bearing page shares.

    `view` rather than `tech` so `checked_ago` travels with the data. Every one of
    these pages shows figures from a stored probe, and each has to say how old it
    is -- passing the timestamp alongside is what stops a template omitting it.
    """
    return {
        "user": user,
        "domain": domain,
        "site_url": status.site_url,
        "site_type": status.site_type.value,
        "platform": view.tech.platform.value,
        "markable": {s.key for s in status.statuses if manually_markable(s.component)},
        "checked_ago": view.checked_ago,
        "is_stale": view.is_stale,
        "label": "",
        # Outstanding work per family, for the sidebar. Derived from the same
        # `SiteStatus` the page body renders, so a count in the nav cannot
        # disagree with the tab it points at.
        "nav_gaps": {
            family: counts["total"] - counts["live"]
            for family, _label, counts in status.family_counts()
        },
        # Spec compliance of what we generated, keyed by component. Cheap: the
        # rules do no I/O. Absent for an artifact that has no rule set, which the
        # partial renders as "no checks exist" rather than as a pass.
        "reports": reports_for(view),
        "judged": JUDGED_BY,
    }


def _unchecked(request, user, domain: str, title: str):
    """The page for a client nobody has probed yet.

    Deliberately a page rather than a probe. Rendering this costs the client's
    server nothing, and it means a mistyped domain in the URL bar is a dead end
    instead of thirty requests at whoever owns it.
    """
    return templates.TemplateResponse(
        request,
        "unchecked.html",
        {"user": user, "domain": domain, "title": title},
        status_code=status.HTTP_404_NOT_FOUND if not domain else status.HTTP_200_OK,
    )


@app.get("/sites/{domain}/family/{family}", response_class=HTMLResponse)
async def family_tab(
    request: Request,
    domain: str,
    family: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        wanted = Family(family)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such group.") from exc

    site_status, view = await _site_status(session, domain)
    if site_status is None:
        return _unchecked(request, user, domain, FAMILY_LABELS[wanted])

    # Not-applicable components are dropped from the tab rather than listed as
    # absent. A page of "does not apply" reads as a broken tool on exactly the
    # sites where the tool is most useful.
    statuses = [
        s for s in site_status.family(wanted) if s.state is not ComponentState.NOT_APPLICABLE
    ]

    return templates.TemplateResponse(
        request,
        "family.html",
        {
            **_component_context(request, user, domain, site_status, view),
            "family_label": FAMILY_LABELS[wanted],
            "family_blurb": FAMILY_BLURBS[wanted],
            "statuses": statuses,
        },
    )


@app.get("/sites/{domain}/checklist", response_class=HTMLResponse)
async def client_checklist(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    site_status, view = await _site_status(session, domain)
    if site_status is None:
        return _unchecked(request, user, domain, "Your checklist")

    statuses = site_status.for_client()

    return templates.TemplateResponse(
        request,
        "checklist.html",
        {
            **_component_context(request, user, domain, site_status, view),
            "statuses": statuses,
            "done": sum(1 for s in statuses if s.state is ComponentState.LIVE),
            "total": len(statuses),
            "dev_count": len(site_status.for_developer()),
        },
    )


@app.get("/sites/{domain}/handover", response_class=HTMLResponse)
async def developer_handover(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    site_status, view = await _site_status(session, domain)
    if site_status is None:
        return _unchecked(request, user, domain, "Developer handover")

    return templates.TemplateResponse(
        request,
        "handover.html",
        {
            **_component_context(request, user, domain, site_status, view),
            "grouped": site_status.by_effort(),
            "total": len(site_status.for_developer()),
        },
    )


@app.post("/sites/{domain}/marks")
async def set_component_mark(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Record that a person checked something no probe can check.

    Refused for anything the tool can verify itself. Letting someone tick
    `llms.txt` while it returns 404 would put a false claim into a client-facing
    status, which costs more than the sense of progress the marking exists to give.
    """
    form = await request.form()
    key = str(form.get("component_key") or "")
    component = by_key(key)
    if component is None or not manually_markable(component):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That component is decided by the probe, not by hand.",
        )

    if str(form.get("action") or "set") == "clear":
        await repo.clear_mark(session, domain, key)
    else:
        await repo.set_mark(
            session, domain, key, noted_by=user.email, note=str(form.get("note") or "")
        )
    await session.commit()

    back = request.headers.get("referer") or f"/sites/{domain}/checklist"
    return RedirectResponse(back, status_code=status.HTTP_303_SEE_OTHER)


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


@app.post("/runs/{run_id}/rerun")
async def rerun(
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Start a fresh run against the same site, from the beginning.

    A new run rather than a reset of this one. Re-running is nearly always a
    comparison -- did the fix change the output -- and resetting in place
    destroys the artefact being compared against. The old run stays readable
    while its replacement works, which is exactly when its events are wanted.

    It goes through preflight and stops at the review gate like any other run:
    re-running is not a licence to skip the human. The domain's brief is reused,
    so nobody is asked the onboarding questions twice.
    """
    fresh = await repo.clone_run(session, run_id, created_by=user.email)
    if fresh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    new_id = str(fresh.id)
    await repo.record_event(
        session, fresh.id, "preflight", f"Re-run of {run_id} started by {user.email}"
    )
    await session.commit()

    from app.jobs.tasks import preflight_task

    await preflight_task.defer_async(run_id=new_id, requested_max_pages=fresh.max_pages or 0)
    return RedirectResponse(f"/runs/{new_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Ask a running job to stop at its next stage boundary.

    Cooperative, and honest about it: the stage in flight finishes. A crawl of
    400 pages or a batch of LLM calls runs to completion, because interrupting
    one leaves half-written state that is worse than the work saved. What
    stopping guarantees is that no further expensive stage begins.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    if RunStatus(run.status).is_terminal:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That run has already finished.")

    await repo.set_status(session, run, RunStatus.CANCELLED)
    await repo.record_event(session, run_id, "cancelled", f"Cancelled by {user.email}")
    await session.commit()
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/runs/{run_id}/delete")
async def delete_run(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Delete a run and everything it stored. Irreversible.

    Three guards, each for a different failure:

    * **Admin only.** There is no undo and no soft delete, and a run holds
      crawled client page bodies.
    * **Terminal runs only.** A worker mid-stage still holds this run's id and
      would write rows back after the delete, or fail in a way that reads as a
      bug. Cancel first; the button says so.
    * **Typed confirmation.** The domain has to be typed. A second click is not
      a decision -- it is the same click twice -- and this is the one action in
      the tool that cannot be walked back.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    if not RunStatus(run.status).is_terminal:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That run is still working. Cancel it first, then delete it.",
        )

    form = await request.form()
    if str(form.get("confirm") or "").strip().lower() != run.domain.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Type {run.domain} to confirm. Nothing was deleted.",
        )

    domain = run.domain
    await repo.delete_run(session, run_id)
    await session.commit()
    logger.info("run %s (%s) deleted by %s", run_id, domain, user.email)
    return RedirectResponse("/?deleted=" + quote(domain), status_code=status.HTTP_303_SEE_OTHER)


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

    # The site type decides which section template the file is built from, so a
    # human override here has to reach both the plan and the run -- `generate_task`
    # reads it back off the plan, and the renderer reads it off the run.
    chosen = str(form.get("site_pattern") or "").strip()
    if chosen in PATTERN_TEMPLATES:
        plan.site_pattern = chosen
        run.pattern = chosen

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

    # Changing the site type re-orders the whole file, and re-crawling to do that
    # would be absurd -- the pages are already in Postgres. Apply it before the
    # result is rebuilt so `order_sections` sees the new template.
    chosen = str(form.get("site_pattern") or "").strip()
    if chosen in PATTERN_TEMPLATES:
        run.pattern = chosen

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


@app.post("/runs/{run_id}/chat", response_class=HTMLResponse)
async def chat_edit(
    request: Request,
    run_id: UUID,
    message: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Edit the finished file by conversation.

    The model returns operations, not text. They are applied to the same rows the
    edit form writes to, the file is re-rendered from those rows, and the result is
    validated -- and if the edit introduced a spec *error* the whole turn is rolled
    back. That ordering is the point: a chat that can produce an invalid client
    deliverable is worse than no chat.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    if not run.llmstxt:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing generated yet.")

    pages = await repo.get_pages(session, run_id)
    before = _result_from_rows(run, pages)
    excluded_urls = [page.url for page in pages if not page.included]

    turn = await apply_chat_turn(
        LLMClient(settings),
        request=message,
        site_name=run.site_name,
        site_summary=run.site_summary,
        sections=before.sections,
        optional=before.optional,
        excluded=excluded_urls,
    )

    session.add(ChatMessage(run_id=run_id, role="user", body=message.strip(), author=user.email))

    if turn.rejected or not turn.operations:
        reply = turn.rejected or turn.reply or "No change was needed."
        session.add(ChatMessage(run_id=run_id, role="assistant", body=reply, author="model"))
        await session.commit()
        return await _chat_panel(request, session, run_id, user)

    # Snapshot before touching anything, so undo restores the model and not just
    # the rendered text.
    session.add(
        DocumentRevision(
            run_id=run_id,
            llmstxt=run.llmstxt,
            llms_full=run.llms_full,
            site_name=run.site_name,
            site_summary=run.site_summary,
            pages={
                page.url: {
                    "title": page.title,
                    "description": page.description,
                    "section": page.section_name,
                    "is_optional": page.is_optional,
                    "included": page.included,
                }
                for page in pages
            },
            reason=message.strip()[:255],
            author=user.email,
        )
    )

    target = EditTarget(
        site_name=run.site_name,
        site_summary=run.site_summary,
        notes=run.notes,
        pages={
            page.url: {
                "title": page.title,
                "description": page.description,
                "section": page.section_name,
                "is_optional": page.is_optional,
                "included": page.included,
            }
            for page in pages
        },
    )
    report = apply_operations(target, turn.operations)

    for page in pages:
        edited = target.pages[page.url]
        page.title = edited["title"]
        page.description = edited["description"]
        page.section_name = edited["section"]
        page.is_optional = bool(edited["is_optional"])
        page.included = bool(edited["included"])
    run.site_name = target.site_name
    run.site_summary = target.site_summary
    run.notes = target.notes

    rebuilt = rebuild(
        _result_from_rows(run, pages),
        excluded_urls={page.url for page in pages if not page.included},
        site_name=run.site_name,
        site_summary=run.site_summary,
        section_order=target.section_order or None,
    )

    # The gate. A new error-level issue means the edit broke the spec, so nothing
    # is kept -- the user is told what it would have done instead.
    was = {issue.get("code") for issue in (run.issues or []) if issue.get("level") == "error"}
    now = {issue.code for issue in rebuilt.issues if issue.level == "error"}
    if now - was:
        await session.rollback()
        broke = ", ".join(sorted(now - was))
        async with session_scope() as fresh:
            fresh.add(
                ChatMessage(run_id=run_id, role="user", body=message.strip(), author=user.email)
            )
            fresh.add(
                ChatMessage(
                    run_id=run_id,
                    role="assistant",
                    body=(
                        f"That edit would have made the file invalid ({broke}), so I have not "
                        "applied it. Nothing changed."
                    ),
                    author="model",
                )
            )
        return await _chat_panel(request, session, run_id, user)

    await repo.store_result(session, run, rebuilt)

    reply = turn.reply or "Done."
    if report.rejected:
        reply += " Refused: " + "; ".join(report.rejected)
    session.add(
        ChatMessage(
            run_id=run_id,
            role="assistant",
            body=reply,
            operations=report.applied,
            rejected=report.rejected,
            author="model",
        )
    )
    await repo.record_event(
        session, run_id, "chat", f"{user.email}: {len(report.applied)} edit(s) applied"
    )
    await session.commit()
    return await _chat_panel(request, session, run_id, user)


@app.get("/runs/{run_id}/chat", response_class=HTMLResponse)
async def chat_panel(
    request: Request,
    run_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    return await _chat_panel(request, session, run_id, user)


async def _chat_panel(request: Request, session: AsyncSession, run_id: UUID, user: User):
    run = await repo.get_run(session, run_id)
    messages = await repo.recent_chat(session, run_id)
    return templates.TemplateResponse(
        request,
        "partials/chat.html",
        {"user": user, "run": run, "messages": messages},
    )


# -- admin ------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    days: int = 30,
    user: User = Depends(require_admin_or_404),
    session: AsyncSession = Depends(get_session),
):
    """Spend and activity. Admin only, and invisible to everyone else.

    Cost used to sit on the run page, where anyone looking at a client's file also
    saw what the agency paid to produce it. geo-tracker moved the same number off
    its client-facing overview for the same reason; this follows it.
    """
    window = max(1, min(days, 365))
    runs = await repo.runs_since(session, days=window)

    costs = [cost_of(run.stats) for run in runs]
    totals = totals_of(costs)
    rows = sorted(zip(runs, costs, strict=True), key=lambda pair: pair[1].total_usd, reverse=True)

    by_day: dict[str, dict[str, float]] = {}
    for run, cost in zip(runs, costs, strict=True):
        key = run.created_at.date().isoformat()
        day = by_day.setdefault(key, {"runs": 0, "usd": 0.0})
        day["runs"] += 1
        day["usd"] += cost.total_usd

    by_model: dict[str, dict[str, float]] = {}
    for run in runs:
        for model, counts in ((run.stats or {}).get("llm") or {}).get("by_model", {}).items():
            entry = by_model.setdefault(
                model, {"calls": 0, "prompt": 0, "completion": 0, "usd": 0.0}
            )
            entry["calls"] += int(counts.get("calls") or 0)
            entry["prompt"] += int(counts.get("prompt") or 0)
            entry["completion"] += int(counts.get("completion") or 0)
            if (rates := rate_for(model)) is not None:
                entry["usd"] += (int(counts.get("prompt") or 0) / 1_000_000) * rates[0]
                entry["usd"] += (int(counts.get("completion") or 0) / 1_000_000) * rates[1]
            else:
                entry["usd"] = -1.0  # sentinel: unpriced, rendered as em dash

    return templates.TemplateResponse(
        request,
        "admin/costs.html",
        {
            "user": user,
            "days": window,
            "rows": rows[:60],
            "totals": totals,
            "by_day": sorted(by_day.items()),
            "by_model": sorted(by_model.items(), key=lambda kv: -kv[1]["usd"]),
            "serp_rate": SERP_CALL_USD,
        },
    )


@app.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs(
    request: Request,
    user: User = Depends(require_admin_or_404),
    session: AsyncSession = Depends(get_session),
):
    """Every run, with what it cost and what it produced."""
    runs = await repo.list_runs(session, limit=200)
    rows = [(run, cost_of(run.stats)) for run in runs]
    return templates.TemplateResponse(request, "admin/runs.html", {"user": user, "rows": rows})


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
