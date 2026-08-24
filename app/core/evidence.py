"""What the probe actually confirmed, gathered for the rule engine.

`app/core/rules/` has held forty-three rules and a scoring engine since it was
written, and until now **nothing outside the tests imported it**. The live path
validated a generated llms.txt with `app/core/validate.py` -- a nine-check string
scanner whose error codes are things like `"h1"` -- and validated agents.md with
nothing at all.

Which means AGT-004, *"Every URL in the file must be one the probe confirmed. An
agent will follow these"*, was written, tested, and never ran. The invariant held
only because `agents_doc` and `bundle` refuse to *emit* an unverified claim in the
first place. That is a good defence and it is a single point of failure: anything
that edits a file after generation -- a human, or the refine layer -- bypasses it
entirely.

This module is the missing half. It turns what a probe stored into the arguments
the audit functions want, so the same rules that should always have gated the
output can gate anything, including an edit.

## The sentinel

`audit_agents` sets `probe_ran = verified_urls is not None`. So:

    verified_urls=[]    the probe ran and confirmed nothing -> AGT-004 RUNS,
                        and every URL in the file fails it
    verified_urls=None  no probe ran -> AGT-004 SKIPS

Passing `[]` where `None` was meant condemns a correct file. Passing `None` where
`[]` was meant silently disables the rule that matters most, and nothing anywhere
would report that it had stopped running. `verified_for` returns `None` only when
there is genuinely no snapshot, and `test_evidence.py` asserts both directions.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Evidence", "evidence_for"]


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the rules may treat as established fact about a site.

    Frozen, because a rule that could widen its own evidence would be no check at
    all.
    """

    site_url: str
    # None means no probe has run. An empty tuple means one ran and confirmed
    # nothing -- see the module docstring; these are opposite claims.
    verified_urls: tuple[str, ...] | None = None
    transactional: bool | None = None
    content_type: str | None = None

    @property
    def as_list(self) -> list[str] | None:
        """In the shape `audit_agents` wants, preserving the None sentinel."""
        return None if self.verified_urls is None else list(self.verified_urls)


def evidence_for(view) -> Evidence:
    """Gather from a `SiteView`. No network, no database -- it is all stored.

    Four sources, each of which already carries its own justification for being
    citable:

    * `probe.verified_endpoints` -- its own docstring calls these "the only
      endpoints the generator may name" -- read from the site's own UCP profile.
    * `tech.endpoint_urls` -- surfaces that answered with the right content type.
    * `bundle.verified_endpoints` -- operator-declared endpoints that
      `verify_declared` confirmed answer. An operator's word alone is not enough
      and never has been.
    * the site's own origin, which is verified by definition once probed.
    * `view.crawled_urls` -- pages a completed crawl fetched and recorded.

    That last one was left out of the first version of this module, and the rule
    engine caught it the first time it ran against real output: AGT-004 condemned
    twelve URLs in our own generated agents.md, all twelve of them crawled pages.

    The reasoning that excluded them -- that "a URL existed when we fetched it" is
    weaker than "this endpoint answers as advertised" -- is a real distinction,
    and it applies to an **endpoint an agent will call**, not to a **page an agent
    will read**. `_assemble` had already drawn that line correctly: *"pages that
    crawl already fetched are pages this one can cite -- the evidence rule met by
    a different means rather than waived."* Two parts of the codebase disagreeing
    about what counts as verified is precisely what wiring the rules in was for.

    Deliberately still excluded: anything the operator merely typed, and anything
    a sitemap merely listed. A sitemap entry is a claim by the site about itself
    that nobody has checked.
    """
    if view is None:
        return Evidence(site_url="")

    urls: set[str] = {view.site_url.rstrip("/")}
    urls.update(u.rstrip("/") for u in getattr(view, "crawled_urls", ()) if u)
    urls.update(u.rstrip("/") for u in view.probe.verified_endpoints if u)
    urls.update(u.rstrip("/") for u in view.tech.endpoint_urls if u)
    urls.update(u.rstrip("/") for u in view.bundle.verified_endpoints if u)

    return Evidence(
        site_url=view.site_url,
        verified_urls=tuple(sorted(u for u in urls if u)),
        transactional=view.tech.sells,
        content_type=_content_type_of(view),
    )


def _content_type_of(view) -> str | None:
    """What the live agents.md was served as, if the site publishes one.

    `None` where the site publishes none, which is not the same as a file served
    with a missing or wrong type -- AGT's content-type rule needs to tell those
    apart, and a default of `""` would collapse them.
    """
    surface = getattr(view.probe, "agents_md", None)
    if surface is None:
        return None
    return surface.content_type or None


# -- applying the rules to what was generated --------------------------------


#: Which rule set judges which artifact. Every generated artifact now has one.
#: The UI still reads this mapping to decide between "checked" and "no checks
#: exist yet", so a future artifact added without a rule set renders honestly as
#: unchecked rather than as an implied clean bill.
JUDGED_BY: dict[str, str] = {
    "llms.txt": "index",
    "llms-full.txt": "full",
    "agents.md": "agents",
    "robots.txt": "crawl",
    "_headers": "headers",
    "ai-catalog.json": "catalog",
}


def _policy_of(view) -> str | None:
    """The AI bot policy the operator stated, or None where they did not.

    CRW-009 compares the published file against this. `None` makes it skip,
    because a file disagreeing with an intent nobody recorded is a question for
    a person rather than a finding.
    """
    brief = getattr(view, "brief", None)
    policy = getattr(brief, "ai_bot_policy", None)
    return getattr(policy, "value", None)


def reports_for(view) -> dict[str, object]:
    """Audit every generated artifact that has a rule set. Keyed by component.

    Pure and cheap -- the rules do no I/O, and the bodies are already in the
    bundle -- so this can run on a GET without the caching the probe needed.
    """
    from app.core.components import COMPONENTS
    from app.core.rules import audit, audit_agents, audit_catalog, audit_crawl, audit_headers

    if view is None:
        return {}

    ev = evidence_for(view)
    bodies = {a.name: a.body for a in view.bundle.artifacts}
    reports: dict[str, object] = {}

    for component in COMPONENTS:
        which = JUDGED_BY.get(component.artifact)
        body = bodies.get(component.artifact, "")
        if which is None or not body.strip():
            continue
        if which == "agents":
            reports[component.key] = audit_agents(
                body,
                site_url=ev.site_url,
                verified_urls=ev.as_list,
                transactional=ev.transactional,
                content_type=ev.content_type,
            )
        elif which == "crawl":
            # `fetched=False`: this is the block we generate, an addition to a
            # file we never see. The rules about a catch-all group and about
            # search crawlers are properties of the merged result and skip here.
            reports[component.key] = audit_crawl(
                body,
                intended_policy=_policy_of(view),
                site_url=ev.site_url,
                fetched=False,
            )
        elif which == "headers":
            reports[component.key] = audit_headers(
                body, artifacts=set(bodies), site_url=ev.site_url
            )
        elif which == "catalog":
            reports[component.key] = audit_catalog(
                body, artifacts=set(bodies), site_url=ev.site_url
            )
        elif which == "index":
            reports[component.key] = audit(index_text=body)
        else:
            reports[component.key] = audit(full_text=body)

    return reports
