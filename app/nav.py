"""The left-hand navigation, as data.

Ported from GEO Tracker's `app-sidebar.tsx`, which builds a `NavGroup[]` and hands
it to a dumb renderer. That app is React and this one is Jinja, so the pattern
travels rather than the code: groups in, active state derived from the path, and a
template that only draws what it is given.

Two behaviours are copied deliberately rather than reinvented.

Items are shown even when they cannot be used yet. GEO Tracker keeps its Team page
visible and lets the page itself explain the requirement, on the grounds that a
visible item that says what it needs beats a missing one nobody can find. Here the
site-scoped items behave the same way with no domain selected.

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
    disabled: bool = False
    hint: str = ""
    # Names an icon in `partials/icons.html`. Empty renders the fallback dot
    # rather than nothing, so a typo shows as a wrong icon instead of an item
    # whose label sits half a gutter left of every sibling.
    icon: str = ""
    # How many applicable components in this group are not yet live.
    #
    # `None` means not measured -- no client selected, or no probe stored -- and
    # renders nothing. `0` means measured and clear, and renders a tick. The
    # distinction is the same one the probes draw everywhere else: a family with
    # no data must not read as a family with no problems.
    gap: int | None = None


@dataclass(frozen=True, slots=True)
class NavGroup:
    label: str
    items: list[NavItem] = field(default_factory=list)


def _active(path: str, url: str) -> bool:
    """Whether `url` is the section the current path sits in.

    The index is matched exactly. Everything else is a prefix match, so a run
    detail page keeps "Runs" lit rather than nothing.
    """
    if url == "/":
        return path in ("/", "")
    return path == url or path.startswith(url.rstrip("/") + "/")


def build_nav(
    path: str,
    domain: str = "",
    is_admin: bool = False,
    gaps: dict[str, int] | None = None,
) -> list[NavGroup]:
    """Build the sidebar for one request.

    `gaps` is outstanding work, keyed "checklist" and "handover".

    It used to be per-family, which put a call to act on six navigational tabs
    and on neither of the two pages where acting happens. It also counted
    everything not yet live, including templates nobody can action until a
    service exists.
    Routes that have derived a `SiteStatus` pass it and the sidebar answers
    "where is the work" without opening a tab; routes that have not pass nothing
    and it stays a plain menu. Passing a wrong-but-plausible zero would be worse
    than passing nothing, which is why the parameter is optional rather than
    defaulted to an empty dict.
    """
    scoped = bool(domain)
    site_hint = "" if scoped else "Pick a client first"

    def site_url(suffix: str) -> str:
        # Without a domain these point at the client list. That list is new: this
        # function previously said it pointed at "the picker" and there was no
        # picker, so every disabled item sent an operator to the run starter.
        return f"/sites/{domain}{suffix}" if scoped else "/clients"

    groups = [
        NavGroup(
            label="Clients",
            items=[
                # Exact, not prefix: `/clients` is a prefix of `/clients/new`,
                # which lit both and left the sidebar saying it did not know
                # where the operator was.
                NavItem(
                    "All clients", "/clients", active=path.rstrip("/") == "/clients", icon="clients"
                ),
                NavItem(
                    "Add a client",
                    "/clients/new",
                    active=_active(path, "/clients/new"),
                    icon="add",
                ),
                NavItem(
                    "Check any site",
                    "/agents",
                    active=_active(path, "/agents"),
                    icon="check-site",
                ),
                # The crawl that feeds the Content family. It is an input, not a
                # file, which is why it is no longer called "llms.txt".
                NavItem(
                    "Crawl runs",
                    "/",
                    active=_active(path, "/") or _active(path, "/runs"),
                    icon="runs",
                ),
                # The fallback when a site refuses our crawler. Sits beside the
                # crawl rather than hidden in a settings page, because the moment
                # you need it is the moment a crawl just failed.
                NavItem(
                    "Import a crawl",
                    "/imports/screaming-frog",
                    active=_active(path, "/imports"),
                    icon="import",
                ),
            ],
        ),
        NavGroup(
            label="Discovery",
            items=[
                NavItem(
                    "Overview",
                    site_url(""),
                    # Exact match only. `/sites/{d}` is a prefix of every scoped
                    # page, so a prefix match would light Overview on the
                    # checklist, the handover and all six family tabs at once.
                    active=scoped and path.rstrip("/") == f"/sites/{domain}",
                    disabled=not scoped,
                    hint=site_hint,
                    icon="overview",
                ),
                *(
                    NavItem(
                        label,
                        site_url(f"/family/{family.value}"),
                        active=scoped and _active(path, f"/sites/{domain}/family/{family.value}"),
                        disabled=not scoped,
                        hint=site_hint,
                        icon=FAMILY_ICONS.get(family, ""),
                    )
                    for family, label in FAMILY_LABELS.items()
                ),
            ],
        ),
        NavGroup(
            label="Deliverables",
            items=[
                # The badges live here, not on the six families.
                #
                # A badge is a call to act, so it belongs on the page where the
                # acting happens. On a family it answered a question nobody was
                # asking -- "how many Delivery items are unpublished" -- and it
                # counted *everything* not yet live, including templates that
                # cannot be acted on until a service exists. These two count
                # outstanding work, which is what a number beside a link should
                # mean.
                NavItem(
                    "Your checklist",
                    site_url("/checklist"),
                    active=scoped and _active(path, f"/sites/{domain}/checklist"),
                    disabled=not scoped,
                    hint=site_hint,
                    icon="checklist",
                    gap=(gaps or {}).get("checklist"),
                ),
                NavItem(
                    "Developer handover",
                    site_url("/handover"),
                    active=scoped and _active(path, f"/sites/{domain}/handover"),
                    disabled=not scoped,
                    hint=site_hint,
                    icon="handover",
                    gap=(gaps or {}).get("handover"),
                ),
            ],
        ),
        NavGroup(
            label="Site data",
            items=[
                NavItem(
                    "Brief",
                    site_url("/brief"),
                    active=scoped and _active(path, f"/sites/{domain}/brief"),
                    disabled=not scoped,
                    hint=site_hint,
                    icon="brief",
                ),
                # "Search data" used to sit here pointing at the same URL as Brief
                # with `active=False` hardcoded, so it could never light and
                # clicking it highlighted Brief instead. It was a signpost to a
                # panel further down that page. It now links to the panel.
                NavItem(
                    "Search data",
                    site_url("/brief#search-data"),
                    active=False,
                    disabled=not scoped,
                    hint=site_hint or "Upload or fetch Search Console data",
                    icon="search-data",
                ),
                NavItem(
                    "Settings",
                    site_url("/settings"),
                    active=scoped and _active(path, f"/sites/{domain}/settings"),
                    disabled=not scoped,
                    hint=site_hint,
                    icon="settings",
                ),
            ],
        ),
    ]

    if is_admin:
        # Mirrors the existing `/admin` gate. `require_admin_or_404` already hides
        # the pages themselves; this hides the signposts so a non-admin is not
        # shown doors that 404.
        groups.append(
            NavGroup(
                label="Admin",
                items=[
                    NavItem("Costs", "/admin", active=path == "/admin", icon="costs"),
                    NavItem(
                        "All runs",
                        "/admin/runs",
                        active=_active(path, "/admin/runs"),
                        icon="all-runs",
                    ),
                    NavItem(
                        "Accounts", "/accounts", active=_active(path, "/accounts"), icon="accounts"
                    ),
                ],
            )
        )

    return groups
