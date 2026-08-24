"""Accounts: password hashing, and the rule that keeps a public instance closed.

The model is geo-tracker's local mode, which is what it actually runs in
production on Railway (`DEPLOYMENT_MODE=local`, public domain, no Auth0):

    exactly one self-service signup, then the instance is closed.

The first person to reach a fresh deployment registers and becomes the admin.
Every signup after that is refused. Further accounts exist, but only an existing
admin can create them. That is what makes a public URL safe before an identity
provider is wired up -- the window in which a stranger could claim the instance is
open only until the intended owner signs up, and it is the owner who is given the
link.

The refusal lives here, in the write path, not in a template. A POST straight at
`/signup` from curl gets exactly the same answer the UI does.

One deliberate difference from geo-tracker. Its `before` hook reads
`countUsers()` and then inserts, which is a check-then-act race: two concurrent
signups can both see zero and both succeed. The window is small and the damage is
one extra account, but it is avoidable, so `claim_instance` takes a Postgres
advisory lock and re-checks inside it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()

# Any 64-bit constant; it only has to be the same in every process that signs up.
SIGNUP_LOCK_KEY = 0x11B5_7C7A_0000_0001

MIN_PASSWORD_LENGTH = 12


class SignupClosed(RuntimeError):
    """Raised when someone tries to self-register on a bootstrapped instance."""


class WeakPassword(ValueError):
    pass


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. This is the only thing standing "
            "between a public URL and your OpenAI bill."
        )
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time check that never raises on a malformed or absent hash."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def is_bootstrapped(session: AsyncSession) -> bool:
    return await count_users(session) > 0


async def find_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.strip().lower()))
    return result.scalar_one_or_none()


async def claim_instance(session: AsyncSession, email: str, password: str, name: str = "") -> User:
    """Create the one and only self-service account.

    The advisory lock is held for the rest of the transaction, so a second
    concurrent signup blocks until this one commits and then loses the re-check
    rather than racing it.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SIGNUP_LOCK_KEY})

    if await is_bootstrapped(session):
        raise SignupClosed(
            "This instance already has an account. Sign in, or ask an admin to create one for you."
        )

    user = User(
        email=email.strip().lower(),
        name=name.strip(),
        password_hash=hash_password(password),
        is_admin=True,
        created_by="self",
    )
    session.add(user)
    await session.flush()
    logger.info("instance claimed by %s", user.email)
    return user


async def create_teammate(
    session: AsyncSession, admin: User, email: str, password: str, name: str = ""
) -> User:
    """The operator path. Deliberately not reachable without an admin session."""
    if not admin.is_admin:
        raise PermissionError("Only an admin can create accounts.")

    email = email.strip().lower()
    if await find_by_email(session, email) is not None:
        raise ValueError(f"{email} already has an account.")

    user = User(
        email=email,
        name=name.strip(),
        password_hash=hash_password(password),
        is_admin=False,
        created_by=admin.email,
    )
    session.add(user)
    await session.flush()
    logger.info("account for %s created by %s", user.email, admin.email)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Return the user for valid credentials, or None. Never says which half failed."""
    user = await find_by_email(session, email)
    if user is None or not user.is_active:
        # Burn roughly the same time as a real verification so that a missing
        # account is not distinguishable from a wrong password by timing alone.
        _hasher.hash("timing-equaliser")
        return None

    if not verify_password(user.password_hash, password):
        return None

    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = datetime.now(UTC)
    return user


async def resolve_sso(session: AsyncSession, email: str, name: str = "") -> User:
    """Find or create the account behind a verified Google identity.

    Google sign-in used to bypass this table entirely: `auth_callback` built a
    `User` straight from the ID token claims, and that dataclass defaults
    `is_admin` to False. So **an admin who signed in with Google was not an
    admin** -- /admin, /accounts and the client-delete Danger Zone were all
    unreachable -- and `is_active` was never consulted, so a deactivated person
    could still sign in through Google. Neither showed up while the password form
    was the main path.

    Provisioning on first sign-in rather than refusing is the same access the
    old code already granted; the difference is that the account now exists as a
    row, so it appears on /accounts and can be deactivated. The domain was
    already checked against the signed token before we get here, so this can only
    create accounts inside the allowed domains.

    The first Google identity into an unclaimed instance becomes the admin, which
    mirrors `claim_instance` and holds the same advisory lock, so two concurrent
    first sign-ins cannot both win.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SIGNUP_LOCK_KEY})

    address = email.strip().lower()
    user = await find_by_email(session, address)

    if user is not None:
        if not user.is_active:
            raise SignupClosed(f"{address} has been deactivated on this instance.")
        # A name from Google is fresher than one typed once at signup.
        if name.strip() and not user.name:
            user.name = name.strip()
        user.last_login_at = datetime.now(UTC)
        return user

    first = not await is_bootstrapped(session)
    user = User(
        email=address,
        name=name.strip(),
        # No password hash: this account signs in through Google. `verify_password`
        # already refuses a None hash, so it cannot be used on the password form.
        password_hash=None,
        is_admin=first,
        is_active=True,
        created_by="self" if first else "google-sso",
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    logger.info("provisioned %s from Google SSO (admin=%s)", user.email, first)
    return user


async def list_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).order_by(User.created_at))
    return list(result.scalars())
