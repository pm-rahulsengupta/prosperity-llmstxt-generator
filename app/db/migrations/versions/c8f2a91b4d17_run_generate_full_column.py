"""move generate_full out of the plan blob and onto the run

The flag was written into `runs.plan` at creation, but three separate code paths
do `run.plan = plan.to_dict()` and `CrawlPlan` has no such field -- so the
round-trip through the dataclass dropped it every time. Ticking "Also build
llms-full.txt" produced a run with no llms-full.txt, silently.

A run option is not part of the crawl plan. It gets its own column, where
nothing rewrites it.

Revision ID: c8f2a91b4d17
Revises: 50c248cd3b5d
Create Date: 2026-08-25 05:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f2a91b4d17"
down_revision: str | None = "50c248cd3b5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("generate_full", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Recover the intent of any run whose flag survived long enough to be stored.
    # A run created but not yet approved still carries it in the blob, and losing
    # it on deploy would repeat the bug this migration exists to end.
    op.execute("UPDATE runs SET generate_full = true WHERE plan ->> 'generate_full' = 'true'")


def downgrade() -> None:
    op.drop_column("runs", "generate_full")
