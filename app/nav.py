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

from app.core.components import FAMILY_LABELS

__all__ = ["NavGroup", "NavItem", "build_nav"]


@dataclass(frozen=True, slots=True)
class NavItem:
    title: str
    url: str
    active: bool = False
    disabled: bool = False
    hint: str = ""


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


def build_nav(path: str, domain: str = "", is_admin: bool = False) -> list[NavGroup]:
    """Build the sidebar for one request."""
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
                NavItem("All clients", "/clients", active=path.rstrip("/") == "/clients"),
                NavItem(
                    "Add a client",
                    "/clients/new",
                    active=_active(path, "/clients/new"),
                ),
                NavItem(
                    "Overview",
                    site_url(""),
                    # Exact match only. `/sites/{d}` is a prefix of every scoped
                    # page, so a prefix match here would light Overview on the
                    # checklist, the handover and all six family tabs at once.
                    active=scoped and path.rstrip("/") == f"/sites/{domain}",
                    disabled=not scoped,
                    hint=site_hint,
                ),
            ],
        ),
        NavGroup(
            label="Generate",
            items=[
                # `/runs/...` lights this too: a run is the output of the
                # llms.txt flow, and leaving the whole sidebar dark on the page an
                # operator spends most of their time on is a worse answer than
                # naming the section it belongs to.
                NavItem(
                    "llms.txt",
                    "/",
                    active=_active(path, "/") or _active(path, "/runs"),
                ),
                NavItem("agents.md", "/agents", active=_active(path, "/agents")),
            ],
        ),
        NavGroup(
            label="Files",
            items=[
                NavItem(
                    label,
                    site_url(f"/family/{family.value}"),
                    active=scoped and _active(path, f"/sites/{domain}/family/{family.value}"),
                    disabled=not scoped,
                    hint=site_hint,
                )
                for family, label in FAMILY_LABELS.items()
            ],
        ),
        NavGroup(
            label="Actions",
            items=[
                NavItem(
                    "Your checklist",
                    site_url("/checklist"),
                    active=scoped and _active(path, f"/sites/{domain}/checklist"),
                    disabled=not scoped,
                    hint=site_hint,
                ),
                NavItem(
                    "Developer handover",
                    site_url("/handover"),
                    active=scoped and _active(path, f"/sites/{domain}/handover"),
                    disabled=not scoped,
                    hint=site_hint,
                ),
            ],
        ),
        NavGroup(
            label="Site",
            items=[
                NavItem(
                    "Brief",
                    site_url("/brief"),
                    active=scoped and _active(path, f"/sites/{domain}/brief"),
                    disabled=not scoped,
                    hint=site_hint,
                ),
                # "Search data" used to sit here pointing at the same URL as Brief
                # with `active=False` hardcoded, so it could never light and
                # clicking it highlighted Brief instead. It was a signpost to a
                # panel further down that page. It now links to the panel.
                NavItem(
                    "Search data",
                    site_url("/brief#search-console"),
                    active=False,
                    disabled=not scoped,
                    hint=site_hint or "Upload or fetch Search Console data",
                ),
                NavItem(
                    "Settings",
                    site_url("/settings"),
                    active=scoped and _active(path, f"/sites/{domain}/settings"),
                    disabled=not scoped,
                    hint=site_hint,
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
                    NavItem("Costs", "/admin", active=path == "/admin"),
                    NavItem("Runs", "/admin/runs", active=_active(path, "/admin/runs")),
                    NavItem("Accounts", "/accounts", active=_active(path, "/accounts")),
                ],
            )
        )

    return groups
