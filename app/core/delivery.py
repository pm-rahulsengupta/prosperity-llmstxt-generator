"""What is wrong with this handover, and whose fault it is.

Every input to this module already existed. The rule engine scored the files, the
run recorded its fallbacks, `render_llms_full` stated its own truncation, and the
crawl counted the pages it could not read. All of it was reachable and none of it
was assembled anywhere, so the question an operator actually asks before sending a
bundle to a client -- *is this right, and if not, is it us or them* -- had no
answer short of reading four screens and knowing which numbers to compare.

**The split is the point.** A findings list that mixes the two is worse than no
list, because the response to each is opposite:

* A **defect** is ours. The file is wrong, we can fix it, and it must not be sent
  in this state. `llms-full.txt` shipped with no blockquote for the life of this
  tool; XF-001 has been failing since the rule was written and nothing read it.
* A **finding** is the client's. Their site has duplicate titles, thin pages, no
  rental-policy page. That is the audit doing its job and it belongs in the
  deliverable, not in the way of it.
* A **limit** is neither. The full file stops at FULL-009's budget; three pages
  need JavaScript we did not run; the `site:` index count is unavailable. Nobody
  is at fault and nothing is broken -- but a client who is not told will read the
  gap as one of the first two.

Nothing here re-derives a verdict. It reads what other modules already decided and
says which pile each belongs in, so that a wrong answer is a wrong answer in one
place rather than a second opinion competing with the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.rules import audit, audit_agents
from app.core.rules.registry import Outcome, Report

__all__ = [
    "DeliveryReport",
    "Item",
    "Kind",
    "check_delivery",
]

#: Rules that describe the *site* rather than the file we built from it. A rule
#: here failing is a finding to report to the client; the same rule elsewhere would
#: be a defect to fix before sending. The list is explicit rather than derived from
#: severity, because severity says how much a failure matters and this says who it
#: is about, and no amount of the first answers the second.
SITE_RULES = frozenset(
    {
        "IDX-008",  # duplicate titles -- two of the client's pages share a name
        "IDX-009",  # duplicate URLs
        "FULL-008",  # thin pages -- the client's content, not our rendering
        "AGT-014",  # how the client's server publishes the file
    }
)

#: Rules that cannot be answered without evidence we may not have. A skip here is
#: a limit, not a pass: an unverified file scoring the same as a verified one is
#: the inflation the skip outcomes exist to prevent, and hiding the skip would put
#: it straight back.
EVIDENCE_RULES = frozenset({"IDX-010", "AGT-014", "FULL-010"})


class Kind(StrEnum):
    """Who has to act."""

    #: Ours. Do not send the bundle in this state.
    DEFECT = "defect"
    #: The client's. Report it; it is what they are paying for.
    FINDING = "finding"
    #: Nobody's. Say it out loud so the gap is not misread as one of the above.
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class Item:
    kind: Kind
    title: str
    detail: str
    #: The rule that decided it, where one did. Empty for the run-level checks,
    #: which have no rule because no rule can see a run.
    rule_id: str = ""
    #: What to do. Present on a defect, because a defect an operator cannot act on
    #: is just an apology.
    remedy: str = ""


@dataclass(slots=True)
class DeliveryReport:
    """Everything wrong with one handover, sorted into who acts."""

    items: list[Item] = field(default_factory=list)
    index_score: int | None = None
    agents_score: int | None = None
    #: Files present in the bundle, by name, with their size.
    files: dict[str, int] = field(default_factory=dict)

    @property
    def defects(self) -> list[Item]:
        return [i for i in self.items if i.kind is Kind.DEFECT]

    @property
    def findings(self) -> list[Item]:
        return [i for i in self.items if i.kind is Kind.FINDING]

    @property
    def limits(self) -> list[Item]:
        return [i for i in self.items if i.kind is Kind.LIMIT]

    @property
    def sendable(self) -> bool:
        """Whether this bundle can go to a client as it stands.

        Findings and limits do not block: the first is the deliverable and the
        second is a fact about it. Only a defect does, because a defect means the
        file itself is wrong and sending it spends the client's trust on something
        we already know about.
        """
        return not self.defects

    def summary(self) -> str:
        if not self.items:
            return "Nothing outstanding."
        parts = []
        if self.defects:
            parts.append(f"{len(self.defects)} to fix before sending")
        if self.findings:
            parts.append(f"{len(self.findings)} to report to the client")
        if self.limits:
            parts.append(f"{len(self.limits)} to state in the handover")
        return "; ".join(parts) + "."


def check_delivery(
    *,
    llms_txt: str = "",
    llms_full: str = "",
    agents_md: str = "",
    expected_files: dict[str, str] | None = None,
    directories: dict[str, dict[str, str]] | None = None,
    copy_issues: list[str] | None = None,
    run_stats: dict | None = None,
    must_appear: set[str] | None = None,
    generate_full: bool = False,
) -> DeliveryReport:
    """Audit an assembled handover. Pure: no network, no database.

    `expected_files` is the bundle as `{name: body}`, so a file that should be
    there and is empty is caught as an absence rather than by whoever opens the zip.
    `directories` is the same for the artifacts that are directories rather than
    files -- without it `md/` and `okf/` arrive with an empty body and are read as
    empty files, which is the misreport this function exists to prevent.
    `run_stats` is `Run.stats` and carries what only the run knows -- which stages
    fell back, which pages needed JavaScript, whether the size check answered.
    """
    report = DeliveryReport()
    files = expected_files or {}
    dirs = directories or {}
    report.files = {name: len(body or "") for name, body in files.items() if name not in dirs}
    report.files.update(
        {name: sum(len(b or "") for b in bodies.values()) for name, bodies in dirs.items()}
    )

    _check_files(report, files, generate_full=generate_full)
    _check_directories(report, dirs)
    _check_copy(report, copy_issues or [])
    _check_rules(report, llms_txt, llms_full, agents_md)
    _check_run(report, run_stats or {}, llms_txt, must_appear or set())
    return report


def _check_files(report: DeliveryReport, files: dict[str, str], *, generate_full: bool) -> None:
    """A file that is named and empty, which is the absence nobody notices.

    An operator opening a handover reads the list of names, not the sizes. The
    four artifacts this tool generates were once all built, stored and dropped
    from the bundle for six of seven goals -- reported by someone looking for
    `agents.md` and not finding it, which is the slow way to learn.
    """
    required = ["llms.txt", "agents.md", "robots.txt"]
    if generate_full:
        required.append("llms-full.txt")

    for name in required:
        if name not in files:
            report.items.append(
                Item(
                    Kind.DEFECT,
                    f"{name} is missing from the handover",
                    "It was asked for and the bundle does not contain it.",
                    remedy=f"Re-run the assembly and confirm {name} is generated.",
                )
            )
        elif not (files[name] or "").strip():
            report.items.append(
                Item(
                    Kind.DEFECT,
                    f"{name} is present but empty",
                    "A named file with no content reads as delivered and is not.",
                    remedy="Check the stage that writes it; an empty body means it "
                    "returned nothing rather than that the site needs nothing.",
                )
            )


def _check_directories(report: DeliveryReport, dirs: dict[str, dict[str, str]]) -> None:
    """A directory artifact that arrived with no files in it.

    The file-level check cannot see this: a directory carries an empty `body` by
    design, so `md/` with nothing in it and `md/` with 419 files look identical
    to `_check_files`. An empty directory in a handover is the same failure as an
    empty file -- it reads as delivered and is not.
    """
    for name, bodies in dirs.items():
        if not bodies:
            report.items.append(
                Item(
                    Kind.DEFECT,
                    f"{name} is in the handover with no files in it",
                    "A directory that reads as delivered and contains nothing.",
                    remedy=f"Check the stage that builds {name}; an empty directory "
                    "means it produced nothing rather than that the site needs nothing.",
                )
            )
            continue
        empty = [path for path, body in bodies.items() if not (body or "").strip()]
        if empty:
            report.items.append(
                Item(
                    Kind.DEFECT,
                    f"{name} contains {len(empty)} empty files",
                    "An empty page and a page we failed to read are indistinguishable "
                    "to whoever fetches it.",
                    remedy="Drop them from the directory or fill them; do not publish both.",
                )
            )


def _check_copy(report: DeliveryReport, issues: list[str]) -> None:
    """Superlatives and mixed spellings in prose we are about to publish.

    `render_ai_info` has run `copyrules` over the page's own claims since it was
    written -- its docstring says so -- and stored the result in
    `AiInfoPage.copy_issues`, which nothing read. So the check ran, reached a
    verdict, and was discarded, on the one artifact that carries the client's
    name over prose we wrote.

    A defect rather than a finding: this is our copy on their domain, so it is
    ours to fix before sending, not theirs to be told about.
    """
    if not issues:
        return
    report.items.append(
        Item(
            Kind.DEFECT,
            f"ai-info.html carries {len(issues)} unverifiable claim(s)",
            "Superlatives and mixed spellings in copy we wrote, on a page published "
            f"under the client's name: {', '.join(sorted(issues)[:5])}.",
            remedy="Reword the fact in the brief; the page is rendered from it.",
        )
    )


def _check_rules(report: DeliveryReport, llms_txt: str, llms_full: str, agents_md: str) -> None:
    """Run the rule engine and sort what it says by who it is about."""
    if llms_txt.strip() or llms_full.strip():
        index_report = audit(llms_txt, llms_full)
        report.index_score = index_report.score
        _absorb(report, index_report, disclosed=_disclosed_in(llms_full))

    if agents_md.strip():
        agents_report = audit_agents(agents_md)
        report.agents_score = agents_report.score
        _absorb(report, agents_report)


#: Rules whose failure is a budget working rather than a mistake, *if* the file
#: says so itself. XF-002 asks that every indexed URL appear in the full file, and
#: FULL-009 caps that file at 800,000 characters; on any site past a few hundred
#: pages the two cannot both be satisfied and the cap is the one that should win.
#: `render_llms_full` already writes the shortfall into the file's own footer.
#:
#: So the verdict turns on disclosure, not on arithmetic. A truncation the file
#: states is a limit -- a client reading it knows what they have. The same
#: truncation with the footer missing is a defect, because then the file quietly
#: claims to be the whole site.
DISCLOSABLE_RULES = frozenset({"XF-002"})

_TRUNCATION_MARKER = "omitted to keep this file under"


def _disclosed_in(llms_full: str) -> frozenset[str]:
    """Which disclosable rules this file has already owned up to."""
    return DISCLOSABLE_RULES if _TRUNCATION_MARKER in llms_full else frozenset()


def _kind_for(rule_id: str, disclosed: frozenset[str]) -> Kind:
    if rule_id in disclosed:
        return Kind.LIMIT
    if rule_id in SITE_RULES:
        return Kind.FINDING
    return Kind.DEFECT


def _absorb(
    report: DeliveryReport, scored: Report, *, disclosed: frozenset[str] = frozenset()
) -> None:
    for finding in scored.findings:
        if finding.outcome is Outcome.FAIL:
            report.items.append(
                Item(
                    _kind_for(finding.rule_id, disclosed),
                    f"{finding.rule_id}: {finding.message}",
                    " ".join(finding.examples[:2]) if finding.examples else "",
                    rule_id=finding.rule_id,
                    remedy=""
                    if _kind_for(finding.rule_id, disclosed) is not Kind.DEFECT
                    else "The file is wrong, not the site. Fix and regenerate before sending.",
                )
            )
        elif finding.outcome is Outcome.SKIPPED and finding.rule_id in EVIDENCE_RULES:
            report.items.append(
                Item(
                    Kind.LIMIT,
                    f"{finding.rule_id} could not be checked",
                    finding.reason or finding.message,
                    rule_id=finding.rule_id,
                )
            )


def _check_run(
    report: DeliveryReport,
    stats: dict,
    llms_txt: str,
    must_appear: set[str],
) -> None:
    """What only the run knows. No rule can see any of this.

    Each of these was recorded already and read by nobody. A stage that fell back
    is counted in `stats["llm"]["fallbacks"]`, the pages a non-JS crawler could not
    read are counted in `stats["fetch"]["js_only"]`, and the operator's named URLs
    are in the brief -- and a client asking "why is this page not in my file" had
    no answer assembled anywhere.
    """
    llm = stats.get("llm") or {}
    for fallback in llm.get("fallbacks") or []:
        stage = str(fallback).split(":", 1)[0]
        report.items.append(
            Item(
                Kind.LIMIT,
                f"The {stage} stage fell back to heuristics at least once",
                str(fallback)[:300],
                remedy="",
            )
        )

    fetch = stats.get("fetch") or {}
    js_only = fetch.get("js_only") or 0
    if js_only:
        urls = fetch.get("js_only_urls") or []
        report.items.append(
            Item(
                Kind.FINDING,
                f"{js_only} page(s) need JavaScript to read",
                "An AI crawler that does not run JavaScript sees almost nothing on "
                "these, and they are absent from the file for the same reason: "
                + ", ".join(urls[:3]),
            )
        )

    failed = fetch.get("failed") or 0
    requested = fetch.get("requested") or 0
    if failed and requested:
        report.items.append(
            Item(
                Kind.LIMIT,
                f"{failed} of {requested} pages did not fetch",
                "They are absent from the file. A handful is ordinary -- redirects "
                "and pages retired since the sitemap was written.",
            )
        )

    size_check = stats.get("size_check") or {}
    if size_check.get("reason") not in ("ok", None, ""):
        report.items.append(
            Item(
                Kind.LIMIT,
                "No indexed-page count was available",
                str(size_check.get("detail") or size_check.get("reason")),
            )
        )

    if must_appear and llms_txt:
        absent = sorted(url for url in must_appear if url not in llms_txt)
        if absent:
            report.items.append(
                Item(
                    Kind.DEFECT if len(absent) > len(must_appear) // 2 else Kind.FINDING,
                    f"{len(absent)} of {len(must_appear)} pages you named are not in the file",
                    ", ".join(absent[:5]) + (" ..." if len(absent) > 5 else ""),
                    remedy="Check each: a page in no sitemap was never discovered, "
                    "and a page needing JavaScript was never read.",
                )
            )
