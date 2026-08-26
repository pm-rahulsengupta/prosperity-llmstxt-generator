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
import hmac
import logging
import mimetypes
import re
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi import Path as PathParam
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
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
from app.core import share
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
from app.core.client_report import SECTION_KEYS, build_client_report, section_title
from app.core.components import (
    FAMILY_BLURBS,
    FAMILY_LABELS,
    ComponentState,
    Family,
    SiteType,
    by_key,
)
from app.core.csv_source import parse_screaming_frog_csv
from app.core.edits import EditTarget, apply_operations
from app.core.evidence import JUDGED_BY, reports_for
from app.core.metrics import DateRange
from app.core.onboarding import (
    QUESTIONS,
    SiteBrief,
    brief_from_answers,
    split_embargoed,
)
from app.core.pipeline import rebuild
from app.core.presentation import look_for, run_look, surface_look
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
from app.core.throttle import TokenBucket
from app.db import repo
from app.db.base import get_session, session_scope
from app.db.models import ChatMessage, DocumentRevision, RunStatus
from app.jobs.tasks import SOURCE_IMPORT
from app.llm.client import LLMClient, LLMUsage, Stage
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

# PageSpeed is 10-30s per URL and rate limited per key. The readiness sampler
# hands over three or four pages; this is the ceiling on how many of them get a
# Lighthouse run, homepage first.
PAGESPEED_SAMPLES = 3

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
    if settings.pdf_enabled:
        # Asked once, at boot, so a missing browser is a log line rather than a
        # 500 on a client's first download. The base image ships browsers for
        # Playwright 1.60 and the pin is 1.62; `scrapling install` should have
        # reconciled that at build time, and this is what checks.
        from app.pdf import probe_chromium

        await probe_chromium()
    else:
        import app.pdf as pdf_module

        pdf_module.PDF_READY = False
        logger.info("PDF_ENABLED is off: the client page still prints.")

    from app.jobs.queue import app as queue_app

    async with queue_app.open_async():
        yield


#: Everything a share response says about itself. Attached by `ShareScope` to
#: every `/share/*` response including the 404s, so a template or a route cannot
#: forget one.
SHARE_HEADERS: dict[str, str] = {
    # A header, not only the `<meta>` in the template: a meta tag cannot ride on
    # the artifact downloads, which are text/markdown, and a header cannot be
    # forgotten by a template.
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
    # The most important header here. **The URL is the credential**, and this page
    # links outward -- to the client's own site, to their contact URLs. Under any
    # weaker policy, including strict-origin-when-cross-origin, following one of
    # those links hands the whole share URL to a third party.
    "Referrer-Policy": "no-referrer",
    # Railway's edge, corporate proxies and mail-security caches must not retain a
    # client's audit under a URL that can later be revoked.
    "Cache-Control": "no-store, private, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # `default-src 'none'` with no `script-src` at all: the client templates carry
    # no <script>, so scripts are refused outright rather than allowlisted.
    # `form-action 'none'` is the structural backstop for the template split -- if
    # a staff form ever reached a client page it would be an inert button rather
    # than a leak.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


class ShareScope:
    """Makes `/share/*` a different surface from the rest of the app.

    **Strips the request's Cookie header** so `SessionMiddleware` builds an empty
    session and a staff member testing a link sees exactly what their client sees,
    rather than a page quietly rendered with their own identity in scope.

    The obvious implementation -- `request.session.clear()` in the handler -- is a
    bug, and it is the first thing anyone will write. Starlette's
    `SessionMiddleware` emits a *delete-cookie* `Set-Cookie` when a session was
    non-empty on the way in and empty on the way out, so clicking your own share
    link would sign you out of the app. Stripping the header upstream means the
    middleware never sees a session to clear, and sends no `Set-Cookie` at all.

    **Registration order is load-bearing.** Starlette prepends each
    `add_middleware`, so the last registered runs outermost. This must therefore
    be added *after* `SessionMiddleware` to sit outside it. Added before, it would
    run inside, the cookie would already have been parsed, and the whole class
    would be decoration. `tests/test_share_isolation.py` pins the behaviour rather
    than the ordering, so it fails if the two are ever swapped.
    """

    PREFIX = "/share/"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.PREFIX):
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"cookie"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                existing = {k.lower() for k, _v in message["headers"]}
                message["headers"] = list(message["headers"]) + [
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in SHARE_HEADERS.items()
                    if k.lower().encode("latin-1") not in existing
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
# After SessionMiddleware, so it runs *outside* it and the cookie is gone before
# the session is ever parsed. Starlette prepends, so last registered is outermost.
app.add_middleware(ShareScope)
# Python's mimetypes table has no woff2 on a stock Windows install, so StaticFiles
# serves it as application/octet-stream -- which makes the browser discard the
# `<link rel=preload as=font>` hint and fetch the file a second time.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(StarletteHTTPException)
async def _auth_redirect(request: Request, exc: StarletteHTTPException) -> Response:
    """Turn `require_user`'s 401 into a redirect the browser will actually follow.

    `require_user` raises 401 with a `Location` header (app/auth.py:130-134), but
    browsers only follow `Location` on a 3xx -- on a 401 they render the body. With
    no handler registered, an expired session mid-request painted
    `{"detail":"Sign in to continue."}` onto the page, and for an HTMX poll it
    swapped that JSON straight into the DOM.

    HTMX will not follow a 303 either: it issues the redirect as an XHR and swaps
    the login page into the target element. The documented escape is the
    `HX-Redirect` response header, which htmx turns into a full client-side
    navigation -- and it must ride on a 2xx, because htmx does not swap or process
    error responses by default.
    """
    location = (exc.headers or {}).get("Location")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and location:
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": location})
        return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    return await http_exception_handler(request, exc)


templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["usd"] = usd
# Called from base.html on every render. A global rather than a context key each
# route must remember: sixteen routes render templates, and the one that forgot
# would 500 on a page that has nothing to do with navigation.
templates.env.globals["build_nav"] = build_nav
# How a state reads and what colour it wears, decided in one place. Registered
# as globals for the same reason `build_nav` is: `component.html` renders on
# eight pages, and a context key one route forgets is a 500 on that page only.
templates.env.globals["component_look"] = look_for
templates.env.globals["surface_look"] = surface_look
# A run's status was spelled out inline in three templates, each deriving its
# own pill colour, and one of them showed the enum's own wording.
templates.env.globals["run_look"] = run_look


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
async def auth_callback(request: Request, session: AsyncSession = Depends(get_session)):
    """Complete a Google sign-in against the account row, not just the token.

    `user_from_claims` validates the domain from the signed token and returns a
    `User` whose `is_admin` defaults to False. Signing that in directly -- which
    is what this route used to do -- meant every Google sign-in was a non-admin
    regardless of the account row, and `is_active` was never checked.
    """
    if not settings.sso_enabled:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    token = await oauth.google.authorize_access_token(request)
    claims = user_from_claims(token.get("userinfo") or {}, settings)

    try:
        row = await accounts.resolve_sso(session, claims.email, claims.name)
    except accounts.SignupClosed as exc:
        await session.rollback()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": str(exc)},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    sign_in(
        request,
        User(
            email=row.email,
            name=row.name or claims.name,
            picture=claims.picture,
            is_admin=row.is_admin,
        ),
    )
    await session.commit()
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
    """Claim a fresh instance with a password -- only where Google is not an option.

    With SSO configured this redirects into Google, and that is a security fix
    rather than a preference: `claim_instance` performs **no domain check**, so
    on a public deployment whoever finds the URL first can claim it with any
    address at all. Google validates the domain from the signed ID token before
    `resolve_sso` provisions anything, so the same first-one-wins bootstrap
    becomes restricted to the company's own accounts.
    """
    if await accounts.is_bootstrapped(session):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if settings.sso_enabled:
        return RedirectResponse("/login/google", status_code=status.HTTP_303_SEE_OTHER)
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
    # Resolved rather than read off the cookie, like every other page. This read
    # `current_user(request)`, whose `is_admin` is whatever was true at sign-in --
    # so `/` could draw an Admin nav group for somebody who no longer has one,
    # and every link in it would then 404. The 401 becomes a redirect in
    # `_auth_redirect`, which is what the hand-rolled check below was doing.
    try:
        user = await require_user(request, session)
    except HTTPException:
        # /login sends a brand-new instance on to /signup.
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    runs = await repo.list_runs(session, limit=40)
    return templates.TemplateResponse(request, "index.html", {"user": user, "runs": runs})


