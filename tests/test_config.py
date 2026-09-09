"""The env registry and .env.example must not drift apart.

geo-tracker enforces this with `packages/config/src/env-registry.ts`; this is the
cheap Python version of the same rule. A variable that exists in deploy but in no
registry is a variable nobody can find, and a documented variable the code never
reads is worse -- it looks configured and does nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def documented_vars() -> set[str]:
    return {
        match.group(1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (match := _ASSIGNMENT.match(line.strip()))
    }


def registry_vars() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_every_documented_variable_is_read_by_the_app():
    undeclared = documented_vars() - registry_vars()
    assert not undeclared, (
        f".env.example documents variables app/config.py never reads: {sorted(undeclared)}"
    )


def test_every_registry_variable_is_documented():
    undocumented = registry_vars() - documented_vars()
    assert not undocumented, (
        f"app/config.py reads variables .env.example never mentions: {sorted(undocumented)}"
    )


def test_placeholder_secrets_are_treated_as_absent():
    settings = Settings(
        openai_api_key="your_api_key_here",
        firecrawl_api_key="change-me",
        dataforseo_password="  ",
    )
    assert settings.llm_enabled is False
    assert settings.firecrawl_enabled is False
    assert settings.size_check_runnable is False


def test_real_secrets_survive_and_enable_their_feature():
    settings = Settings(
        openai_api_key="sk-live-123",
        firecrawl_api_key="fc-live-123",
        dataforseo_login="user@example.com",
        dataforseo_password="hunter2",
    )
    assert settings.llm_enabled
    assert settings.firecrawl_enabled
    assert settings.size_check_runnable is False, (
        "credentials alone must not start a paid check; SIZE_CHECK_ENABLED is the decision"
    )


def test_deploy_refuses_to_start_with_a_development_session_secret():
    settings = Settings(app_url="https://llmstxt.example.com")
    with pytest.raises(RuntimeError) as excinfo:
        settings.assert_deployable()
    assert "SESSION_SECRET" in str(excinfo.value)


def test_local_development_is_not_held_to_the_deploy_rules():
    Settings(app_url="http://localhost:3000").assert_deployable()


def test_allowed_domains_are_normalised():
    settings = Settings(allowed_email_domains="@Prosperitymedia.com.au, example.com ")
    assert settings.allowed_domains == frozenset({"prosperitymedia.com.au", "example.com"})


# -- the paid call that answered a retired question ------------------------------


def test_credentials_alone_do_not_start_a_paid_size_check():
    """`size_check_enabled` returned `bool(login and password)`, so it answered
    "can we call DataForSEO" and every caller read it as "should we".

    DataForSEO is configured here for keyword and SERP work that has nothing to
    do with sizing, so every deployment that wanted those paid one SERP call per
    run for this. Wanting the credentials and wanting this check are different
    decisions and now take different switches.
    """
    from app.config import Settings

    credentialled = Settings(
        _env_file=None, dataforseo_login="user@example.com", dataforseo_password="hunter2"
    )

    assert credentialled.size_check_enabled is False, "the decision defaults to off"
    assert credentialled.size_check_runnable is False, "credentials alone must not spend"


def test_the_check_needs_both_the_opt_in_and_the_credentials():
    from app.config import Settings

    opted_in_only = Settings(_env_file=None, size_check_enabled=True)
    both = Settings(
        _env_file=None,
        size_check_enabled=True,
        dataforseo_login="user@example.com",
        dataforseo_password="hunter2",
    )

    assert opted_in_only.size_check_runnable is False
    assert both.size_check_runnable is True


def test_a_missing_count_names_the_real_reason():
    """It reported `NO_CREDENTIALS`, which sends an operator to fix a key that is
    almost always present and was never the cause."""
    from app.scrape.sizing import CountFailure, IndexedCount

    retired = IndexedCount(reason=CountFailure.RETIRED)

    assert retired.failed
    assert "no longer returns one" in retired.explain()
    assert "credentials" not in retired.explain().split("SIZE_CHECK_ENABLED")[0].lower()
