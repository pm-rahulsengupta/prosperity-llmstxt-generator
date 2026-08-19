from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Tests run against the source tree, not an installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import PageEntry

FIXTURES = Path(__file__).parent / "fixtures"


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