@app.post("/runs")
async def create_run(
    request: Request,
    site_url: str = Form(...),
    max_pages: int = Form(0),
    generate_full: bool = Form(False),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run = await repo.create_run(session, normalised, created_by=user.email)
    run.max_pages = max_pages
    # Stored on the plan rather than in a column: it is a decision about this
    # run, it travels with the plan the operator approves, and it needs no
    # migration. `generate_task` reads it back the same way.
    run.generate_full = bool(generate_full)
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
    # Recorded at the end of this handler. The wizard runs before any run exists,
    # which is why `LlmSpend.run_id` is nullable.
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

    # The wizard's spend, which previously reached nothing. Committed here rather
    # than at the end so a template error cannot lose the record of money already
    # spent with the vendor.
    await repo.record_spend(session, usage, domain=domain, spent_by=user.email)
    await session.commit()

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

    # Hosted Lighthouse over the same pages the readiness audit sampled, so the
    # two agree about which pages were judged. Ten to thirty seconds per URL,
    # which is why this only ever runs here -- in the probe someone asked for --
    # and never on a page render.
    findings: list = []
    crux = None
    if settings.pagespeed_enabled:
        from app.scrape.pagespeed import measure_many

        judged = [normalised + "/", *page_sample][:PAGESPEED_SAMPLES]
        findings = await measure_many(judged, settings.pagespeed_api_key)
        for failed in [f for f in findings if not f.usable]:
            logger.info("pagespeed could not measure %s: %s", failed.url, failed.error)

        # Real-user data, which outranks the lab run above wherever it exists.
        # The same key serves both APIs. Three cheap queries, and for a
        # client-sized site it is usually the origin-wide one that answers.
        from app.scrape.crux import fetch_crux

        crux = await fetch_crux(normalised, settings.pagespeed_api_key, page_url=normalised + "/")

    readiness = await audit_readiness(
        normalised,
        settings.crawl_user_agent,
        SiteType.ECOMMERCE if tech.sells else SiteType.CONTENT,
        sample_urls=page_sample,
        lighthouse=findings,
        crux=crux,
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
    # The onboarding answers. CRW-009 compares the published robots.txt against
    # the AI bot policy stated here; without it the rule skips rather than
    # guessing which of the file and the intent is wrong.
    brief: object = None

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


async def _refined(session: AsyncSession, domain: str, doc):
    """Apply the stored refinement for this domain, if there is one.

    Returns the document unchanged and no facts where nothing was refined, so
    every caller can use this without branching.
    """
    from app.core.refine import AssertedFact, RefineOp, apply_refinements

    stored = await repo.load_edit(session, domain, "agents-md")
    if stored is None:
        return doc, []

    operations = [RefineOp(**o) for o in (stored.operations or [])]
    facts = [AssertedFact(**f) for f in (stored.facts or [])]
    refined, facts, _report = apply_refinements(doc, facts, operations, author=stored.edited_by)
    return refined, facts


def _replace_artifact(bundle, name: str, body: str) -> None:
    """Swap one artifact's body, keeping its path and media type.

    `Artifact` is frozen, so this replaces rather than mutates -- which is the
    behaviour wanted anyway: a half-updated bundle would be worse than either
    state.
    """
    from dataclasses import replace as _replace

    for index, artifact in enumerate(bundle.artifacts):
        if artifact.name == name:
            bundle.artifacts[index] = _replace(artifact, body=body)
            return


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

    # Stored refinements, replayed onto the freshly generated document. Operations
    # rather than a stored body is what makes this safe: the file is still a
    # function of the evidence, with an operator's edits on top, so a re-probe
    # that changes the evidence changes the file and the edits still apply.
    doc, facts = await _refined(session, domain, doc)
    if facts or await repo.load_edit(session, domain, "agents-md"):
        _replace_artifact(bundle, "agents.md", render_agents_md(doc, facts=facts))

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
        brief=brief,
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
    # One query for every domain rather than one per row. A crashed worker leaves
    # a run in flight forever, and the client list is where that is noticed.
    running = await repo.unfinished_runs(session)

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
        run = running.get(config.domain)
        rows.append(
            {
                "domain": config.domain,
                "label": config.label,
                "onboarded": bool(config.brief),
                "snapshot": summary,
                "run": (
                    {
                        "id": str(run.id),
                        "status": run.status,
                        "started_ago": _ago(run.created_at),
                    }
                    if run is not None
                    else None
                ),
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
    # False, not True. An unchecked box sends *nothing*, so a `Form(True)` default
    # would make unticking it do nothing at all. The default an operator sees is
    # the `checked` attribute on the input; the default for a request that omits
    # the field entirely is "do not touch their server", which is the safe one.
    check_now: bool = Form(False),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Add a client, check their site, and start onboarding.

    The check is on by default. Without it a brand-new client had no snapshot, so
    the overview, the checklist and the handover all rendered the "nobody has
    checked this yet" page, and the profile showed "Never checked" beside every
    number -- a client added and then left looked identical to one whose site
    could not be reached. Probing costs the client's server about thirty requests
    and buys every one of those pages something to say.

    Synchronous for the same reason `refresh_client` is: the worker runs at
    concurrency 1, so a queued check can sit behind an in-flight crawl for
    minutes, and this is a form somebody is watching.

    **A failed check must not lose the client.** The config is committed first
    and the probe runs in its own block, so an unreachable site, a WAF or a
    timeout leaves the client on file with no snapshot -- exactly the state the
    "Check the site now" button on their profile exists to fix.
    """
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

    if check_now:
        try:
            await _check_and_store(session, normalised, domain, user.email)
            await session.commit()
        except Exception:
            # Logged, not raised. The client is already saved and the brief is
            # still the right next screen; their profile will say the site has
            # never been checked, which is true.
            logger.exception("first check of %s failed; the client was still added", domain)
            await session.rollback()

    # Then straight into onboarding, which is where the brief is answered.
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
    """Everything true about one client, in one place.

    This was four fields and a delete form. It is the profile now -- the page an
    operator opens to answer "what state is this client in and what can I do
    about it" -- so it carries each area's *current state* beside its control.
    A page of controls with no state beside them is how you end up re-running a
    crawl that is already running.
    """
    config = await repo.load_site_config(session, domain)
    snapshot = await repo.load_snapshot(session, domain)
    runs = await repo.runs_for_domain(session, domain, limit=5)
    brief = await repo.load_brief(session, domain)
    now = datetime.now(UTC)

    # From the whole table, not from the five rows shown below it. Measured on
    # the live instance: prosperitymedia.com.au had a run Queued for five days
    # sitting *sixth*, so this found nothing while the client list -- which
    # queries every in-flight run -- reported one, and the delete guard refused
    # a delete the profile offered no way to unblock.
    unfinished = (await repo.unfinished_runs(session)).get(domain)
    return {
        "user": user,
        "domain": domain,
        "label": config.label if config else "",
        "exists": config is not None,
        "added_ago": _ago(config.created_at) if config is not None else "",
        "going": await repo.preview_client_deletion(session, domain),
        "error": error,
        # -- onboarding -----------------------------------------------------
        "onboarded": bool(config.brief) if config is not None else False,
        "answered_by": brief.answered_by,
        "embargoed_count": len(brief.embargoed),
        # -- the last check -------------------------------------------------
        "readiness": readiness_from_dict(snapshot.readiness).score if snapshot else None,
        "checked_ago": _ago(snapshot.fetched_at) if snapshot else "",
        "is_stale": bool(snapshot and now - snapshot.fetched_at > timedelta(days=1)),
        # -- crawls ---------------------------------------------------------
        "runs": [
            {
                "id": str(run.id),
                "status": run.status,
                "started_ago": _ago(run.created_at),
                "pages": run.max_pages,
                "by": run.created_by,
            }
            for run in runs
        ],
        "unfinished_run_id": str(unfinished.id) if unfinished is not None else "",
        "share_enabled": get_settings().share_links_enabled,
        # Never anything derived from a token: the row's id is what the revoke
        # form posts, and the label is the operator's own note.
        "links": await repo.list_share_links(session, domain),
        "now": now,
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


async def _delete_context(session: AsyncSession, user: User, domain: str, error: str | None):
    """What a delete would take, and whether it is safe to take it yet.

    `unfinished_run_id` is the guard. `run.html` already refuses to delete a
    *run* that is still working -- "a worker mid-stage still holds its id and
    would write rows back after the delete" -- but deleting the *client* removed
    every one of its runs with no such check, which is the same hazard with a
    larger blast radius. Found with two clients sat unfinished on the live
    instance, one of them the client this was about to be used on.
    """
    return {
        "user": user,
        "domain": domain,
        "going": await repo.preview_client_deletion(session, domain),
        # All of them, not the most recent. This client held four, and stopping
        # one only revealed the next -- four rounds of "a crawl is still
        # working" with no sign of how many were left.
        "unfinished": [
            {"id": str(run.id), "status": run.status, "started_ago": _ago(run.created_at)}
            for run in await repo.unfinished_runs_for_domain(session, domain)
        ],
        "error": error,
    }


@app.get("/sites/{domain}/delete", response_class=HTMLResponse)
async def confirm_delete_client(
    request: Request,
    domain: str,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """What deleting this client would destroy, before anything is destroyed.

    A page rather than a dialog: the counts come from the same function the
    delete itself calls, so what an operator confirms and what actually goes
    cannot disagree, and a browser back-button lands somewhere harmless.
    """
    return templates.TemplateResponse(
        request, "client_delete.html", await _delete_context(session, user, domain, None)
    )


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

    def refuse(reason: str):
        return _delete_context(session, user, domain, reason)

    # Checked on the POST as well as on the page. A confirmation page an operator
    # left open for ten minutes says nothing about what is running now.
    if await repo.unfinished_runs_for_domain(session, domain):
        return templates.TemplateResponse(
            request,
            "client_delete.html",
            await refuse(
                "A crawl for this client is still working. Stop it first -- a worker "
                "mid-stage holds its run id and would write rows back after the delete."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    form = await request.form()
    if str(form.get("confirm") or "").strip().lower() != domain.lower():
        return templates.TemplateResponse(
            request,
            "client_delete.html",
            await refuse(f"Type {domain} to confirm. Nothing was deleted."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    removed = await repo.delete_client(session, domain)
    await session.commit()
    return RedirectResponse(
        "/clients?deleted=" + quote(f"{domain}: {removed.summary()}"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/imports/screaming-frog", response_class=HTMLResponse)
async def import_form(request: Request, user: User = Depends(require_user)):
    """Upload a crawl instead of performing one.

    The fallback for a site we cannot read: a WAF, a bot-protection rule, or a
    staging environment behind auth. Screaming Frog runs from the operator's own
    machine with the client's blessing, so an Internal All export is a crawl that
    already happened and already succeeded.
    """
    return templates.TemplateResponse(
        request, "import.html", {"user": user, "error": None, "report": None}
    )


# -- audits pushed from the LLM Access Checker --------------------------------


def _audit_domain(payload: dict) -> str:
    """The domain an audit is about, normalised the way this tool stores them.

    The Checker sends `parsed.netloc`, which carries the `www.` a site was
    audited under. `repo.domain_of` is what every other table here is keyed by,
    so routing the payload through it is what makes an audit land on the client
    an operator already has rather than creating a second one beside it.
    """
    raw = str(payload.get("domain") or "").strip()
    return repo.domain_of(raw) if raw else ""


def _audit_taken_at(payload: dict) -> datetime:
    """When the Checker ran it, falling back to now.

    Not to the epoch: a missing timestamp would sort the audit to the bottom for
    ever and `latest_audit` would keep returning an older one, which is a worse
    lie than being a few seconds out.
    """
    stamp = str(payload.get("generated_at") or "").strip()
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


@app.post("/api/audits")
async def receive_audit(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Accept one audit from the LLM Access Checker.

    Machine-to-machine, so it carries its own bearer token rather than a session
    cookie -- `require_user` would be wrong here and would also mean the Checker
    needed a Google account.

    Three refusals, in order of how quietly they would otherwise fail:

    * **Intake closed.** No token configured means the door is shut, not open.
      An unset secret degrading into "accept anything" is the one way this fails
      dangerously rather than merely uselessly.
    * **Wrong token.** `compare_digest`, not `==`, so a wrong guess costs the
      same time whatever it got right.
    * **No domain.** An audit we cannot attribute to a client is not storable;
      taking it and dropping it would be worse than refusing it.

    The payload is stored verbatim. It is built as a dict literal inside the
    Checker's Streamlit UI rather than as a versioned contract, so anything read
    out of it here is a convenience copy and a shape change has to degrade the
    join rather than lose the audit.
    """
    settings = get_settings()
    if not settings.audit_intake_open:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Audit intake is not configured on this instance.",
        )

    presented = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, settings.audit_webhook_token):
        logger.warning("rejected an audit push with a bad token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad token.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not a JSON object.")

    domain = _audit_domain(payload)
    if not domain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No domain in the payload.")

    audit_id = str(payload.get("audit_id") or "").strip()
    if not audit_id:
        # Falling back to the timestamp keeps the push idempotent for a Checker
        # that has no database configured and therefore no id to send.
        audit_id = f"{domain}:{payload.get('generated_at') or ''}"

    row, is_new = await repo.save_audit(
        session,
        domain=domain,
        audit_id=audit_id[:64],
        payload=payload,
        overall_score=payload.get("overall_score"),
        pillar_scores=payload.get("pillar_scores") or {},
        rubric_version=payload.get("rubric_version"),
        audited_at=_audit_taken_at(payload),
    )
    await session.commit()
    logger.info("stored audit %s for %s (new=%s)", audit_id, domain, is_new)
    return {"stored": True, "domain": domain, "created": is_new, "id": str(row.id)}


@app.post("/imports/screaming-frog", response_class=HTMLResponse)
async def import_screaming_frog(
    request: Request,
    site_url: str = Form(...),
    export: UploadFile = File(...),
    max_urls: int = Form(0),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Parse an Internal All export into a run, then generate from it.

    **The plan-review gate is skipped, deliberately.** That gate exists to decide
    what to crawl before spending on crawling it. An import has already been
    crawled -- by the operator, in Screaming Frog, where they already chose what
    to export. There is nothing left to decide before the spend, because the
    spend already happened.
    """
    try:
        normalised = normalise_site_url(site_url)
    except ValueError as exc:
        return _import_error(request, user, str(exc))

    raw = await export.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Screaming Frog writes UTF-16 when the export is made on Windows with
        # certain locale settings, and the failure is otherwise a wall of
        # mojibake rather than a message.
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            return _import_error(
                request, user, "That file is not UTF-8 or UTF-16 text. Export it again as CSV."
            )

    entries = parse_screaming_frog_csv(text, max_urls=max(0, max_urls))
    if not entries:
        return _import_error(
            request,
            user,
            "No usable rows. An Internal All export needs Address, Status Code, "
            "Content Type and Indexability columns, and only indexable HTML pages "
            "that answered 200 are kept.",
        )

    domain = urlparse(normalised).netloc
    off_site = [e.url for e in entries if domain not in urlparse(e.url).netloc]
    if off_site:
        # A pasted export from the wrong crawl is easy to do and expensive to
        # discover later, when a client's file cites another client's pages.
        return _import_error(
            request,
            user,
            f"{len(off_site)} row(s) are not on {domain}, so this export is for a "
            f"different site. First was {off_site[0]}.",
        )

    run = await repo.create_run(session, normalised, created_by=user.email, source=SOURCE_IMPORT)
    run.max_pages = len(entries)
    brief = await repo.load_brief(session, domain)
    config = await repo.load_site_config(session, domain)

    plan = CrawlPlan(
        site_name=(config.label if config and config.label else domain),
        site_pattern=(config.plan or {}).get("site_pattern", "catalog") if config else "catalog",
        source=SOURCE_IMPORT,
        reasoning=f"Imported {len(entries)} pages from a Screaming Frog export.",
        recommended_page_cap=len(entries),
    )
    run.plan = plan.to_dict()
    run.plan_source = SOURCE_IMPORT
    run.pattern = plan.site_pattern
    run.site_name = plan.site_name

    # Embargo applies to an import exactly as it does to a crawl. The patterns
    # exist so certain pages are never stored, and an upload is a way for them to
    # arrive that skips the fetch-time filter entirely.
    kept, suppressed = split_embargoed([e.url for e in entries], brief)
    allowed = set(kept)
    entries = [e for e in entries if e.url in allowed]
    if not entries:
        return _import_error(
            request, user, "Every row in that export matches an embargo pattern for this client."
        )

    await repo.replace_pages(session, run.id, entries)
    await repo.set_status(session, run, RunStatus.CRAWLING)
    for pattern, count in sorted(suppressed.items()):
        await repo.record_event(
            session,
            run.id,
            "crawl",
            f"Embargo {pattern!r}: {count} imported row(s) discarded and not stored",
        )
    run_id = str(run.id)
    await session.commit()

    from app.jobs.tasks import generate_task

    await generate_task.defer_async(run_id=run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


def _import_error(request: Request, user: User, message: str):
    return templates.TemplateResponse(
        request,
        "import.html",
        {"user": user, "error": message, "report": None},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# -- client share links -------------------------------------------------------
#
# The only routes in this file that return client data without a session. The
# token *is* the authorisation: there is no ownership model on any table here, so
# `require_user` cannot be what protects a client's audit from a client. Neither
# the domain nor the section appears in the URL -- both come off the row -- which
# makes "the handler ignores what the request claims" a property of the shape
# rather than something a reader has to check.

#: Bounds render cost, not guessing. See `app/core/throttle.py`: at 256 bits a
#: throttle buys no guessing resistance whatsoever, and this is here because every
#: share view runs the whole assemble-and-audit path uncached.
_SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")

_share_throttle = TokenBucket(rate_per_minute=60, burst=20)


def _share_panel(
    request: Request,
    domain: str,
    *,
    minted: str = "",
    error: str = "",
    this_section: str = "",
) -> Response:
    """The mint form, and the one and only showing of a new link.

    `minted` is the plaintext URL. It is rendered here and never stored, so this
    response is the single moment it exists -- which is why the template says so
    rather than leaving the operator to discover it by coming back later.
    """
    return templates.TemplateResponse(
        request,
        "partials/share_panel.html",
        {
            "domain": domain,
            "minted": minted,
            "error": error,
            "sections": [s.value for s in share.ShareSection],
            "this_section": this_section,
            "default_days": get_settings().share_link_default_days,
            "max_days": get_settings().share_link_max_days,
        },
    )


def _share_gone(request: Request) -> Response:
    """One response for every way a link can fail.

    Unknown, expired, revoked, client deleted, domain never probed -- all return
    the same 404 with the same body. It must not reveal whether the token ever
    existed, which domain it named, or that a `/sites/...` surface exists.

    Not 410 for an expired link: 410 means "this was here", which is a disclosure
    in itself. `require_admin_or_404` already takes the same line for the same
    reason.
    """
    return templates.TemplateResponse(
        request, "client/gone.html", {}, status_code=status.HTTP_404_NOT_FOUND
    )


async def _share_render(request, session, token, *, want_pdf=False):
    """Resolve a token and build the document it authorises.

    No `user` parameter and no `current_user` call anywhere in this path -- see
    `ShareScope`, which has already removed the cookie.
    """
    settings = get_settings()
    if not settings.share_links_enabled:
        return _share_gone(request), None

    if not _share_throttle.allow(share.token_hash(token), now=time.monotonic()):
        return (
            Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS, headers={"Retry-After": "60"}),
            None,
        )

    link = await repo.resolve_share_link(session, token)
    now = datetime.now(UTC)
    if link is None:
        # No link id to log, and nothing worth logging: an unknown token is
        # usually a truncated paste. Never log the token itself -- a token in a
        # log line is a live credential in a log line, and Railway keeps logs.
        logger.warning("share token rejected (unknown)")
        return _share_gone(request), None

    state = link.state(now)
    if state != "live":
        logger.warning("share link %s rejected (%s)", link.id, state)
        return _share_gone(request), None

    site_status, view = await _site_status(session, link.domain)
    if site_status is None or view is None:
        logger.warning("share link %s has no snapshot for %s", link.id, link.domain)
        return _share_gone(request), None

    config = await repo.load_site_config(session, link.domain)
    report = build_client_report(
        view,
        site_status,
        link.section,
        client_name=(config.label if config and config.label else link.domain),
    )

    await repo.record_share_view(session, link, now=now)
    await session.commit()
    logger.info(
        "share link %s viewed (domain=%s section=%s views=%d)",
        link.id,
        link.domain,
        link.section,
        link.view_count,
    )
    return None, (link, report)


@app.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
async def share_page(
    request: Request,
    token: str = PathParam(..., min_length=share.TOKEN_CHARS, max_length=share.TOKEN_CHARS),
    session: AsyncSession = Depends(get_session),
):
    """A client's view of one section. No session, by construction."""
    if not _SHARE_TOKEN.fullmatch(token):
        return _share_gone(request)

    refusal, resolved = await _share_render(request, session, token)
    if refusal is not None:
        return refusal

    _link, report = resolved
    return templates.TemplateResponse(
        request,
        "client/report.html",
        {
            "report": report,
            "downloads": f"/share/{token}/download",
            "pdf": f"/share/{token}/pdf" if get_settings().pdf_enabled else "",
            # Set by the PDF renderer when it fetches this page. Chromium's print
            # path will not reliably force a `<details>` open from CSS, so the
            # attribute is set here instead.
            "expand": request.query_params.get("print") == "1",
        },
    )


@app.get("/share/{token}/download/{artifact}", include_in_schema=False)
async def share_download(
    request: Request,
    artifact: str,
    token: str = PathParam(..., min_length=share.TOKEN_CHARS, max_length=share.TOKEN_CHARS),
    session: AsyncSession = Depends(get_session),
):
    """One generated file, scoped to the token's domain.

    **No fallthrough default.** `/agents/download` ends in `else: render_agents_md`,
    so an unknown `kind` there returns agents.md; here an artifact the bundle does
    not name is a 404. A share route that guesses is a share route that can be
    asked for something nobody decided to share.
    """
    if not _SHARE_TOKEN.fullmatch(token):
        return _share_gone(request)

    refusal, resolved = await _share_render(request, session, token)
    if refusal is not None:
        return refusal

    link, _report = resolved
    _state, view = await _site_status(session, link.domain)
    found = view.bundle.get(artifact) if view is not None else None
    if found is None:
        return _share_gone(request)

    return PlainTextResponse(
        found.body,
        headers={"Content-Disposition": f'attachment; filename="{found.name}"'},
        media_type=f"{found.media_type}; charset=utf-8",
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    """Disallow everything, and never name `/share/`.

    Two things worth stating, because both invite a well-meant "fix":

    * A `Disallow: /share/` line would *publish the existence and shape of the
      surface it names*, to anyone who reads robots.txt. `Disallow: /` names
      nothing.
    * robots.txt is not the control here. A disallowed URL can still be indexed by
      reference, and a compliant crawler that obeys this line never fetches the
      page and so never sees its `noindex` header. That tension resolves in favour
      of both, because share URLs are never linked from anywhere public -- no
      referrer, no sitemap, no inbound link -- so indexing-by-reference needs
      somebody to publish the link, at which point it is compromised anyway.
      `X-Robots-Tag` is what covers the crawler that ignores this file, which,
      given what this tool audits, is an ironic but real population.
    """
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n", headers={"Cache-Control": "public, max-age=3600"}
    )


# -- minting and revoking, which are staff actions ----------------------------


@app.post("/sites/{domain}/share")
async def create_share(
    request: Request,
    domain: str,
    section: str = Form(...),
    days: int = Form(0),
    label: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Mint a link and show it once.

    `require_user`, not `require_admin`: every signed-in staff member already sees
    every client, so requiring an admin would add friction without adding a
    boundary.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    if not settings.share_links_enabled:
        return _share_panel(request, domain, error="Share links are turned off for this instance.")
    if section not in share.ShareSection.__members__.values():
        return _share_panel(request, domain, error="Unknown section.")

    wanted = days or settings.share_link_default_days
    if wanted > settings.share_link_max_days:
        # Refused, never silently clamped: a link the operator believes lasts a
        # year and which dies in ninety days is the silent-cap failure the
        # conventions forbid.
        return _share_panel(
            request,
            domain,
            error=f"The longest a link may last is {settings.share_link_max_days} days.",
        )

    if (
        await repo.live_share_link_count(session, domain, now=now)
        >= settings.share_links_per_domain
    ):
        return _share_panel(
            request,
            domain,
            error=(
                f"{settings.share_links_per_domain} links are already live for this client. "
                "Revoke one before making another."
            ),
        )

    _link, token = await repo.create_share_link(
        session,
        domain=domain,
        section=section,
        expires_at=now + timedelta(days=wanted),
        created_by=user.email,
        label=label,
    )
    await session.commit()
    return _share_panel(request, domain, minted=share.share_url(settings.app_url, token))


@app.post("/sites/{domain}/share/{link_id}/revoke")
async def revoke_share(
    request: Request,
    domain: str,
    link_id: UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Not admin-only. Revoking is the safe direction, and needing an admin is how
    a link stays live over a weekend."""
    link = await repo.revoke_share_link(session, link_id, by=user.email, now=datetime.now(UTC))
    if link is not None and link.domain != domain:
        # The id is a UUID and the domain is in the path; they must agree, or the
        # revoke button on one client's page could act on another's row.
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such link.")
    await session.commit()
    return RedirectResponse(f"/sites/{domain}/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/sites/{domain}/client/{section}", response_class=HTMLResponse)
async def client_preview(
    request: Request,
    domain: str,
    section: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """What the client will see, rendered for staff.

    The same builder and the same template, so a preview cannot flatter the real
    thing. No `downloads` route is passed: a preview has no token, and there is no
    other URL a signed-out client could use.
    """
    if section not in SECTION_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown section.")
    site_status, view = await _site_status(session, domain)
    if site_status is None or view is None:
        return _unchecked(request, user, domain, "Client view")
    config = await repo.load_site_config(session, domain)
    report = build_client_report(
        view,
        site_status,
        section,
        client_name=(config.label if config and config.label else domain),
    )
    return templates.TemplateResponse(
        request,
        "client/report.html",
        {
            "report": report,
            "downloads": "",
            "pdf": f"/sites/{domain}/client/{section}.pdf" if get_settings().pdf_enabled else "",
            "expand": False,
        },
    )


def _pdf_name(domain: str, section: str, when) -> str:
    """ASCII only, so no RFC 5987 encoding is needed in the header."""
    safe = "".join(c if c.isalnum() or c in "-." else "-" for c in f"{domain}-{section}")
    return f"{safe}-{when:%Y-%m-%d}.pdf"


def _pdf_unavailable(request: Request, message: str) -> Response:
    """Never a 500, and never a traceback.

    Every failure here degrades to the same advice, which is only honest because
    the print stylesheet came first: the page a client is looking at is laid out
    for paper, so their browser can produce the same document.
    """
    return templates.TemplateResponse(
        request,
        "client/no_pdf.html",
        {"message": message},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": "30"},
    )


async def _serve_pdf(request: Request, path: str, *, domain: str, section: str, client_name: str):
    """Render `path` and return the bytes.

    `path` is always a `/share/...` URL, including for the staff button, so the
    PDF is by construction a render of the page the client sees. There is no
    second code path that could drift from it -- the same reasoning `_assemble`
    gives for being one function shared by the page and the download.
    """
    from app import pdf as pdf_module

    if not get_settings().pdf_enabled or pdf_module.PDF_READY is False:
        return _pdf_unavailable(request, "PDF export is switched off on this instance.")

    when = date.today()
    try:
        body = await pdf_module.render_pdf(
            path,
            doc_title=f"{section_title(section)} - {client_name}",
            footer_left=f"Prepared by Prosperity Media for {client_name}",
        )
    except pdf_module.PdfBusy:
        return _pdf_unavailable(request, "Another export is running. Try again in a moment.")
    except (pdf_module.PdfUnavailable, pdf_module.PdfFailed) as exc:
        logger.warning("pdf render failed for %s/%s: %s", domain, section, exc)
        return _pdf_unavailable(request, "We could not build the PDF just now.")

    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_name(domain, section, when)}"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/share/{token}/pdf", include_in_schema=False)
async def share_pdf(
    request: Request,
    token: str = PathParam(..., min_length=share.TOKEN_CHARS, max_length=share.TOKEN_CHARS),
    session: AsyncSession = Depends(get_session),
):
    """The client's own download button."""
    if not _SHARE_TOKEN.fullmatch(token):
        return _share_gone(request)

    refusal, resolved = await _share_render(request, session, token)
    if refusal is not None:
        return refusal

    link, report = resolved
    return await _serve_pdf(
        request,
        f"/share/{token}?print=1",
        domain=link.domain,
        section=link.section,
        client_name=report.client_name,
    )


@app.get("/sites/{domain}/client/{section}.pdf")
async def client_preview_pdf(
    request: Request,
    domain: str,
    section: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Staff export, without sending anyone a link.

    Mints itself a short-lived token so the render still goes through `/share/`.
    Two minutes is enough for one render and useless to anyone who finds it in a
    log afterwards.
    """
    if section not in SECTION_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown section.")
    if not get_settings().share_links_enabled:
        return _pdf_unavailable(
            request, "PDF export needs share links turned on: the render fetches the client page."
        )

    site_status, view = await _site_status(session, domain)
    if site_status is None or view is None:
        return _unchecked(request, user, domain, "Client view")

    config = await repo.load_site_config(session, domain)
    name = config.label if config and config.label else domain
    _link, token = await repo.create_share_link(
        session,
        domain=domain,
        section=section,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        created_by=user.email,
        label="internal PDF export",
    )
    await session.commit()

    return await _serve_pdf(
        request,
        f"/share/{token}?print=1",
        domain=domain,
        section=section,
        client_name=name,
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


# -- refining a generated file ----------------------------------------------


REFINABLE = {"agents-md"}


async def _refine_state(session: AsyncSession, domain: str):
    """The document, its stored refinements, and the rendered result.

    One place that assembles all three, because the panel, the turn handler and
    the download must agree about what the current file is. Two assemblies would
    let an operator refine one thing and download another.
    """
    view = await _from_snapshot(session, domain)
    if view is None:
        return None, None, [], ""

    # `_from_snapshot` has already applied the refinements, so `view.doc` is the
    # refined document. Re-deriving the facts is the only extra step.
    stored = await repo.load_edit(session, domain, "agents-md")
    from app.core.refine import AssertedFact

    facts = [AssertedFact(**f) for f in (stored.facts if stored else [])]
    return view, view.doc, facts, render_agents_md(view.doc, facts=facts)


@app.get("/sites/{domain}/refine", response_class=HTMLResponse)
async def refine_panel(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    view, doc, facts, rendered = await _refine_state(session, domain)
    if view is None:
        return _unchecked(request, user, domain, "Refine")
    return await _render_refine(request, session, domain, user, doc, facts, rendered)


async def _render_refine(request, session, domain, user, doc, facts, rendered):
    from app.core.evidence import evidence_for
    from app.core.rules import audit_agents

    view = await _from_snapshot(session, domain)
    evidence = evidence_for(view)
    report = audit_agents(
        rendered,
        site_url=evidence.site_url,
        verified_urls=evidence.as_list,
        transactional=evidence.transactional,
        content_type=evidence.content_type,
    )
    stored = await repo.load_edit(session, domain, "agents-md")
    return templates.TemplateResponse(
        request,
        "partials/refine.html",
        {
            "user": user,
            "domain": domain,
            "rendered": rendered,
            "facts": facts,
            "report": report,
            "edited": stored is not None,
            "edited_by": stored.edited_by if stored else "",
            "messages": await repo.recent_chat_for_domain(session, domain),
        },
    )


@app.post("/sites/{domain}/refine", response_class=HTMLResponse)
async def refine_turn(
    request: Request,
    domain: str,
    message: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """One conversational edit, gated by the same rules that judge the file.

    The ordering is the point, and it is stricter than the llms.txt editor's:

    1. Audit what the file is now, so "was it already failing" is answerable.
    2. Ask the model for operations.
    3. Apply what `apply_refinements` allows.
    4. Audit the result.
    5. Keep it **only** if AGT-004 passes and no ERROR rule newly fails and no
       existing failure grew.

    The llms.txt gate compares error *codes* and lets an edit worsen a failure it
    already had. This compares rule ids and counts, because "it was already a bit
    broken" is not a reason to let something break it further.
    """
    from app.core.evidence import evidence_for
    from app.core.refine import apply_refinements
    from app.core.rules import audit_agents
    from app.llm.prompts import refine as refine_prompt

    view, doc, facts, rendered = await _refine_state(session, domain)
    if view is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Check the site before refining its files.")

    evidence = evidence_for(view)

    def judge(text: str):
        return audit_agents(
            text,
            site_url=evidence.site_url,
            verified_urls=evidence.as_list,
            transactional=evidence.transactional,
            content_type=evidence.content_type,
        )

    before = judge(rendered)

    # The ceiling, checked before the call rather than after. Said out loud when
    # it bites: a refusal that reads as "the model had nothing to add" is the
    # silent cap the conventions forbid.
    spent = await repo.spend_today(session, domain)
    if spent >= settings.max_interactive_calls_per_day:
        await repo.record_chat(
            session,
            domain,
            "assistant",
            f"Daily limit reached for this client: {spent} interactive calls today, "
            f"against a ceiling of {settings.max_interactive_calls_per_day}. Nothing "
            "was changed. Raise MAX_INTERACTIVE_CALLS_PER_DAY or try tomorrow.",
            author="system",
        )
        await session.commit()
        return await _render_refine(request, session, domain, user, doc, facts, rendered)

    urls = sorted(
        {c.url for c in [*doc.capabilities, *doc.read_only_urls]} | {p.url for p in doc.policies}
    )
    usage = LLMUsage()
    data = await LLMClient(settings, usage).structured(
        stage=Stage.CHAT,
        system=refine_prompt.SYSTEM,
        user=refine_prompt.build_user_message(message, doc, facts, rendered),
        schema=refine_prompt.schema(urls),
        schema_name="refine_operations",
    )
    # Persisted whether or not the model returned anything usable. A refused turn
    # still spent tokens, and a fallback still needs to be visible.
    await repo.record_spend(session, usage, domain=domain, spent_by=user.email)
    await repo.record_chat(session, domain, "user", message.strip(), author=user.email)

    if data is None:
        await repo.record_chat(
            session,
            domain,
            "assistant",
            "The model did not return a usable edit. Nothing was changed."
            + (" No OpenAI key is configured." if not settings.llm_enabled else ""),
            author="model",
        )
        await session.commit()
        return await _render_refine(request, session, domain, user, doc, facts, rendered)

    reply, operations = refine_prompt.parse(data)
    edited_doc, edited_facts, applied = apply_refinements(
        doc, list(facts), operations, author=user.email
    )
    candidate = render_agents_md(edited_doc, facts=edited_facts)
    after = judge(candidate)

    broke = _regressions(before, after)
    if broke:
        await repo.record_chat(
            session,
            domain,
            "assistant",
            f"That edit would have made the file worse ({'; '.join(broke)}), so I have not "
            "applied it. Nothing changed.",
            author="model",
        )
        await session.commit()
        return await _render_refine(request, session, domain, user, doc, facts, rendered)

    stored = await repo.load_edit(session, domain, "agents-md")
    kept = [{"op": o.op, "url": o.url, "text": o.text} for o in operations]
    await repo.save_edit(
        session,
        domain,
        "agents-md",
        operations=[*(stored.operations if stored else []), *kept]
        if applied.changed
        else (stored.operations if stored else []),
        facts=[
            {"text": f.text, "noted_by": f.noted_by, "noted_at": f.noted_at} for f in edited_facts
        ],
        edited_by=user.email,
    )

    note = reply or "Done."
    if applied.rejected:
        note += " Refused: " + "; ".join(applied.rejected)
    await repo.record_chat(session, domain, "assistant", note, author="model")
    await session.commit()
    return await _render_refine(request, session, domain, user, edited_doc, edited_facts, candidate)


def _regressions(before, after) -> list[str]:
    """What the edit broke. Empty means it is safe to keep.

    Three tests, and the second and third are what the existing chat gate lacks.
    """
    from app.core.rules import AGENTS_BY_ID
    from app.core.rules.registry import Severity

    broken: list[str] = []

    # 1. The invariant. Not "newly failed" -- failed at all. An edit must never
    #    leave a URL in the file that no probe confirmed, even if one was already
    #    there.
    if after.failed("AGT-004"):
        finding = after.by_id("AGT-004")
        broken.append(f"AGT-004: {finding.message}")

    was = {f.rule_id: f for f in before.failures}
    for finding in after.failures:
        rule = AGENTS_BY_ID.get(finding.rule_id)
        if rule is None or rule.severity is not Severity.ERROR:
            continue
        # 2. A rule that did not fail before.
        if finding.rule_id not in was:
            broken.append(f"{finding.rule_id} would start failing")
        # 3. A rule that failed before and now fails harder. "It was already a
        #    bit broken" is not permission to break it further.
        elif finding.count > was[finding.rule_id].count:
            broken.append(
                f"{finding.rule_id} would go from {was[finding.rule_id].count} to {finding.count}"
            )

    return broken


@app.post("/sites/{domain}/refine/reset", response_class=HTMLResponse)
async def refine_reset(
    request: Request,
    domain: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Discard every refinement and go back to what the generator produces."""
    await repo.clear_edit(session, domain, "agents-md")
    await session.commit()
    view, doc, facts, rendered = await _refine_state(session, domain)
    if view is None:
        return _unchecked(request, user, domain, "Refine")
    return await _render_refine(request, session, domain, user, doc, facts, rendered)


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
        "markable": {
            s.key for s in status.statuses if manually_markable(s.component) and not s.probe_decided
        },
        # What is left to do, for the overview tile. `client_count` was the
        # *total* on the checklist -- the same number for every client -- and
        # read as "6 things to do" when the checklist itself said "2 of 6 done".
        "client_todo": sum(1 for s in status.for_client() if s.state is not ComponentState.LIVE),
        "client_total": len(status.for_client()),
        "dev_todo": sum(1 for s in status.for_developer() if s.state is not ComponentState.LIVE),
        "dev_total": len(status.for_developer()),
        "checked_ago": view.checked_ago,
        "is_stale": view.is_stale,
        "label": "",
        # Outstanding work, for the two pages where work happens. Derived from
        # the same `SiteStatus` the page body renders, so a count in the nav
        # cannot disagree with the page it points at.
        #
        # These were per-family, which put a call to act on six navigational
        # tabs and on neither list. A family badge also answered a question
        # nobody asks -- "how many Delivery items are unpublished" -- while the
        # checklist, which is the thing you work through, wore nothing.
        "nav_gaps": {
            "checklist": sum(1 for s in status.for_client() if s.state is not ComponentState.LIVE),
            "handover": sum(
                1 for s in status.for_developer() if s.state is not ComponentState.LIVE
            ),
        },
        # Spec compliance of what we generated, keyed by component. Cheap: the
        # rules do no I/O. Absent for an artifact that has no rule set, which the
        # partial renders as "no checks exist" rather than as a pass.
        "reports": reports_for(view),
        "judged": JUDGED_BY,
        # The share control, on every page that has something worth sharing.
        # Added here rather than to four route bodies for the reason `build_nav`
        # is a global: the one route that forgot would render a page whose Share
        # button silently did nothing.
        "share_enabled": get_settings().share_links_enabled,
        "sections": [s.value for s in share.ShareSection],
        "default_days": get_settings().share_link_default_days,
        "max_days": get_settings().share_link_max_days,
        "minted": "",
        "error": "",
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
            "family_key": wanted.value,
            "statuses": statuses,
            # Refining is offered where the artifact is prose an operator would
            # want to reword. Configuration and machine data are not.
            "refinable": any(s.key in REFINABLE and s.publishable for s in statuses),
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


def _back_to(candidate: str, fallback: str) -> str:
    r"""A caller-supplied return path, or the fallback if it is not one.

    A form field that ends up in a `Location` header is an open redirect unless
    something checks it. Only a path on this origin is allowed: it must start
    with a single `/`, which rules out `https://evil.example` and the
    protocol-relative `//evil.example` that a lone "starts with /" test lets
    straight through.

    The backslash matters as much as the slash. Browsers normalise `\` to `/` in
    the authority position, so `/\evil.example` is scheme-relative to Chrome and
    Firefox while passing any check that only looks for `//`.
    """
    candidate = (candidate or "").strip()
    if candidate.startswith("/") and candidate[1:2] not in ("/", "\\"):
        return candidate
    return fallback


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    back: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Ask a running job to stop at its next stage boundary.

    Cooperative, and honest about it: the stage in flight finishes. A crawl of
    400 pages or a batch of LLM calls runs to completion, because interrupting
    one leaves half-written state that is worse than the work saved. What
    stopping guarantees is that no further expensive stage begins.

    `back` exists because this is reachable from the client list now, and
    bouncing an operator to a run detail page they did not ask for loses their
    place in the list they were working through.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    if RunStatus(run.status).is_terminal:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That run has already finished.")

    await repo.set_status(session, run, RunStatus.CANCELLED)
    await repo.record_event(session, run_id, "cancelled", f"Cancelled by {user.email}")
    await session.commit()
    return RedirectResponse(
        _back_to(back, f"/runs/{run_id}"), status_code=status.HTTP_303_SEE_OTHER
    )


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

    usage = LLMUsage()
    turn = await apply_chat_turn(
        LLMClient(settings, usage),
        request=message,
        site_name=run.site_name,
        site_summary=run.site_summary,
        sections=before.sections,
        optional=before.optional,
        excluded=excluded_urls,
    )

    session.add(ChatMessage(run_id=run_id, role="user", body=message.strip(), author=user.email))
    # This route had no usage object at all, so every chat turn on this tool's
    # most expensive model reached the costs page as nothing.
    await repo.record_spend(session, usage, domain=run.domain, run_id=run_id, spent_by=user.email)

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

    # Two sources, kept separate. Pipeline spend lives on `Run.stats`; interactive
    # spend -- the refine panel, the chat editor, the brief wizard -- lives in
    # `llm_spend`, because it is not a property of a run and one of the three
    # happens before any run exists. Summing them into a single figure would hide
    # which half is growing, and the interactive half is the operator-driven one.
    interactive = await repo.interactive_spend_since(session, days=window)
    by_stage: dict[str, dict[str, float]] = {}
    for row in interactive:
        entry = by_stage.setdefault(
            row.stage or "unknown",
            {"calls": 0, "prompt": 0, "completion": 0, "usd": 0.0, "unpriced": 0},
        )
        entry["calls"] += row.calls
        entry["prompt"] += row.prompt_tokens
        entry["completion"] += row.completion_tokens
        rates = rate_for(row.model) if row.model else None
        if rates is None:
            # Same rule as `cost_of`: a model with no rate is unknown, not free.
            entry["unpriced"] += row.prompt_tokens + row.completion_tokens
            continue
        input_rate, output_rate = rates
        entry["usd"] += (row.prompt_tokens / 1_000_000) * input_rate
        entry["usd"] += (row.completion_tokens / 1_000_000) * output_rate

    interactive_usd = sum(e["usd"] for e in by_stage.values())
    interactive_calls = sum(int(e["calls"]) for e in by_stage.values())

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
            "by_stage": sorted(by_stage.items()),
            "interactive_usd": interactive_usd,
            "interactive_calls": interactive_calls,
            "interactive_ceiling": settings.max_interactive_calls_per_day,
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
