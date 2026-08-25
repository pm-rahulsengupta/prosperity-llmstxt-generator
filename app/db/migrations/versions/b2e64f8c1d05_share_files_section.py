"""allow 'files' as a share section

The export list is a section a client can be sent on its own, so the CHECK
constraint that decides which sections a token may name has to admit it.

This is the cost of putting the section list in the database, and it is worth
paying: the constraint is what stops a token being minted for a section no route
renders, which would be a link that 404s forever *after* it was emailed to a
client. A migration per new section is a small price for that failure landing on
the operator's screen instead of in the client's inbox.

Revision ID: b2e64f8c1d05
Revises: f41d7c9a6e02
Create Date: 2026-08-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b2e64f8c1d05"
down_revision: str | None = "f41d7c9a6e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = [
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
]
AFTER = [*BEFORE, "files"]


def _rewrite(values: list[str]) -> None:
    op.drop_constraint("ck_share_links_section", "share_links", type_="check")
    op.create_check_constraint(
        "ck_share_links_section",
        "share_links",
        "section IN (" + ", ".join(f"'{value}'" for value in values) + ")",
    )


def upgrade() -> None:
    _rewrite(AFTER)


def downgrade() -> None:
    # Any link already minted for the files section would violate the narrower
    # constraint, so it goes first. Revoking rather than deleting keeps the audit
    # trail of who sent it and when.
    op.execute(
        "UPDATE share_links SET revoked_at = now(), revoked_by = 'downgrade' "
        "WHERE section = 'files' AND revoked_at IS NULL"
    )
    op.execute("DELETE FROM share_links WHERE section = 'files'")
    _rewrite(BEFORE)
