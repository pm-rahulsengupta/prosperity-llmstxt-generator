"""procrastinate job queue schema

Revision ID: a1c4f9d2e701
Revises: 0674f4a4a074
Create Date: 2026-08-20

The queue's tables are part of the deployed schema and have to be created by the
same migrate step as everything else. `procrastinate schema --apply` is a separate
command that is not idempotent, so relying on it means either a human remembering
to run it once per environment or a deploy that fails the second time it runs.

The SQL is pinned as a file rather than read from the installed library at runtime.
Calling `SchemaManager.get_schema()` here would mean this revision produces a
different schema after a procrastinate upgrade -- fine on a fresh database, wrong
on an existing one, and undetectable until the two disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "a1c4f9d2e701"
down_revision: str | None = "0674f4a4a074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "procrastinate_schema.sql"


def upgrade() -> None:
    op.execute(SQL_FILE.read_text(encoding="utf-8"))


# A hand-written list of objects to drop goes stale the first time procrastinate
# adds one, and the symptom is a later upgrade failing on a type that a downgrade
# claimed to have removed. This drops whatever is actually there by name prefix.
DOWNGRADE = """
DO $$
DECLARE obj record;
BEGIN
    FOR obj IN
        SELECT c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm')
          AND c.relname LIKE 'procrastinate\\_%'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', obj.name);
    END LOOP;

    FOR obj IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%'
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', obj.sig);
    END LOOP;

    FOR obj IN
        SELECT t.typname AS name
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typname LIKE 'procrastinate\\_%'
          AND t.typtype IN ('e', 'c')
    LOOP
        EXECUTE format('DROP TYPE IF EXISTS public.%I CASCADE', obj.name);
    END LOOP;
END $$;
"""


def downgrade() -> None:
    op.execute(DOWNGRADE)
