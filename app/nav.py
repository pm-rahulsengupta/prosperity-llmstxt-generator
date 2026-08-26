"""The left-hand navigation, as data.

Ported from GEO Tracker's `app-sidebar.tsx`, which builds a `NavGroup[]` and hands
it to a dumb renderer. That app is React and this one is Jinja, so the pattern
travels rather than the code: groups in, active state derived from the path, and a
template that only draws what it is given.

**The rail is contextual.** With no client selected it carries the two things you
can do without one -- open the client list, or check a site you have not
onboarded. Pick a client and it becomes that client's sections, with a way back
out.

It used to show every item at all times, with the twelve site-scoped ones greyed
out and titled "Pick a client first". That was a deliberate borrow from GEO
Tracker, which keeps its Team page visible on the grounds that a visible item
explaining what it needs beats a missing one nobody can find. The borrow did not
survive contact: GEO Tracker disables *one* item, and twelve of them is a wall of
dead text that reads as a broken page rather than as an instruction. The first
thing an operator meets on a fresh instance is now two live links.

Active state is prefix-matched with the index special-cased, because `/` is a prefix
of everything and would otherwise light up on every page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.components import FAMILY_LABELS, Family

FAMILY_ICONS: dict[Family, str] = {
    Family.CRAWL: "crawl",
    Family.CONTENT: "content",
    Family.AGENTS: "agents",
    Family.CAPABILITIES: "capabilities",
    Family.DELIVERY: "delivery",
    Family.PAGE: "page",
}

__all__ = ["NavGroup", "NavItem", "build_nav"]


@dataclass(frozen=True, slots=True)
class NavItem:
    title: str
    url: str
    active: bool = False
    hint: str = ""
    # Names an icon in `partials/icons.html`. Empty renders the fallback dot
    # rather than nothing, so a typo shows as a wrong icon instead of an item
    # whose label sits half a gutter left of every sibling.
    icon: str = ""
    # How many items on this list are not yet published.
    #
    # `None` means not measured -- no probe stored -- and renders nothing. `0`
    # means measured and clear, and renders a tick. The distinction is the same
    # one the probes draw everywhere else: a list with no data must not read as
    # a list with no problems.
    gap: int | None = None
    # Renders as a way out of the current context rather than as a destination.
    back: bool = False


@dataclass(frozen=True, slots=True)
class NavGroup:
    label: str
    items: list[NavItem] = field(default_factory=list)
    # Whether `label` is a name rather than a category. Group labels are set as
    # tiny uppercase eyebrows, which is right for "Detail" and "Admin" and
    # unreadable for a domain.
    is_name: bool = False


def _active(path: str, url: str) -> bool:
    """Whether `url` is the section the current path sits in.

    The index is matched exactly. Everything else is a prefix match, so a run
    detail page keeps its section lit rather than nothing.
    """
    if url == "/":
        return path in ("/", "")
    return path == url or path.startswith(url.rstrip("/") + "/")


def _admin_group(path: str) -> NavGroup:
    """Mirrors the existing `/admin` gate.

    `require_admin_or_404` already hides the pages themselves; this hides the
    signposts so a non-admin is not shown doors that 404.
    """
    return NavGroup(
        label="Admin",
        items=[
            NavItem("Costs", "/admin", active=path == "/admin", icon="costs"),
            NavItem(
                "All runs", "/admin/runs", active=_active(path, "/admin/runs"), icon="all-runs"
            ),
            NavItem("Accounts", "/accounts", active=_active(path, "/accounts"), icon="accounts"),
        ],
    )


def _unscoped(path: str) -> list[NavGroup]:
    """The rail with no client selected: the two things you can do without one.

    "Add a client", "Crawl runs" and "Import a crawl" were here too. They are
    actions on the client list rather than places, so they moved onto `/clients`
    as buttons -- which is also where an operator is standing when they want one.

    Prefix-matching `/clients` keeps this lit on `/clients/new`, which is a page
    within that section. It was an exact match before, because with a separate
    "Add a client" item a prefix match lit both and left the rail saying it did
    not know where the operator was. That item is gone, so the reason is too.
    """
    return [
        NavGroup(
            label="Clients",
            items=[
                NavItem(
                    "All clients",
                    "/clients",
                    active=_active(path, "/clients"),
                    icon="clients",
                ),
                NavItem(
                    "Check any site",
                    "/agents",
                    active=_active(path, "/agents"),
                    icon="check-site",
                ),
            ],
        )
    ]


def _scoped(path: str, domain: str, gaps: dict[str, int]) -> list[NavGroup]:
    """The rail inside a client.

    Ordered by what an operator does rather than by how the registry is
    structured: the two lists that are the workflow first, under the client's own
    name, then the six families as detail, then the inputs.
    """

    def url(suffix: str) -> str:
        return f"/sites/{domain}{suffix}"

    def here(suffix: str) -> bool:
        return _active(path, url(suffix))

    return [
        NavGroup(
            label="",
            items=[NavItem("All clients", "/clients", icon="back", back=True)],
        ),
        NavGroup(
            # The client's own name, so the rail says which one you are in. Every
            # scoped page looked identical in the sidebar before.
            label=domain,
            is_name=True,
            items=[
                NavItem(
                    "Overview",
                    url(""),
                    # Exact match only. `/sites/{d}` is a prefix of every scoped
                    # page, so a prefix match would light Overview on the
                    # checklist, the handover and all six family tabs at once.
                    active=path.rstrip("/") == f"/sites/{domain}",
                    icon="overview",
                ),
                # The badges live on the two lists, not on the six families.
                #
                # A badge is a call to act, so it belongs on the page where the
                # acting happens. On a family it answered a question nobody was
                # asking -- "how many Delivery items are unpublished" -- and it
                # counted *everything* not yet live, including templates that
                # cannot be actioned until a service exists.
                NavItem(
                    "Your checklist",
                    url("/checklist"),
                    active=here("/checklist"),
                    icon="checklist",
                    gap=gaps.get("checklist"),
                ),
                NavItem(
                    "Developer handover",
                    url("/handover"),
                    active=here("/handover"),
                    icon="handover",
                    gap=gaps.get("handover"),
                ),
            ],
        ),
        NavGroup(
            label="Detail",
            items=[
                NavItem(
                    label,
                    url(f"/family/{family.value}"),
                    active=here(f"/family/{family.value}"),
                    icon=FAMILY_ICONS.get(family, ""),
                )
                for family, label in FAMILY_LABELS.items()
            ],
        ),
        NavGroup(
            label="Site data",
            items=[
                NavItem("Brief", url("/brief"), active=here("/brief"), icon="brief"),
                # "Search data" used to point at the same URL as Brief with
                # `active=False` hardcoded, so it could never light and clicking
                # it highlighted Brief instead. It is a signpost to a panel
                # further down that page, and now links to the panel.
                NavItem(
                    "Search data",
                    url("/brief#search-data"),
                    hint="Upload or fetch Search Console data",
                    icon="search-data",
                ),
                NavItem("Settings", url("/settings"), active=here("/settings"), icon="settings"),
            ],
        ),
    ]


def build_nav(
    path: str,
    domain: str = "",
    is_admin: bool = False,
    gaps: dict[str, int] | None = None,
) -> list[NavGroup]:
    """Build the sidebar for one request.

    `gaps` is outstanding work, keyed "checklist" and "handover".

    Routes that have derived a `SiteStatus` pass it and the sidebar answers
    "where is the work" without opening a tab; routes that have not pass nothing
    and it stays a plain menu. Passing a wrong-but-plausible zero would be worse
    than passing nothing, which is why the parameter is optional rather than
    defaulted to an empty dict.
    """
    groups = _scoped(path, domain, gaps or {}) if domain else _unscoped(path)
    if is_admin:
        groups.append(_admin_group(path))
    return groups
