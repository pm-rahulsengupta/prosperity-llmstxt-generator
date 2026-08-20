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
from fastapi import HTTPException, Request, status

from app.config import Settings, get_settings

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


def require_user(request: Request) -> User:
    """FastAPI dependency. A session is always required; only the way in varies.

    Two ways to sign in, and the app supports both at once rather than switching
    on a mode flag: a password account, and -- once a Google client is configured
    -- Google. Whichever produced the session, what lands here is a session.

    The one exception is a machine with neither an identity provider nor a
    database-backed account, i.e. `pytest` and a bare `python -m app.web` with no
    migrations run. `ALLOW_ANONYMOUS=true` opts into that explicitly. It is
    refused in any https deployment by `Settings.assert_deployable`, so it cannot
    be the reason a public instance is open.
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


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can manage accounts.")
    return user
