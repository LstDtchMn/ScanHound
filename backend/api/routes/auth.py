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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_MIN_PASSWORD_LEN = 8


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
    return {
        "auth_required": has_password or nonce_active,
        "has_password": has_password,
        "nonce_active": nonce_active,
    }


@router.post("/login")
def login(body: LoginRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Verify the password and issue a long-lived session token."""
    if not reg.db or not reg.db.has_password():
        raise HTTPException(status_code=400, detail="No password is configured")
    stored = reg.db.get_password_hash()
    if not auth_service.verify_password(body.password, stored):
        # bcrypt's own cost is the brute-force deterrent; keep the message vague.
        raise HTTPException(status_code=401, detail="Incorrect password")
    token = auth_service.new_session_token()
    expires_at = auth_service.session_expiry()
    reg.db.create_session(auth_service.hash_token(token), expires_at)
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
    if reg.db.has_password():
        stored = reg.db.get_password_hash()
        if not auth_service.verify_password(body.current_password or "", stored):
            raise HTTPException(
                status_code=401, detail="Current password is incorrect")
    reg.db.set_password_hash(auth_service.hash_password(new_password))
    reg.db.delete_all_sessions()  # force re-login everywhere
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, reg: ServiceRegistry = Depends(get_registry)):
    """Invalidate the caller's current session token (no-op for the nonce)."""
    token = _bearer(request)
    if reg.db and token:
        reg.db.delete_session(auth_service.hash_token(token))
    return {"ok": True}
