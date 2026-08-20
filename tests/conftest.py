from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Tests run against the source tree, not an installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.core.models import PageEntry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's `.env` or shell decide what the tests assert.

    `Settings` reads `.env` and `.env.local` by design, which is right for the app
    and wrong for a test suite: the deploy-safety test passed on a clean checkout
    and silently stopped testing anything the moment a real SESSION_SECRET existed
    on disk. Both sources are cut here, for every test, so the suite behaves the
    same on this laptop and in CI.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def sf_csv() -> str:
    return (FIXTURES / "screaming_frog_internal_all.csv").read_text(encoding="utf-8-sig")


@pytest.fixture
def page() -> PageEntry:
    """A mid-importance page with every signal present."""
    return PageEntry(
        url="https://example.com/docs/quickstart",
        title="Quick Start Guide | Example",
        description="Get started with Example in five minutes.",
        word_count=800,
        crawl_depth=2,
        unique_inlinks=12,
        link_score=60,
    )
