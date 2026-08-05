"""A rebuilt-empty database must not be mistaken for a fresh install (A-1).

Round-2 review, CRITICAL. A corrupt database is auto-quarantined and a fresh
EMPTY one is rebuilt in its place. That new database honestly reports no
credential -- and "no credential" is what un-gates /auth/set-password. So:

    a password existed
      -> the database was quarantined automatically
      -> an unauthenticated request POSTs /auth/set-password
      -> the caller is now administrator

The earlier three-state rewrite defends a FAILING credential read. This is a
SUCCEEDING read of a database that was just rebuilt empty, which is a
different case and was not covered: the existing auth tests prove fresh-install
bootstrap and unreadable-storage fail-closed, neither of which is this.

These tests drive the REAL quarantine path rather than writing a marker by
hand, because the property under test is that the production sequence cannot
produce an unauthenticated takeover.
"""

import os
import sqlite3
import tempfile

import pytest

from backend import auth_service
from backend.api.dependencies import (
    RECOVERY_LOCKED,
    credential_state,
)
from backend.database import DatabaseManager, corruption_flag_path


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm",
                   ".corrupt_flag.json", ".corrupt_flag.notified.json"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass
    # the quarantine backups
    import glob
    for f in glob.glob(path + ".corrupt.*"):
        try:
            os.unlink(f)
        except OSError:
            pass


def _credentialed_db(path):
    db = DatabaseManager(path)
    assert db.set_password_hash(auth_service.hash_password("original-secret"))
    assert db.has_password() is True
    db.close()
    return db


def _corrupt_the_file(path):
    """Make the main DB file genuinely unopenable, so the PRODUCTION
    quarantine branch runs rather than a simulated one."""
    with open(path, "r+b") as fh:
        fh.seek(0)
        fh.write(b"this is not a sqlite database at all, not even close")


def _quarantine_for_real(path):
    _corrupt_the_file(path)
    rebuilt = DatabaseManager(path)          # triggers quarantine + rebuild
    return rebuilt


# ── the attack ───────────────────────────────────────────────────────

def test_a_quarantined_install_is_recovery_locked_not_fresh(db_path):
    _credentialed_db(db_path)
    rebuilt = _quarantine_for_real(db_path)
    try:
        # The rebuilt DB is genuinely empty and says so, honestly.
        assert rebuilt.has_password() is False
        assert rebuilt.credential_state() == "absent"
        # A quarantine marker was left on disk BEFORE the rebuild.
        assert (os.path.exists(corruption_flag_path(db_path))
                or os.path.exists(f"{db_path}.corrupt_flag.notified.json"))

        # ...but the auth layer must NOT read that as a fresh install.
        assert credential_state(rebuilt) == RECOVERY_LOCKED, (
            "an empty rebuilt database read as 'absent' un-gates "
            "/auth/set-password and hands admin to the first caller")
    finally:
        rebuilt.close()


def test_the_gate_is_armed_so_the_bootstrap_exemption_is_unreachable(db_path):
    """auth_enabled() is what decides whether /auth/set-password is exempt."""
    from backend.api import dependencies as deps

    _credentialed_db(db_path)
    rebuilt = _quarantine_for_real(db_path)
    saved_db, saved_nonce = deps.registry.db, deps.registry.auth_nonce
    try:
        deps.registry.db = rebuilt
        deps.registry.auth_nonce = ""
        assert deps.auth_enabled() is True, (
            "with the gate disarmed the bootstrap path is exempt and "
            "unauthenticated password setup succeeds")
    finally:
        deps.registry.db, deps.registry.auth_nonce = saved_db, saved_nonce
        rebuilt.close()


def test_the_permanent_notified_marker_also_locks(db_path):
    """DISAGREEING CASE for checking only the pending flag.

    The pending flag is CONSUMED once the corruption alert is confirmed
    delivered. Relying on it alone re-opens the takeover precisely when the
    operator has been successfully notified -- i.e. when they are least likely
    to still be watching the box.
    """
    _credentialed_db(db_path)
    rebuilt = _quarantine_for_real(db_path)
    try:
        # Simulate the alert having been delivered: pending -> notified.
        pending = corruption_flag_path(db_path)
        if os.path.exists(pending):
            os.replace(pending, f"{db_path}.corrupt_flag.notified.json")
        assert not os.path.exists(pending)

        assert credential_state(rebuilt) == RECOVERY_LOCKED, (
            "the lock lapsed as soon as the alert was delivered")
    finally:
        rebuilt.close()


# ── the positive controls, which are what make the above meaningful ──

def test_a_genuinely_fresh_install_still_bootstraps(db_path):
    """POSITIVE CONTROL. A fix that locked every credential-less database
    would pass every test above and break first-run setup for everyone."""
    from backend.api import dependencies as deps

    os.unlink(db_path)                       # never initialised
    fresh = DatabaseManager(db_path)
    saved_db, saved_nonce = deps.registry.db, deps.registry.auth_nonce
    try:
        deps.registry.db = fresh
        deps.registry.auth_nonce = ""
        assert not os.path.exists(corruption_flag_path(db_path))
        assert credential_state(fresh) == "absent", (
            "a fresh install has no quarantine marker and must remain "
            "bootstrappable")
        assert deps.auth_enabled() is False
    finally:
        deps.registry.db, deps.registry.auth_nonce = saved_db, saved_nonce
        fresh.close()


