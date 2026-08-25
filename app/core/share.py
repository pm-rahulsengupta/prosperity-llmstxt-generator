"""Tokens for client share links.

Pure: no database, no FastAPI. The rules about what a token *is* live here so
they can be tested without either.

**Opaque and random, not signed.** An `itsdangerous` token carrying
`{domain, section, expiry}` would need no table -- and could not be revoked. The
only way to kill one would be rotating `SESSION_SECRET`, which signs every staff
member out. Adding a denylist to fix that costs the table anyway, and loses what
a row gives for free: the list of live links for a client, who created each one,
whether the client has opened it, and removal when the client is deleted. A
signed token also still *authorises* a lookup for a client deleted last month;
with a row, deletion removes the authority.

**Stored as a SHA-256 digest, never in the clear, and deliberately not argon2**
even though argon2 is already a dependency for passwords. Two reasons, and the
second is the one that bites:

* Argon2's cost parameter exists to slow brute force against secrets a human
  chose. There is nothing to brute force in 256 random bits -- at ten thousand
  guesses a second against ten thousand live links, the expected time to find
  one is longer than the universe has existed.
* Argon2 hashes are salted, so they cannot be looked up by index. Verifying a
  presented token would mean loading every live row and running a KDF against
  each: one page view becomes O(live links) x 50ms of deliberate CPU. That is a
  self-inflicted denial of service wearing a security control's clothes.

So the hash is not defending against guessing. It defends against *disclosure* --
a database dump, a read replica handed to an analyst, a `SELECT *` pasted into
Slack, a backup on a laptop. None of those should contain live credentials.

The consequence has to be designed for rather than discovered: **the plaintext
token exists exactly once**, in the response that creates it. There is no way to
show it again, and any UI that appears to means someone stored it. A lost link is
revoked and reissued.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from enum import StrEnum

__all__ = [
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "TOKEN_CHARS",
    "ShareSection",
    "matches",
    "new_token",
    "share_url",
    "token_hash",
]

#: 256 bits. See the module docstring for why no throttle is needed on top.
TOKEN_BYTES = 32
#: What `token_urlsafe(32)` produces. Pinned by a test and used to constrain the
#: route's path parameter, so malformed input is refused before a query runs.
TOKEN_CHARS = 43

DEFAULT_DAYS = 30
MAX_DAYS = 90


class ShareSection(StrEnum):
    """What a token authorises. One section, chosen when the link is made.

    Mirrors `client_report.SECTION_KEYS`; a test asserts the two stay equal. They
    are separate because this one is authorisation-bearing and lives in the
    database as a CHECK constraint, while that one is a rendering concern.
    """

    OVERVIEW = "overview"
    CHECKLIST = "checklist"
    HANDOVER = "handover"
    CRAWL = "crawl"
    CONTENT = "content"
    AGENTS = "agents"
    CAPABILITIES = "capabilities"
    DELIVERY = "delivery"
    PAGE = "page"
    REPORT = "report"


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def matches(stored: str, presented: str) -> bool:
    """Compare a stored digest against a presented token.

    `hmac.compare_digest` is not here for timing. The comparison that actually
    finds the row happens inside Postgres on a B-tree index and is not constant
    time, and it does not need to be: what leaks is how many leading bytes of a
    *digest* matched, and turning that into a token means finding SHA-256
    preimages, which is far harder than guessing the token outright.

    It is here as a structural assertion. It survives someone later changing the
    query to `ilike`, or a collation change, or a well-meant `startswith` fix --
    the class of edit that silently loosens a credential match and that no other
    test would catch.
    """
    return hmac.compare_digest(stored, token_hash(presented))


def share_url(app_url: str, token: str) -> str:
    return f"{app_url.rstrip('/')}/share/{token}"
