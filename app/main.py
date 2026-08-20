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
from datetime import date
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
from app.core.edits import EditTarget, apply_operations
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
from app.scrape.readiness import SiteType, audit_readiness
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
    readiness = await audit_readiness(
        site_url,
        settings.crawl_user_agent,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
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


async def _agents_document(session: AsyncSession, normalised: str):
    """Probe a site and build its document. Shared by the page and the download.

    Both paths must produce the same file. Duplicating the assembly is how they
    would come to differ, and an operator downloading something other than what
    they reviewed is the kind of divergence nobody notices until a client does.
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
    # The readiness audit runs after, not alongside. It is nine more requests to
    # the same host, and firing them concurrently with the other two probes is how
    # a small site starts refusing us and we report our own impatience as its
    # shortcomings.
    readiness = await audit_readiness(
        normalised,
        settings.crawl_user_agent,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
    )

    domain = urlparse(normalised).netloc
    config = await repo.load_site_config(session, domain)
    brief = await repo.load_brief(session, domain)

    # Links come from a completed llms.txt run for the same domain. The two files
    # describe one site, so pages that crawl already fetched are pages this one
    # can cite -- the evidence rule met by a different means rather than waived.
    # Endpoints the tech probe confirmed are citable on the same terms as anything
    # else here: each answered with the right content type, so each is a capability
    # rather than a convention someone expects to exist.
    run = None
    read_only: list = [
        Capability(label=d.name, url=d.url, evidence=f"answered {d.evidence}")
        for d in tech.endpoints
    ]
    policies: list = []
    contact = ""
    run = await repo.latest_complete_run(session, domain)
    if run is not None:
        pages = await repo.get_pages(session, run.id)
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

    # Declared endpoints are verified before anything references them. An
    # operator naming their own MCP server is the only way we learn of it, and
    # also the easiest place for a typo or a decommissioned host to reach a
    # published file.
    declared = await verify_declared(brief, settings.crawl_user_agent)
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
    return probe, doc, tech, catalog, readiness, bundle


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "agents.html",
        {
            "user": user,
            "site_url": "",
            "probe": None,
            "doc": None,
            "tech": None,
            "catalog": None,
            "readiness": None,
        },
    )


@app.post("/agents", response_class=HTMLResponse)
async def agents_generate(
    request: Request,
    site_url: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Probe first, then write only what the probe confirmed.

    Synchronous rather than queued: this is four HTTP requests, not a crawl, and
    putting it behind the job queue would add a status page to something that
    finishes before the operator lets go of the mouse.
    """
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    probe, doc, tech, catalog, readiness, bundle = await _agents_document(session, normalised)

    return templates.TemplateResponse(
        request,
        "agents.html",
        {
            "user": user,
            "site_url": normalised,
            "probe": probe,
            "doc": doc,
            "tech": tech,
            "catalog": catalog,
            "readiness": readiness,
            "bundle": bundle,
            "rendered": render_agents_md(doc),
        },
    )


@app.get("/agents/download")
async def agents_download(
    site: str,
    kind: str = "md",
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Re-probe and re-render rather than serving something remembered.

    A cached document could name an endpoint the site has since withdrawn, and
    handing a client a file that instructs agents to call a dead endpoint is the
    failure this whole feature is built to avoid. Four requests is a cheap price
    for the file being true when it is downloaded.
    """
    normalised = normalise_site_url(site)
    probe, doc, _tech, catalog, _readiness, bundle = await _agents_document(session, normalised)

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
