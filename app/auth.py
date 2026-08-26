"""Two ways in, one session.

A password account is the primary path and is what makes a fresh deployment
usable before any identity provider exists -- the same choice geo-tracker makes,
which runs `DEPLOYMENT_MODE=local` on its public Railway domain today. Google
sign-in is layered on top and takes over as soon as a client is configured.

The account rules themselves live in `app/accounts.py`; this module is only
concerned with sessions and with who a request is.


For Google, the domain check reads `hd` (and falls back to the email) from the
**verified ID token**, not from the `hd` request parameter. That parameter is a UI hint sent by
the client: it changes which account chooser Google shows and it is trivially
removed from the authorize URL. Trusting it would let any Google account in. The
claim inside the signed token is the only trustworthy statement of which domain an
account belongs to.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import find_by_email
from app.config import Settings, get_settings
from app.db.base import get_session

logger = logging.getLogger(__name__)

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
SESSION_KEY = "user"


@dataclass(frozen=True, slots=True)
class User:
    """The signed-in identity, as carried in the session cookie.

    Not the database row: this is what a request has proven about itself. Routes
    that need the row load it by email.
    """

    email: str
    name: str = ""
    picture: str = ""
    is_admin: bool = False

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url=GOOGLE_METADATA,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def user_from_claims(claims: dict[str, Any], settings: Settings) -> User:
    """Validate the verified ID token's claims and build a user, or refuse."""
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Google returned no email address.")
    if not claims.get("email_verified", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That Google account is unverified.")

    allowed = settings.allowed_domains
    # `hd` is present for Workspace accounts; the email domain is the fallback and
    # is equally part of the signed token.
    hosted_domain = (claims.get("hd") or "").strip().lower()
    domain = hosted_domain or email.rsplit("@", 1)[-1]

    if allowed and domain not in allowed:
        logger.warning("Rejected sign-in from %s (domain %s)", email, domain)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{email} is not a {', '.join(sorted(allowed))} account.",
        )

    return User(
        email=email,
        name=(claims.get("name") or "").strip(),
        picture=(claims.get("picture") or "").strip(),
    )


def current_user(request: Request) -> User | None:
    data = request.session.get(SESSION_KEY)
    return User(**data) if isinstance(data, dict) else None


def sign_in(request: Request, user: User) -> None:
    """Put a signed-in user in the session cookie."""
    request.session[SESSION_KEY] = {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "is_admin": user.is_admin,
    }


def _signed_in(request: Request) -> User:
    """Whoever the cookie claims to be. Says nothing about their authority.

    Split out of `require_user` when that stopped being the whole answer. Kept
    separate because two things genuinely differ: whether there is a session at
    all (cheap, no I/O) and what that person is currently allowed to do.
    """
    settings = get_settings()
    if settings.allow_anonymous:
        return User(email="local@localhost", name="Local development", is_admin=True)

    user = current_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"Location": "/login"},
        )
    return user


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency. A session is always required; only the way in varies.

    Two ways to sign in, and the app supports both at once rather than switching
    on a mode flag: a password account, and -- once a Google client is configured
    -- Google. Whichever produced the session, what lands here is a session.

    **The cookie is an identity, not an authority.** `sign_in` copies `is_admin`
    into it and nothing ever read that back, so the flag was decided once, at
    sign-in, and then frozen for the life of the cookie:

    - Promoting somebody on /accounts did nothing until they signed out and back
      in, with nothing anywhere saying a re-login was needed.
    - Revoking admin, or deactivating an account outright, did not end the
      session it was revoked from. `resolve_sso` refuses a deactivated account at
      the *door*; a session already through the door kept working indefinitely.

    Both are fixed by resolving the row here rather than at the six privileged
    checks, because `user.is_admin` is also what decides whether a *template*
    draws the Admin nav group and the client Danger Zone. Fixing only
    `require_admin` would have left an admin who could delete a client by POSTing
    to the URL but could not see the button.

    The cost is one indexed lookup per authenticated request, on routes that
    mostly hold a session open already.

    The one exception is a machine with neither an identity provider nor a
    database-backed account, i.e. `pytest` and a bare `python -m app.web` with no
    migrations run. `ALLOW_ANONYMOUS=true` opts into that explicitly. It is
    refused in any https deployment by `Settings.assert_deployable`, so it cannot
    be the reason a public instance is open.
    """
    user = _signed_in(request)
    if get_settings().allow_anonymous:
        # No accounts table to consult, by definition.
        return user

    row = await find_by_email(session, user.email)
    if row is None or not row.is_active:
        # Deleted or deactivated since sign-in.
        logger.info("session for %s no longer resolves to an active account", user.email)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"Location": "/login"},
        )
    return User(email=row.email, name=row.name, picture=user.picture, is_admin=row.is_admin)


async def require_admin(account: User = Depends(require_user)) -> User:
    if not account.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can manage accounts.")
    return account


async def require_admin_or_404(account: User = Depends(require_user)) -> User:
    """Admin, or the page does not exist.

    404 rather than 403, copied from geo-tracker's `/admin` layout. A 403 confirms
    there is an admin area worth attacking; a 404 says nothing at all. The cost of
    the lie is that a genuine admin who has lost their flag sees a puzzling 404 --
    worth it for a surface that exposes spend and account management.
    """
    if not account.is_admin:
        logger.info("non-admin %s probed an admin route", account.email)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return account
