"""Authentication endpoints: settable password + login sessions.

Replaces the ephemeral ``SCANHOUND_AUTH_NONCE`` (regenerated on every restart,
printed to stdout, consumed once by the desktop Tauri sidecar) with a password
persisted in the DB, so browser / self-hosted deployments get a stable
credential. The nonce path stays intact for the sidecar; the bearer-token
middleware in ``backend.api.main`` accepts either a valid session token or the
nonce.

``/auth/login`` and ``/auth/status`` are auth-exempt (see ``_AUTH_EXEMPT_PATHS``
in main) so the login page can reach them before holding any token.
"""
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies import (
    ServiceRegistry, get_registry, allow_open, credential_state,
    issue_ws_ticket, ws_ticket_ttl_seconds, RECOVERY_LOCKED,
)
from backend import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_MIN_PASSWORD_LEN = 8

# ── Login rate limiting ──────────────────────────────────────────────────
# bcrypt's cost slows a single guess, but nothing stopped unlimited parallel
# attempts. Cap failed attempts per client IP in a sliding window; successful
# logins clear the counter. In-memory is sufficient for this single-process,
# self-hosted tool.
_RATE_WINDOW_S = 300.0   # 5-minute window
_RATE_MAX_FAILS = 10     # then lock that IP out for the rest of the window
_login_fails: Dict[str, Deque[float]] = defaultdict(deque)
_login_fails_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    """Whether ``ip`` has exhausted its failed-login budget for the window."""
    now = time.monotonic()
    with _login_fails_lock:
        fails = _login_fails[ip]
        while fails and now - fails[0] > _RATE_WINDOW_S:
            fails.popleft()
        return len(fails) >= _RATE_MAX_FAILS


def _record_login_fail(ip: str) -> None:
    with _login_fails_lock:
        _login_fails[ip].append(time.monotonic())


def _clear_login_fails(ip: str) -> None:
    with _login_fails_lock:
        _login_fails.pop(ip, None)


class LoginRequest(BaseModel):
    password: str


class SetPasswordRequest(BaseModel):
    new_password: str
    current_password: Optional[str] = None


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


@router.get("/status")
def auth_status(reg: ServiceRegistry = Depends(get_registry)):
    """Report whether auth is required so the frontend can decide to show login.

    Leaks nothing sensitive — only whether a password / nonce gate is active.
    """
    has_password = bool(reg.db and reg.db.has_password())
    nonce_active = bool(reg.auth_nonce)
    # True only in the fresh-install / wiped-credential state where the HTTP
    # and WS layers fail CLOSED (SH-H01) despite auth_required being False —
    # the frontend must show the set-password prompt instead of trusting
    # auth_required, whose protected fetches would 401 anyway. Not set when
    # SCANHOUND_ALLOW_OPEN=1 (that no-credential state is an intentional
    # escape hatch, not a fresh install needing a prompt).
    state = credential_state(reg.db) if reg.db else "absent"
    recovery_locked = state == RECOVERY_LOCKED
    # NOT a setup prompt when recovery-locked: showing "create a password"
    # invites the very request the gate refuses, so the operator would see a
    # form that always fails. The frontend needs the distinct signal.
    setup_required = (not has_password and not nonce_active
                      and not allow_open() and not recovery_locked)
    return {
        "auth_required": has_password or nonce_active,
        "has_password": has_password,
        "nonce_active": nonce_active,
        "setup_required": setup_required,
        # Distinct from setup_required on purpose: the operator must be told
        # the install is locked pending recovery, not invited to create a
        # password the gate will refuse.
        "recovery_locked": recovery_locked,
    }


@router.post("/login")
def login(body: LoginRequest, request: Request,
          reg: ServiceRegistry = Depends(get_registry)):
    """Verify the password and issue a long-lived session token."""
    if not reg.db or not reg.db.has_password():
        raise HTTPException(status_code=400, detail="No password is configured")
    ip = _client_ip(request)
    if _rate_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts; try again later")
    stored = reg.db.get_password_hash()
    if not auth_service.verify_password(body.password, stored):
        _record_login_fail(ip)
        # bcrypt's own cost is the brute-force deterrent; keep the message vague.
        raise HTTPException(status_code=401, detail="Incorrect password")
    _clear_login_fails(ip)
    token = auth_service.new_session_token()
    expires_at = auth_service.session_expiry()
    if not reg.db.create_session(auth_service.hash_token(token), expires_at):
        # create_session is a _mutate: it returns False on a write failure
        # rather than raising. Returning the token anyway hands the client a
        # credential that 401s on every later request, which the frontend
        # renders as an endless login loop with the correct password.
        raise HTTPException(
            status_code=500,
            detail="Could not persist the login session; "
                   "the database is not writable")
    reg.db.purge_expired_sessions(auth_service.now_iso())  # opportunistic cleanup
    return {"token": token, "expires_at": expires_at}


