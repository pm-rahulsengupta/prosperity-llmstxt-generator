"""The one page a model can cite when asked who this company is.

An llms.txt is not indexable and cannot be quoted as a source; it is a directive
an agent reads on its way somewhere else. An AI info page is the opposite -- a
normal HTML page at a stable path, linked from the footer, that a search index
can return and a model can cite. Both are worth having and they are not
substitutes.

## Everything on this page has a source

The temptation with this artefact is to write a nice paragraph about the client.
This module cannot do that and is built so it cannot start: every claim it
renders comes from `SiteBrief.facts`, and a `Fact` carries `value` **and**
`source` or it does not exist. A section with no facts behind it is omitted
rather than filled, which is why a thin page is the honest output for a site we
know little about, and why the page grows as the brief does.

That constraint is what makes the page defensible when the audit later reports
that the client's own About page contradicts it. We can say where we got it.

## Not a template, and not prose

`info_render` is the only way markup is produced here -- see its docstring for
why an f-string in this module is a source-level test failure rather than a
style preference. There is no `<script>`, and the single `<style>` block is a
literal with nothing interpolated into it.

The copy is checked by `copyrules` before it is returned, the same check the
generated llms.txt descriptions pass. A page of brand facts is exactly where a
superlative creeps in ("Australia's best rates"), and this artefact is published
on the client's own domain under their name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.core.copyrules import locale_conflicts, superlatives_in
from app.core.info_render import Html, el, join, safe_url, text
from app.core.models import Section
from app.core.text import domain_of, unquoted

__all__ = ["PAGE_PATH", "AiInfoPage", "render_ai_info"]

#: Stable by design. The guidance this artefact follows is explicit that the URL
#: must never move: a cited page that 404s is worse than one that was never
#: published, because the citation survives in model output long after the page.
PAGE_PATH = "/ai-info"

#: Deliberately small and literal. Nothing is interpolated into it, so it cannot
#: become an injection surface, and it is inlined rather than linked because the
#: client publishes one file and not a directory.
_STYLE = """
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 46rem; padding: 2rem 1.25rem 4rem;
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 1.9rem; line-height: 1.2; margin: 0 0 .5rem; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .5rem; }
.lead { font-size: 1.1rem; margin: 0 0 1.5rem; }
dl { margin: 0; }
dt { font-weight: 600; margin-top: 1rem; }
dd { margin: .15rem 0 0; }
dd.src { font-size: .85rem; opacity: .7; }
ul { padding-left: 1.1rem; }
li { margin: .3rem 0; }
footer { margin-top: 3rem; font-size: .875rem; opacity: .75; }
"""


@dataclass(slots=True)
class AiInfoPage:
    """The rendered page, and what it refused to say.

    `omitted` names the sections that had no evidence behind them. It is
    surfaced rather than swallowed because "we could not say who founded this
    company" is a question for the operator to take back to the client, and an
    empty section on a published page asks nobody anything.
    """

    html: str = ""
    facts: int = 0
    omitted: list[str] = field(default_factory=list)
    copy_issues: list[str] = field(default_factory=list)

    @property
    def thin(self) -> bool:
        """True where the page carries too little to be worth publishing.

        A page asserting only a name and a URL adds nothing a model could not
        read off the homepage, and publishing one spends the client's footer
        link on a page that does not earn it.
        """
        return self.facts < 3


def _fact_list(facts: dict, allowed: frozenset[str]) -> tuple[Html, int]:
    """Facts as a definition list, each with the source it came from.

    A fact whose source is a URL the probe never saw renders without a link
    rather than being dropped: the claim is still the operator's and still
    citable, and inventing a link to it is the failure mode that matters.
    """
    rows: list[Html] = []
    for label in sorted(facts):
        fact = facts[label]
        value = getattr(fact, "value", "")
        source = getattr(fact, "source", "")
        if not str(value).strip():
            continue
        rows.append(el("dt", str(label)))
        rows.append(el("dd", str(value)))
        if source:
            href = safe_url(source, allowed=allowed)
            cite = el("a", str(source), href=href, rel="nofollow") if href else text(str(source))
            rows.append(el("dd", join(text("Source: "), cite), class_="src"))
    return join(*rows), sum(1 for r in rows if str(r).startswith("<dt"))


def _section_list(sections: list[Section], allowed: frozenset[str]) -> Html:
    """What the site covers, from the groupings llms.txt already computed.

    Section names and descriptions only. Listing every page would reproduce
    llms.txt in HTML, and the two would then disagree the first time one was
    regenerated without the other.
    """
    items: list[Html] = []
    for section in sections:
        if not section.pages:
            continue
        label = section.name
        note = f" — {section.description}" if section.description else ""
        first = section.pages[0]
        href = safe_url(first.url, allowed=allowed)
        head = el("a", label, href=href) if href else text(label)
        items.append(el("li", join(head, text(note))))
    return el("ul", *items) if items else Html("")


def render_ai_info(
    site_url: str,
    site_name: str,
    site_summary: str,
    brief: object | None = None,
    sections: list[Section] | None = None,
    verified_urls: frozenset[str] | None = None,
    published: frozenset[str] | None = None,
    generated_on: date | None = None,
) -> AiInfoPage:
    """Build the page, omitting every section it has no evidence for.

    `published` is the set of site-absolute paths the bundle will actually ship
    (`/llms.txt`, `/agents.md`, ...). Nothing is linked that is not in it.
    """
    page = AiInfoPage()
    sections = sections or []
    domain = domain_of(site_url)
    stamp = (generated_on or date.today()).isoformat()
    # A leading ">" belongs to llms.txt syntax, not to a published meta description.
    site_summary = unquoted(site_summary)

    allowed = frozenset(u.rstrip("/") for u in (verified_urls or frozenset()))
    if site_url:
        allowed = allowed | {site_url.rstrip("/")}

    facts = dict(getattr(brief, "facts", {}) or {})
    audience = str(getattr(brief, "audience", "") or "").strip()
    found_for = str(getattr(brief, "found_for", "") or "").strip()

    body: list[Html] = [el("h1", site_name)]
    if site_summary:
        body.append(el("p", site_summary, class_="lead"))
    else:
        page.omitted.append("summary")

    fact_rows, fact_count = _fact_list(facts, allowed)
    page.facts = fact_count
    if fact_count:
        body.append(el("h2", "Facts"))
        body.append(el("dl", fact_rows))
    else:
        page.omitted.append("facts")

    if audience or found_for:
        body.append(el("h2", "Who this is for"))
        if audience:
            body.append(el("p", audience))
        if found_for:
            body.append(el("p", f"Commonly searched for: {found_for}"))
    else:
        page.omitted.append("audience")

    section_html = _section_list(sections, allowed)
    if str(section_html):
        body.append(el("h2", "What this site covers"))
        body.append(section_html)
    else:
        page.omitted.append("coverage")

    base = site_url.rstrip("/")
    # Only the files this bundle actually produced. The same rule `render_headers`
    # follows for Link rels, for the same reason: a link to a file the client
    # never publishes costs an agent a request and teaches it to distrust the
    # rest of the page.
    machine_readable = [
        el("li", el("a", f"{domain}{path}", href=f"{base}{path}"))
        for path in sorted(published or ())
    ]
    if machine_readable:
        body.append(el("h2", "Machine-readable files"))
        body.append(el("ul", *machine_readable))
    else:
        page.omitted.append("machine-readable")

    body.append(
        el(
            "footer",
            text(f"This page was last updated on {stamp}. "),
            text(f"It is maintained by {site_name} as a factual reference for "),
            text("automated readers, and is reviewed when the site changes."),
        )
    )

    description = (site_summary or f"Factual reference information about {site_name}.").strip()
    head = join(
        el("meta", charset="utf-8"),
        el("meta", name="viewport", content="width=device-width, initial-scale=1"),
        el("title", f"AI information: {site_name}"),
        el("meta", name="description", content=f"{description} Last reviewed {stamp}."),
        el("link", rel="canonical", href=f"{base}{PAGE_PATH}"),
        Html(f"<style>{_STYLE}</style>"),
    )

    page.html = (
        "<!doctype html>\n"
        + str(
            el(
                "html",
                join(el("head", head), el("body", join(*body))),
                lang="en",
            )
        )
        + "\n"
    )

    page.copy_issues = _copy_issues(site_summary, facts)
    return page


def _copy_issues(summary: str, facts: dict) -> list[str]:
    """Run the generator's own copy rules over the prose this page publishes.

    Reuses `copyrules` rather than restating the patterns. The last time this
    codebase had two implementations of a superlative check they drifted in
    opposite directions and one of them blocked a delivery over a place name, so
    there is exactly one and this calls it.
    """
    prose = [summary or ""] + [str(getattr(f, "value", "")) for f in facts.values()]
    issues: list[str] = []
    for line in prose:
        issues.extend(superlatives_in(line))
    issues.extend(locale_conflicts("\n".join(prose)))
    return sorted(set(issues))
