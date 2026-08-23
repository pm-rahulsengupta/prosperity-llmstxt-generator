"""add site_snapshots probe cache

Purely additive: a new table, no column added to an existing one and no backfill.
An older image running against the migrated database is unaffected, so this can
be applied before the code that reads it is deployed.

The downgrade drops the table, which loses only cached probe results. Those are
regenerable by pressing Refresh on each client -- no operator-entered data lives
here, which is why dropping it is an acceptable downgrade rather than a data loss.

Revision ID: 7a1a545f239f
Revises: dcc63583bd97
Create Date: 2026-08-21 08:53:32.342507+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7a1a545f239f"
down_revision: str | None = "dcc63583bd97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column(
            "probe", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "readiness",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "tech", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("fetched_by", sa.String(length=320), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_site_snapshots_domain"), "site_snapshots", ["domain"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_site_snapshots_domain"), table_name="site_snapshots")
    op.drop_table("site_snapshots")
