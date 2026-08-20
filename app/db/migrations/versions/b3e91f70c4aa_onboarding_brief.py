"""onboarding brief

Revision ID: b3e91f70c4aa
Revises: 9357e0271021
Create Date: 2026-08-20 08:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3e91f70c4aa"
down_revision: str | None = "9357e0271021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `server_default` is load-bearing: site_configs is populated, and a NOT NULL
    # column added without one fails on the existing rows.
    op.add_column(
        "site_configs",
        sa.Column(
            "brief",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("site_configs", "brief")
