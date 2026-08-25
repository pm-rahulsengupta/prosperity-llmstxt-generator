"""share_links: a client-facing link whose token is the authorisation

Purely additive: one new table, no backfill, so an older image running against a
migrated database is unaffected.

The downgrade drops the table, which **fails closed** -- every outstanding share
link dies at once and the URLs return the same 404 as an unknown token. That is
the right direction for a credential store, and the links are reissuable, but it
is worth knowing before running it that a client mid-implementation loses their
copy of the handover.

The CHECK on `section` is a deliberate deviation from house style; `Run.status`
is a bare `String(32)`. `section` is authorisation-bearing, and a value no route
handles is a link that 404s forever *after* it has been emailed to a client. The
constraint moves that failure from the client's inbox to the operator's screen.

Revision ID: f41d7c9a6e02
Revises: c8f2a91b4d17
Create Date: 2026-08-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f41d7c9a6e02"
down_revision: str | None = "c8f2a91b4d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECTIONS = (
    "overview",
    "checklist",
    "handover",
    "crawl",
    "content",
    "agents",
    "capabilities",
    "delivery",
    "page",
    "report",
)


def upgrade() -> None:
    op.create_table(
        "share_links",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("label", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(320), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("expires_at > created_at", name="ck_share_links_expiry"),
        sa.CheckConstraint(
            "section IN (" + ", ".join(f"'{s}'" for s in SECTIONS) + ")",
            name="ck_share_links_section",
        ),
    )
    op.create_index("ix_share_links_token_hash", "share_links", ["token_hash"], unique=True)
    op.create_index("ix_share_links_domain", "share_links", ["domain"])
    op.create_index("ix_share_links_domain_created", "share_links", ["domain", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_share_links_domain_created", table_name="share_links")
    op.drop_index("ix_share_links_domain", table_name="share_links")
    op.drop_index("ix_share_links_token_hash", table_name="share_links")
    op.drop_table("share_links")
