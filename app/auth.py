"""Google sign-in, restricted to Prosperity accounts.

The domain check reads `hd` (and falls back to the email) from the **verified ID
token**, not from the `hd` request parameter. That parameter is a UI hint sent by
the client: it changes which account chooser Google shows and it is trivially
removed from the authorize URL. Trusting it would let any Google account in. The
claim inside the signed token is the only trustworthy statement of which domain an
account belongs to.

With no client ID configured the app runs open, and says so on every page. That is
right for local development and would be wrong in deploy, which is why
`Settings.assert_deployable` refuses to boot an https deployment without it.
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
    email: str
    name: str = ""
    picture: str = ""

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


def require_user(request: Request) -> User:
    """FastAPI dependency. Open when SSO is unconfigured, enforced when it is."""
    settings = get_settings()
    if not settings.sso_enabled:
        return User(email="local@localhost", name="Local development")

    user = current_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"Location": "/login"},
        )
    return user