def test_a_normal_credentialed_install_is_unaffected(db_path):
    db = DatabaseManager(db_path)
    try:
        db.set_password_hash(auth_service.hash_password("secret"))
        assert credential_state(db) == "present"
    finally:
        db.close()


def test_an_unreadable_database_still_reports_unknown_not_locked(db_path):
    """The earlier three-state behaviour must survive: a FAILING read is
    'unknown', which is a different remedy from a rebuilt-empty one."""
    from unittest.mock import patch

    db = DatabaseManager(db_path)
    try:
        with patch.object(DatabaseManager, "get_connection", return_value=None):
            assert credential_state(db) == "unknown"
    finally:
        db.close()


def test_recovery_by_removing_the_marker_restores_bootstrap(db_path):
    """The documented recovery path must actually work, or the lock is a
    permanent outage rather than a gate."""
    _credentialed_db(db_path)
    rebuilt = _quarantine_for_real(db_path)
    try:
        assert credential_state(rebuilt) == RECOVERY_LOCKED
        for suffix in (".corrupt_flag.json", ".corrupt_flag.notified.json"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass
        assert credential_state(rebuilt) == "absent", (
            "the operator cleared the marker on the host; bootstrap must "
            "become possible again")
    finally:
        rebuilt.close()


class TestNonceLiftsTheLock:
    """Jesse's ratified trade (2026-08-05): keep the lock, but presenting the
    desktop nonce is enough to lift it — no hand-editing files on the host.

    The nonce is out-of-band proof by construction: it comes from the local
    process that started the app, not from the network. A session token is not,
    and could not be anyway — a rebuilt-empty database has no credential that
    could have issued one.
    """

    def test_the_nonce_lifts_the_lock_and_keeps_the_incident_record(self, db_path):
        from backend.api import dependencies as deps

        _credentialed_db(db_path)
        rebuilt = _quarantine_for_real(db_path)
        saved_db, saved_nonce = deps.registry.db, deps.registry.auth_nonce
        try:
            deps.registry.db = rebuilt
            deps.registry.auth_nonce = "desktop-nonce-abc123"
            assert credential_state(rebuilt) == RECOVERY_LOCKED

            assert deps.token_authorized("desktop-nonce-abc123") is True

            assert credential_state(rebuilt) == "absent", (
                "the lock did not lift after out-of-band proof")
            # The history must survive the unlock.
            assert os.path.exists(f"{db_path}.corrupt_flag.recovered.json"), (
                "the only durable record that corruption happened was deleted")
            assert not os.path.exists(corruption_flag_path(db_path))
        finally:
            deps.registry.db, deps.registry.auth_nonce = saved_db, saved_nonce
            try:
                os.unlink(f"{db_path}.corrupt_flag.recovered.json")
            except OSError:
                pass
            rebuilt.close()

    def test_a_wrong_nonce_does_not_lift_the_lock(self, db_path):
        """DISAGREEING CASE. A fix that cleared the marker on any nonce
        COMPARISON rather than a successful match would pass the test above
        and hand the unlock to anyone who guesses at the endpoint."""
        from backend.api import dependencies as deps

        _credentialed_db(db_path)
        rebuilt = _quarantine_for_real(db_path)
        saved_db, saved_nonce = deps.registry.db, deps.registry.auth_nonce
        try:
            deps.registry.db = rebuilt
            deps.registry.auth_nonce = "the-real-nonce"

            assert deps.token_authorized("not-the-nonce") is False
            assert credential_state(rebuilt) == RECOVERY_LOCKED, (
                "a failed nonce attempt lifted the lock")
        finally:
            deps.registry.db, deps.registry.auth_nonce = saved_db, saved_nonce
            rebuilt.close()

    def test_a_session_token_cannot_lift_the_lock(self, db_path):
        """Only the nonce counts. A session token is network-presented, so it
        is not out-of-band proof — and on a rebuilt-empty database there is no
        credential that could have issued one in the first place."""
        from backend.api import dependencies as deps
        from backend import auth_service

        _credentialed_db(db_path)
        rebuilt = _quarantine_for_real(db_path)
        saved_db, saved_nonce = deps.registry.db, deps.registry.auth_nonce
        try:
            deps.registry.db = rebuilt
            deps.registry.auth_nonce = ""
            token = auth_service.new_session_token()
            rebuilt.create_session(auth_service.hash_token(token),
                                   auth_service.session_expiry())

            assert deps.token_authorized(token) is True, "fixture: valid session"
            assert credential_state(rebuilt) == RECOVERY_LOCKED, (
                "a session token lifted the lock")
        finally:
            deps.registry.db, deps.registry.auth_nonce = saved_db, saved_nonce
            rebuilt.close()
