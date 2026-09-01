"""Key every table by the same spelling of a domain.

Two conventions were in use at once. `repo.domain_of` lowercases and strips a
leading `www.`, and it is what `Run.domain` has always held. Five call sites in
`main.py` used `urlparse(normalised).netloc` instead, which keeps both -- and that
is what `SiteConfig` and every other client-scoped table were keyed by.

So a client added as `www.redspot.com.au` had a config under `www.redspot.com.au`
and runs under `redspot.com.au`, and `client_home` filters runs with
`r.domain == domain` against the path segment. The client page for any `www.` site
listed **no runs at all**, `runs_for_domain` returned nothing, and the delete guard
in `_settings_context` could not see the in-flight runs it exists to find. Both
spellings resolved to a page, so nothing looked broken; the pages just disagreed.

`repo.domain_of` wins, because `runs` is the largest table and already holds it.

**Non-destructive by choice.** The two unique columns can collide -- a client may
hold rows under both spellings -- and this migration never merges two rows that
both carry content. Where it cannot fold them safely it leaves the un-normalised
row in place and says so in the log. That row becomes unreachable once the code
addresses only the normalised spelling, which is untidy but loses nothing, and a
person can merge it deliberately. A migration is the wrong place to guess which
of two briefs an operator meant to keep.

Revision ID: e5b17c30a9f4
Revises: d7a3c15e9b48
"""

from __future__ import annotations

import logging

from alembic import op

revision = "e5b17c30a9f4"
down_revision = "d7a3c15e9b48"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

#: `lower()` before the strip, not after. `^www\.` on a mixed-case host does not
#: match `WWW.`, which is the same ordering bug `repo.domain_of` carried: it left
#: "WWW.NRMA.COM.AU" as "www.nrma.com.au" and made a third key for one domain.
NORMALISE = r"regexp_replace(lower(domain), '^www\.', '')"

#: Indexed, not unique. Several rows per domain is the normal state of these, so
#: rewriting the value can never collide and needs no policy.
PLAIN_TABLES = (
    "runs",
    "share_links",
    "component_marks",
    "site_metrics",
    "site_audits",
    "artifact_edits",
    "llm_spend",
)


def _normalise_plain(table: str) -> int:
    result = op.get_bind().exec_driver_sql(
        f"UPDATE {table} SET domain = {NORMALISE} WHERE domain <> {NORMALISE}"
    )
    return result.rowcount or 0


def _normalise_unique(table: str) -> int:
    """The same rewrite, skipping any row whose target spelling is already taken.

    Without the guard this statement is the migration's own failure mode: a row
    left un-normalised above because it collided is exactly a row whose target
    exists, so re-keying it violates the unique constraint and takes the deploy
    down. Skipping is the correct outcome -- those rows were reported as needing a
    human, and this leaves them for one.
    """
    result = op.get_bind().exec_driver_sql(
        f"""
        UPDATE {table} AS t SET domain = {NORMALISE.replace("domain", "t.domain")}
        WHERE t.domain <> {NORMALISE.replace("domain", "t.domain")}
          AND NOT EXISTS (
              SELECT 1 FROM {table} AS other
              WHERE other.id <> t.id
                AND other.domain = {NORMALISE.replace("domain", "t.domain")}
          )
        """
    )
    return result.rowcount or 0


def upgrade() -> None:
    for table in PLAIN_TABLES:
        moved = _normalise_plain(table)
        if moved:
            logger.info("one_domain_key: %s rows re-keyed in %s", moved, table)

    bind = op.get_bind()

    # -- site_snapshots: a cache, so a collision is resolved by keeping the newer.
    #
    # Safe to delete from because every column is re-derivable: the row is what one
    # probe of the site found, and "Check the site now" rebuilds it. Keeping the
    # older of two would be the only wrong answer.
    dropped = bind.exec_driver_sql(
        f"""
        DELETE FROM site_snapshots older
        USING site_snapshots newer
        WHERE {NORMALISE.replace("domain", "older.domain")}
            = {NORMALISE.replace("domain", "newer.domain")}
          AND older.id <> newer.id
          AND (older.fetched_at, older.id) < (newer.fetched_at, newer.id)
        """
    ).rowcount
    if dropped:
        logger.info("one_domain_key: %s stale duplicate snapshot(s) dropped", dropped)
    _normalise_unique("site_snapshots")

    # -- site_configs: never merged, because `brief` is answered by a person.
    #
    # An empty target row is the one case that folds safely: nothing is lost by
    # removing a config with no label, no plan and no answered brief, and doing so
    # lets the row that *does* carry the onboarding keep it. Anything else is left
    # alone and reported.
    folded = bind.exec_driver_sql(
        f"""
        DELETE FROM site_configs empty
        USING site_configs carrying
        WHERE empty.domain = {NORMALISE.replace("domain", "carrying.domain")}
          AND carrying.domain <> empty.domain
          AND empty.label = ''
          AND empty.brief = '{{}}'::jsonb
          AND empty.plan = '{{}}'::jsonb
          AND NOT (carrying.label = '' AND carrying.brief = '{{}}'::jsonb
                   AND carrying.plan = '{{}}'::jsonb)
        """
    ).rowcount
    if folded:
        logger.info("one_domain_key: %s empty duplicate config(s) folded", folded)

    stuck = bind.exec_driver_sql(
        f"""
        SELECT a.domain, b.domain
        FROM site_configs a
        JOIN site_configs b
          ON {NORMALISE.replace("domain", "a.domain")} = {NORMALISE.replace("domain", "b.domain")}
         AND a.domain <> b.domain
        WHERE a.domain <> {NORMALISE.replace("domain", "a.domain")}
        """
    ).fetchall()
    for source, target in stuck:
        # Left in place deliberately. Both rows carry something a person entered,
        # and picking a winner here would silently discard onboarding answers.
        logger.warning(
            "one_domain_key: %r not re-keyed -- %r already exists and both hold content. "
            "Merge them by hand; %r is no longer reachable from the app.",
            source,
            target,
            source,
        )

    _normalise_unique("site_configs")


def downgrade() -> None:
    """Not reversible, and saying so is better than pretending.

    The `www.` a row used to carry is not recorded anywhere once it is stripped,
    and the duplicate rows folded above are gone. Restoring this state means a
    backup, not a migration.
    """
    raise NotImplementedError("one_domain_key cannot be reversed; restore from a backup instead")