@router.post("/set-password")
def set_password(body: SetPasswordRequest,
                 reg: ServiceRegistry = Depends(get_registry)):
    """Set or change the admin password; revokes all existing sessions.

    Reaching this route already means the middleware authorized the caller
    (open install, valid session, or the desktop nonce). Changing an existing
    password additionally requires the current one.
    """
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database unavailable")
    new_password = body.new_password or ""
    if len(new_password) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {_MIN_PASSWORD_LEN} characters")
    state = credential_state(reg.db)
    if state == RECOVERY_LOCKED:
        # The database was auto-quarantined and rebuilt empty, so it honestly
        # reports no credential -- but this installation HAS been initialised.
        # Letting the bootstrap path run here hands admin to whoever asks
        # first. Recovery needs out-of-band proof, and the message says which
        # kinds, because a lockout nobody can explain is its own outage.
        logger.error(
            "set_password refused: database was quarantined (marker beside "
            "%s). Recovery requires the desktop nonce or removing the marker "
            "on the host.", getattr(reg.db, "db_path", "the database"))
        raise HTTPException(
            status_code=409,
            detail="This database was automatically rebuilt after corruption "
                   "was detected, so it has no password even though this "
                   "install previously had one. To prevent an unauthenticated "
                   "takeover, setting a password here is blocked. Recover by "
                   "starting with the desktop nonce, or by removing the "
                   "'.corrupt_flag.json' / '.corrupt_flag.notified.json' file "
                   "next to the database on the host once you are satisfied "
                   "the machine is yours.")
    if state == "unknown":
        # A failed credential read must never be mistaken for "no password
        # configured": that answer both un-gates this route at the middleware
        # (bootstrap exemption) and skips the current-password check below,
        # which together allow an unauthenticated password takeover.
        raise HTTPException(
            status_code=503,
            detail="Could not read the stored credential; password unchanged")
    if state == "present":
        stored = reg.db.get_password_hash()
        if not auth_service.verify_password(body.current_password or "", stored):
            raise HTTPException(
                status_code=401, detail="Current password is incorrect")
    if not reg.db.set_password_hash(auth_service.hash_password(new_password)):
        # Bailing out before touching auth_sessions keeps a failed write a
        # clean no-op instead of "signed out everywhere, old password live".
        raise HTTPException(
            status_code=500,
            detail="Could not save the new password; it is unchanged")
    if not reg.db.delete_all_sessions():  # force re-login everywhere
        # The password DID change, so the caller must be told both facts: use
        # the new password, and previously issued tokens are still valid.
        raise HTTPException(
            status_code=500,
            detail="Password changed, but existing sessions could not be "
                   "revoked. Sign out all devices manually and retry.")
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, reg: ServiceRegistry = Depends(get_registry)):
    """Invalidate the caller's current session token (no-op for the nonce)."""
    token = _bearer(request)
    if reg.db and token:
        if not reg.db.delete_session(auth_service.hash_token(token)):
            # delete_session is a _mutate: False means the DELETE never landed,
            # so the row — and the token — survive for the rest of the 30-day
            # TTL while the UI shows a clean sign-out. A zero-row DELETE still
            # returns True, so this distinguishes "write failed" from success,
            # NOT "row existed"; the nonce path has no row and keeps 200ing.
            logger.error(
                "logout: session delete failed; token remains valid until expiry")
            raise HTTPException(
                status_code=500,
                detail="Sign-out could not be completed on the server; "
                       "the session may still be active")
    return {"ok": True}


@router.post("/ws-ticket")
def ws_ticket(request: Request, reg: ServiceRegistry = Depends(get_registry)):
    """Mint a short-lived, single-use ticket for the WebSocket handshake.

    Reaching this route already means the middleware authorized the caller.
    The ticket exists because a browser cannot set an Authorization header on
    a WebSocket, so the session token would otherwise ride in the URL, where
    every proxy in front of this app logs it verbatim.
    """
    credential = _bearer(request)
    if not credential:
        # Open mode (SCANHOUND_ALLOW_OPEN=1) has no credential to stand in
        # for, and the socket already accepts a tokenless handshake there.
        raise HTTPException(status_code=401, detail="Bearer token required")
    return {"ticket": issue_ws_ticket(credential),
            "expires_in": ws_ticket_ttl_seconds()}
