"""Single env registry.

Every environment variable the app reads is declared here and nowhere else.
`tests/test_config.py` asserts that this module and `.env.example` agree, which is
the cheap version of the rule geo-tracker enforces via `packages/config/src/env-registry.ts`:
a variable that exists in deploy but not in the registry is a variable nobody can find.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    # Imported for the annotation only. At runtime `canonical_policy` imports it
    # inside the function: config is the lowest layer here and importing the core
    # metrics module at module scope would make it depend upward.
    from app.core.metrics import CanonicalPolicy

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
    llm_model_chat: str = "gpt-4o"

    # --- Crawler ----------------------------------------------------------
    max_browser_concurrency: int = Field(default=2, ge=1, le=8)
    max_http_concurrency: int = Field(default=8, ge=1, le=64)
    crawl_user_agent: str = "ProsperityLLMsTxtBot/1.0 (+https://prosperitymedia.com.au)"
    crawl_obey_robots: bool = True
    crawl_default_max_pages: int = 500

    # --- Firecrawl (last-resort fetch fallback, billed per page) -----------
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = "https://api.firecrawl.dev/v2"

    # --- Google Search Console (the metrics source that repairs page ranking) --
    #
    # Two ways in, because the two environments cannot share one. Locally the key
    # is a file kept outside the repo; on Railway there is no filesystem to put it
    # on, so the whole JSON document goes in a service variable. Neither ever
    # reaches git -- the repo is public, and a key that lands in it is compromised
    # on push, with rotation the only remedy.
    gsc_service_account_file: str = ""
    gsc_service_account_json: str = ""
    # Query parameters that identify a distinct page on a given site and must
    # survive canonicalisation. `?page=2` and `?sort=price` are real pages on a
    # marketplace and noise everywhere else, so this cannot be a global constant:
    # a blanket strip is exactly as wrong as no strip, just in the other
    # direction. Format: "domain=param,param;domain=param".
    canonical_meaningful_params: str = ""

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

    def canonical_policy(self, domain: str) -> CanonicalPolicy:
        """The canonicalisation rules for one site.

        The host is taken from the domain rather than configured separately: a
        run already knows which property it is for, and one more variable to
        keep in sync is one more way for two of them to disagree.
        """
        from app.core.metrics import CanonicalPolicy

        wanted = domain.lower().strip()
        for clause in self.canonical_meaningful_params.split(";"):
            name, _, params = clause.partition("=")
            if name.strip().lower() == wanted and params.strip():
                return CanonicalPolicy(
                    meaningful_params=frozenset(
                        p.strip().lower() for p in params.split(",") if p.strip()
                    ),
                    canonical_host=wanted,
                )
        return CanonicalPolicy(canonical_host=wanted)

    @property
    def gsc_enabled(self) -> bool:
        return bool(self.gsc_service_account_json or self.gsc_service_account_file)

    def gsc_credentials(self) -> dict | None:
        """The service-account document, from whichever source is configured.

        Inline JSON wins: if a deploy sets both, the variable is the deliberate
        one and a stale path left in `.env` should not quietly take precedence.
        Returns None rather than raising when unconfigured, because running
        without GSC is a supported mode, not an error -- ranking falls back to
        depth and content signals and says so.
        """
        import json
        from pathlib import Path

        if self.gsc_service_account_json:
            return json.loads(self.gsc_service_account_json)
        if self.gsc_service_account_file:
            path = Path(self.gsc_service_account_file).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"GSC service-account file not found: {path}")
            return json.loads(path.read_text(encoding="utf-8"))
        return None

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
