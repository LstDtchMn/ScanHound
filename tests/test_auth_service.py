"""Unit tests for backend.auth_service (password hashing / verification).

Pure functions — no DB, no app, no fixtures needed beyond what pytest gives
for free.
"""
import time

import bcrypt
import pytest

from backend import auth_service


def test_verify_password_roundtrip_normal_cost():
    stored = auth_service.hash_password("pw")
    assert auth_service.verify_password("pw", stored) is True
    assert auth_service.verify_password("wrong", stored) is False


def test_verify_password_rejects_garbage_hash():
    assert auth_service.verify_password("pw", "not-a-bcrypt-hash") is False


def test_verify_password_rejects_empty_inputs():
    stored = auth_service.hash_password("pw")
    assert auth_service.verify_password("", stored) is False
    assert auth_service.verify_password("pw", "") is False


# ── bcrypt cost cap (Codex 1c) ─────────────────────────────────────────
# A cost-31 hash in auth_credentials would hang a login for hours (measured:
# cost-18 ≈ 12s per verify). Unreachable through the app's own surface today,
# but defense-in-depth against a mangled manual DB restore.

def test_verify_password_rejects_over_cost_hash_fast():
    # Generated once here rather than fixture-shared: bcrypt.gensalt(15) is
    # itself the expensive part we want to confirm we DON'T pay twice.
    over_cost_hash = bcrypt.hashpw(
        auth_service._prehash("pw"), bcrypt.gensalt(15)
    ).decode("utf-8")
    start = time.monotonic()
    result = auth_service.verify_password("pw", over_cost_hash)
    elapsed = time.monotonic() - start
    assert result is False
    assert elapsed < 0.5, f"verify_password took {elapsed:.2f}s — cost cap not short-circuiting"


def test_verify_password_rejects_non_canonical_hash_prefix():
    # A well-formed-looking but non-`$2` string (e.g. a corrupted/foreign
    # hash format) must fail closed, not raise or hang.
    assert auth_service.verify_password("pw", "$1$abcdefgh$somehash") is False


def test_verify_password_still_accepts_normal_cost_12_hash():
    # Regression guard: the cap must not reject legitimate hashes at the
    # library's own default cost.
    stored = bcrypt.hashpw(auth_service._prehash("pw"), bcrypt.gensalt(12)).decode("utf-8")
    assert auth_service.verify_password("pw", stored) is True
