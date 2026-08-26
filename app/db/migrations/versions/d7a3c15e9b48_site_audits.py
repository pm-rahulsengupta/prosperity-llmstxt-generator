"""Store audits pushed from the LLM Access Checker.

The Checker is the diagnosis and this tool is the remediation, and until now they
had never spoken: an operator audited a client, got forty findings, then opened a
second tool that started from scratch and knew none of them. This is where the
findings land.

Appended rather than replaced, unlike `site_snapshots`. That table answers "how
is this site now"; this one carries a score from a versioned rubric whose author
refuses to trend across versions, so the series is what lets a number be read
with the rubric that produced it.

`audit_id` is unique so a webhook that fires twice leaves one row.

Revision ID: d7a3c15e9b48
Revises: b2e64f8c1d05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "d7a3c15e9b48"
down_revision = "b2e64f8c1d05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_audits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("audit_id", sa.String(64), nullable=False),
        sa.Column("overall_score", sa.Integer(), nullable=True),
        # `server_default` on both JSONB columns so the table is valid without a
        # rewrite if a row is ever inserted by anything but the ORM.
        sa.Column("pillar_scores", JSONB(), nullable=False, server_default="{}"),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("rubric_version", sa.Integer(), nullable=True),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("audit_id", name=op.f("uq_site_audits_audit_id")),
    )
    op.create_index(op.f("ix_site_audits_domain"), "site_audits", ["domain"])
    op.create_index("ix_site_audits_domain_audited", "site_audits", ["domain", "audited_at"])


def downgrade() -> None:
    """Drops every stored audit.

    They are recoverable -- the Checker keeps its own copy and can push again --
    which is the only reason this is a plain drop rather than a refusal.
    """
    op.drop_index("ix_site_audits_domain_audited", table_name="site_audits")
    op.drop_index(op.f("ix_site_audits_domain"), table_name="site_audits")
    op.drop_table("site_audits")
