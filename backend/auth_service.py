"""Password hashing and session-token helpers for the API auth layer.

Kept separate from the FastAPI route so the crypto is unit-testable without
spinning up the app. Passwords are bcrypt-hashed (SHA-256 pre-hashed first so
the full password stays significant past bcrypt's 72-byte limit); session
tokens are opaque high-entropy strings persisted only as SHA-256 hashes, so a
database leak never exposes a live token.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import secrets

import bcrypt

# Long-lived browser sessions — this is a self-hosted, single-admin tool, so
# re-login friction matters more than aggressive rotation. Changing the
# password revokes every existing session regardless of TTL.
SESSION_TTL_DAYS = 30
_TOKEN_BYTES = 32


def _prehash(password: str) -> bytes:
    """SHA-256 + base64 a password before bcrypt.

    bcrypt silently truncates at 72 bytes and stops at the first NUL; pre-
    hashing keeps the entire password significant. The base64 of a 32-byte
    digest is 44 bytes, comfortably under bcrypt's limit. (Same idea as
    passlib's ``bcrypt_sha256``.)
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a bcrypt hash (utf-8 text) for a plaintext password."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a stored hash."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_token(token: str) -> str:
    """SHA-256 hex of a session token — what we persist and look up by."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """Generate an opaque, URL-safe session token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def session_expiry(ttl_days: int = SESSION_TTL_DAYS,
                   now: _dt.datetime | None = None) -> str:
    """ISO-8601 UTC expiry timestamp for a new session."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now + _dt.timedelta(days=ttl_days)).isoformat()


def is_expired(expires_at: str, now: _dt.datetime | None = None) -> bool:
    """Whether an ISO-8601 expiry timestamp is at or before ``now``."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        exp = _dt.datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=_dt.timezone.utc)
    return exp <= now
