"""Accepting an audit pushed from the LLM Access Checker.

The Checker is the diagnosis and this tool is the remediation. Until now they had
never spoken: an operator audited a client, got forty findings, then opened a
second tool that started from scratch and knew none of them.

The two apps sit in different Railway projects, so the private network cannot
reach between them, and reading the Checker's Postgres directly would mean
putting a database of client audit data on the public internet with a password as
the only control. It pushes instead. That makes this endpoint the one place in
the app that takes a write from something that is not a signed-in person, so most
of what is here is about the ways that fails quietly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

TOKEN = "a-shared-secret-of-reasonable-length"

PAYLOAD = {
    "domain": "www.nrma.com.au",
    "overall_score": 32,
    "overall_grade": "F",
    "generated_at": "2026-08-26T04:15:00Z",
    "rubric_version": 4,
    "pillar_scores": {"robots_crawl": 61, "schema_entity": 12},
    "recommendations": [
        {"severity": "error", "pillar": "JS Rendering", "text": "Server-side render prices."},
        {"severity": "warn", "pillar": "AI Discoverability", "text": "No llm.txt found."},
    ],
    "llm_result": {"raw_data": {"llm_txt": {"/llms.txt": {"found": False}}}},
}


class _Recorder:
    """Enough of an AsyncSession for the route, capturing what was stored."""

    def __init__(self):
        self.saved: dict | None = None

    async def commit(self):
        pass


@pytest.fixture
def intake(monkeypatch):
    monkeypatch.setenv("ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("AUDIT_WEBHOOK_TOKEN", TOKEN)

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import repo
    from app.db.base import get_session
    from app.main import app

    recorder = _Recorder()

    async def save_audit(session, **kwargs):
        recorder.saved = kwargs
        return SimpleNamespace(id="11111111-1111-1111-1111-111111111111"), True

    monkeypatch.setattr(repo, "save_audit", save_audit)
    app.dependency_overrides[get_session] = lambda: recorder

    # No `with`: the lifespan opens a procrastinate pool against a real database.
    try:
        yield TestClient(app), recorder
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def post(client, body, token=TOKEN, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/api/audits", content=json.dumps(body), headers=headers, **kwargs)


# -- the door --------------------------------------------------------------------


def test_a_correctly_signed_push_is_stored(intake):
    client, recorder = intake

    response = post(client, PAYLOAD)

    assert response.status_code == 200
    assert response.json()["stored"] is True
    assert recorder.saved is not None


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "wrong",
        # One byte out. A `==` comparison returns at the first differing byte, so
        # this is the case a timing attack walks towards.
        TOKEN[:-1] + "x",
    ],
)
def test_a_bad_token_is_refused(intake, token):
    client, recorder = intake

    response = post(client, PAYLOAD, token=token)

    assert response.status_code == 401
    assert recorder.saved is None, "a refused push must store nothing"


def test_an_unconfigured_instance_refuses_rather_than_accepting(monkeypatch):
    """The failure that only appears where nobody is looking.

    `if token and not matches` accepts everything on an instance where the
    secret was never set. The route refuses before it compares.
    """
    monkeypatch.setenv("ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("AUDIT_WEBHOOK_TOKEN", raising=False)

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    try:
        response = TestClient(app).post("/api/audits", json=PAYLOAD)
        assert response.status_code == 503
    finally:
        get_settings.cache_clear()


# -- what gets stored ---------------------------------------------------------------


def test_the_domain_is_normalised_the_way_every_other_table_keys_it(intake):
    """The Checker sends `parsed.netloc`, so `www.` comes with it.

    Without normalising, an audit for www.nrma.com.au would sit beside the client
    an operator already has at nrma.com.au and never join to it.
    """
    client, recorder = intake

    post(client, PAYLOAD)

    assert recorder.saved["domain"] == "nrma.com.au"


def test_the_payload_is_stored_verbatim(intake):
    """It is a dict literal in the Checker's UI, not a versioned contract.

    Keeping the whole thing means a shape change upstream degrades the join
    rather than losing the audit.
    """
    client, recorder = intake

    post(client, PAYLOAD)

    assert recorder.saved["payload"] == PAYLOAD


def test_the_checkers_own_id_is_used_when_it_sends_one(intake):
    client, recorder = intake

    post(client, {**PAYLOAD, "audit_id": "abc-123"})

    assert recorder.saved["audit_id"] == "abc-123"


def test_an_audit_with_no_id_still_deduplicates(intake):
    """A Checker with no database configured has no id to send.

    Falling back to domain+timestamp keeps a retry idempotent instead of
    inserting the same audit twice.
    """
    client, recorder = intake

    post(client, PAYLOAD)
    first = recorder.saved["audit_id"]
    post(client, PAYLOAD)

    assert first == recorder.saved["audit_id"]
    assert "nrma.com.au" in first


def test_a_missing_timestamp_becomes_now_not_the_epoch(intake):
    """The epoch would sort it below every real audit for ever.

    `latest_audit` would then keep returning an older one, which is a worse lie
    than being a few seconds out.
    """
    from datetime import UTC, datetime

    client, recorder = intake

    post(client, {k: v for k, v in PAYLOAD.items() if k != "generated_at"})

    assert (datetime.now(UTC) - recorder.saved["audited_at"]).total_seconds() < 60


def test_the_rubric_version_is_kept(intake):
    """The Checker refuses to trend scores across rubric versions.

    A score rendered without it invites exactly the comparison it refuses.
    """
    client, recorder = intake

    post(client, PAYLOAD)

    assert recorder.saved["rubric_version"] == 4


# -- refusals that would otherwise fail quietly -------------------------------------


def test_an_audit_with_no_domain_is_refused(intake):
    """Storable-but-unattributable is worse than refused: it would sit in the
    table belonging to no client and be found by nothing."""
    client, recorder = intake

    response = post(client, {k: v for k, v in PAYLOAD.items() if k != "domain"})

    assert response.status_code == 400
    assert recorder.saved is None


def test_a_body_that_is_not_json_is_refused(intake):
    client, _ = intake

    response = client.post(
        "/api/audits", content="not json", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 400


def test_a_json_array_is_refused(intake):
    """`json.loads` is happy with a list; every read after it would not be."""
    client, _ = intake

    assert post(client, [PAYLOAD]).status_code == 400


# -- the context builder actually runs ------------------------------------------


@pytest.mark.parametrize("has_audit", [False, True])
async def test_the_profile_context_builds_with_an_audit_and_without_one(monkeypatch, has_audit):
    """The gap that let a missing import ship.

    `_settings_context` is what renders a client's profile, and nothing called
    it: the render tests hand templates a hand-built fixture instead. So a
    `NameError` in it passed 1,226 tests and would have been a 500 on the page.
    Ruff caught that one; this catches the next.

    Stubbed at the repo boundary rather than with a fake session, because the
    point is that the function runs end to end -- a session stub good enough to
    fool eight queries would be a second implementation of the database.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.core.onboarding import SiteBrief
    from app.db import repo
    from app.main import _settings_context

    row = SimpleNamespace(payload=PAYLOAD, audited_at=datetime.now(UTC))

    async def nothing(*a, **k):
        return None

    async def empty_list(*a, **k):
        return []

    async def empty_dict(*a, **k):
        return {}

    monkeypatch.setattr(repo, "load_site_config", nothing)
    monkeypatch.setattr(repo, "load_snapshot", nothing)
    monkeypatch.setattr(repo, "runs_for_domain", empty_list)
    monkeypatch.setattr(repo, "list_share_links", empty_list)
    monkeypatch.setattr(repo, "unfinished_runs", empty_dict)
    monkeypatch.setattr(repo, "load_brief", lambda *a, **k: _coro(SiteBrief()))
    monkeypatch.setattr(repo, "preview_client_deletion", lambda *a, **k: _coro(_deletion()))
    monkeypatch.setattr(repo, "latest_audit", lambda *a, **k: _coro(row if has_audit else None))

    context = await _settings_context(
        None, SimpleNamespace(email="a@b.c", is_admin=True), "x.example", None
    )

    assert (context["audit"] is not None) is has_audit
    if has_audit:
        assert context["audit"].overall_score == 32
        assert context["audited_ago"]


async def _coro(value):
    return value


def _deletion():
    from app.db.repo import ClientDeletion

    return ClientDeletion(
        domain="x.example",
        runs=0,
        pages=0,
        marks=0,
        metric_rows=0,
        snapshots=0,
        edits=0,
        spend_rows=0,
        share_links=0,
        audits=0,
        config=0,
    )
