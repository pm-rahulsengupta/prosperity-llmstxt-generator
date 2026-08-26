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
    # The three that return client data without a session. The token is the
    # authorisation, and neither the domain nor the section comes from the URL.
    "/share/{token}": "share link; the token is the authorisation",
    "/share/{token}/download/{artifact}": "share link; scoped to the token's domain",
    "/share/{token}/pdf": "share link; the same page, printed",
}

GATES = {"require_user", "require_admin", "require_admin_or_404"}

#: Routes authenticated by a bearer token rather than by a session, each with the
#: reason a session would be the wrong control.
#:
#: A third category, not a hole in the second. The property both write tests
#: protect is "nothing unauthenticated writes" -- and these *are* authenticated,
#: just not by a cookie. `test_a_token_gated_route_actually_checks_its_token`
#: is what stops this list becoming a rubber stamp: adding a path here without
#: a constant-time comparison in its body fails.
TOKEN_GATED: dict[str, str] = {
    "/api/audits": "the LLM Access Checker pushes audits; it has no Google account",
}


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
        and route.path not in TOKEN_GATED
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
        and route.path not in TOKEN_GATED
    ]

    assert public_writes == [], f"unauthenticated write routes: {public_writes}"


def _endpoint_code(path: str) -> str:
    """The route's body with its docstring removed.

    Both tests below reason about the *order* of statements, and a docstring
    that names `compare_digest` while explaining the design would otherwise
    satisfy a check meant to find the call itself. Asserting on prose is how a
    guard passes on a function that does not have one.
    """
    import ast
    import inspect

    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == path)
    tree = ast.parse(inspect.cleandoc(inspect.getsource(route.endpoint)))
    function = tree.body[0]
    body = function.body[1:] if ast.get_docstring(function) else function.body
    return chr(10).join(ast.unparse(node) for node in body)


def test_a_token_gated_route_actually_checks_its_token():
    """What stops `TOKEN_GATED` becoming a way to bypass the two tests above.

    A path listed there has to compare a secret in constant time. `==` on a
    secret leaks its length and then its content to anyone who can time the
    response, so the presence of `compare_digest` is what is asserted.
    """
    for path in TOKEN_GATED:
        assert "compare_digest" in _endpoint_code(path), (
            f"{path} does not compare its token in constant time"
        )


def test_a_token_gated_route_fails_shut_when_unconfigured():
    """An unset secret must close the door, not open it.

    The dangerous shape is `if token and not matches: refuse`, which accepts
    everything on an instance where nobody set the secret. The refusal has to
    come *before* the comparison, which is what the ordering here pins -- the
    failure only appears on a misconfigured deployment, which is exactly where
    nobody is looking.
    """
    for path in TOKEN_GATED:
        code = _endpoint_code(path)
        guard = code.find("raise HTTPException")
        comparison = code.find("compare_digest")

        assert 0 <= guard < comparison, (
            f"{path} compares a token before refusing an unconfigured instance"
        )
