"""Single env registry.

Every environment variable the app reads is declared here and nowhere else.
`tests/test_config.py` asserts that this module and `.env.example` agree, which is
the cheap version of the rule geo-tracker enforces via `packages/config/src/env-registry.ts`:
a variable that exists in deploy but not in the registry is a variable nobody can find.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppTarget = Literal["web", "worker", "migrate"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core -------------------------------------------------------------
    port: int = 3000
    app_target: AppTarget = "web"
    run_migrations: bool = False
    app_url: str = "http://localhost:3000"
    session_secret: str = "dev-only-change-me"
    log_level: str = "INFO"

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql://postgres:postgres@localhost:5432/llmstxt"

    # --- Google SSO -------------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_email_domains: str = "prosperitymedia.com.au"

    # Skip authentication entirely. For the test suite and for a local run against
    # no database. `assert_deployable` refuses it on https, so it cannot be what
    # leaves a deployed instance open.
    allow_anonymous: bool = False

    # --- LLM --------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str = ""
    llm_model_plan: str = "gpt-4o"
    llm_model_triage: str = "gpt-4o-mini"
    llm_model_summarise: str = "gpt-4o-mini"
    llm_model_qa: str = "gpt-4o"

    # --- Crawler ----------------------------------------------------------
    max_browser_concurrency: int = Field(default=2, ge=1, le=8)
    max_http_concurrency: int = Field(default=8, ge=1, le=64)
    crawl_user_agent: str = "ProsperityLLMsTxtBot/1.0 (+https://prosperitymedia.com.au)"
    crawl_obey_robots: bool = True
    crawl_default_max_pages: int = 500

    # --- Firecrawl (last-resort fetch fallback, billed per page) -----------
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = "https://api.firecrawl.dev/v2"

    # --- Size pre-check (DataForSEO `site:` query, one SERP call per site) -
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    size_check_location_code: int = 2036  # Australia
    size_check_language_code: str = "en"

    # ----------------------------------------------------------------------

    @field_validator(
        "openai_api_key",
        "google_client_secret",
        "firecrawl_api_key",
        "dataforseo_password",
        mode="after",
    )
    @classmethod
    def _reject_placeholders(cls, v: str) -> str:
        """Treat an obvious placeholder as absent.

        Upstream getcito ships `OPENAI_API_KEY=your_api_key_here`, and geo-tracker's
        ops doc records the resulting failure mode: the placeholder counts as
        "configured", wins provider selection, and then fails at call time. Blanking
        it here means the LLM stages degrade to heuristics instead, which is the
        behaviour we actually want.
        """
        low = v.strip().lower()
        if low in {"", "changeme", "change-me"} or ("your" in low and "here" in low):
            return ""
        return v.strip()

    @property
    def allowed_domains(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower().lstrip("@")
            for d in self.allowed_email_domains.split(",")
            if d.strip()
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def firecrawl_enabled(self) -> bool:
        return bool(self.firecrawl_api_key)

    @property
    def size_check_enabled(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)

    @property
    def sso_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def assert_deployable(self) -> None:
        """Fail fast at boot rather than silently running insecurely.

        The source app shipped with FLASK_SECRET_KEY unset, so production session
        cookies were signed with the literal string "dev-only-change-me".
        """
        problems: list[str] = []
        if self.app_url.startswith("https://"):
            if self.session_secret == "dev-only-change-me":
                problems.append("SESSION_SECRET is still the development default")
            # Google is optional -- password accounts are a complete way in, and are
            # what geo-tracker runs in production. Anonymous access is not: it would
            # mean a public URL with no gate at all.
            if self.allow_anonymous:
                problems.append("ALLOW_ANONYMOUS cannot be set on an https deployment")
            if self.sso_enabled and not self.allowed_domains:
                problems.append("ALLOWED_EMAIL_DOMAINS must not be empty when Google SSO is on")
        if problems:
            raise RuntimeError("Refusing to start: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
