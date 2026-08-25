"""Every route is gated, except the ones on this list.

This catches nothing today. Its whole value is tomorrow: `require_user` is the
entire authorisation layer in this app -- there is no owner column on any
client-scoped table -- so a route added without it hands every client's data to
anyone who finds the URL.

Before share links there was nothing unauthenticated that returned client data,
which made "is this route gated?" a question nobody had to ask. Now there is, and
the exception needs to be one reviewable list rather than a habit.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app

#: Public on purpose, each with the reason it is safe.
PUBLIC: dict[str, str] = {
    "/healthz": "liveness; returns the literal string ok",
    "/login": "the sign-in form itself",
    "/login/google": "redirects to Google",
    "/auth/callback": "the OAuth return leg",
    "/signup": "closed forever after the first account, enforced in the write path",
    "/logout": "clears a session",
    "/": "redirects; renders nothing client-specific",
    "/robots.txt": "static, names nothing",
    # The two that return client data without a session. The token is the
    # authorisation, and neither the domain nor the section comes from the URL.
    "/share/{token}": "share link; the token is the authorisation",
    "/share/{token}/download/{artifact}": "share link; scoped to the token's domain",
}

GATES = {"require_user", "require_admin", "require_admin_or_404"}


def _dependency_names(route: APIRoute) -> set[str]:
    return {
        dependency.call.__name__
        for dependency in route.dependant.dependencies
        if getattr(dependency, "call", None) is not None
    }


def test_every_route_is_gated_or_listed():
    ungated = [
        f"{sorted(route.methods)} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path not in PUBLIC
        and not (_dependency_names(route) & GATES)
    ]

    assert ungated == [], f"routes with no auth and no entry in PUBLIC: {ungated}"


def test_the_public_list_has_no_stale_entries():
    """A path removed from the app must be removed from the allowlist too.

    Otherwise the list slowly becomes a place where a future route's name is
    already blessed before anyone writes it.
    """
    live = {route.path for route in app.routes if isinstance(route, APIRoute)}

    assert not (set(PUBLIC) - live), f"listed but gone: {sorted(set(PUBLIC) - live)}"


def test_the_share_routes_take_no_user():
    """Not merely ungated -- they must not accept an identity at all.

    A `user` parameter would be an invitation to branch on it, and a share page
    that renders differently for staff is a share page nobody can check.
    """
    share_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/share/")
    ]
    assert share_routes

    for route in share_routes:
        names = {parameter.name for parameter in route.dependant.query_params}
        names |= {parameter.name for parameter in route.dependant.path_params}
        assert "user" not in names, route.path
        assert not (_dependency_names(route) & GATES), route.path


def test_no_write_route_is_public():
    """A public GET is a disclosure risk; a public POST is a control one.

    `/sites/{domain}/refresh` in particular performs a server-side fetch of an
    arbitrary host from a path parameter, and it must stay behind a session.
    """
    public_writes = [
        f"{sorted(route.methods)} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.methods & {"POST", "PUT", "PATCH", "DELETE"}
        and not (_dependency_names(route) & GATES)
        and route.path not in {"/login", "/signup"}
    ]

    assert public_writes == [], f"unauthenticated write routes: {public_writes}"
