"""Peer-review blocker D-1: the one-time DB relocation must not certify a
stale snapshot after a concurrent update.

The pre-fix sequence was: checkpoint the legacy WAL, close the connection,
``shutil.copy2`` the main DB file, then compare user_version and row counts.
Nothing held the source still between the checkpoint and the copy. In WAL mode
a transaction committing in that window lands in the source's -wal while the
copied main file remains the pre-write snapshot, and a row-count comparison
cannot see a same-count change (an UPDATE, or a paired DELETE+INSERT). A
coherent but stale copy was therefore certified, adopted, and — because the
``os.path.exists(new_path)`` guard in ``_resolve_db_path`` never retries —
adopted permanently.

Measured on this image before the fix, to confirm the premise rather than
assume it: after ``PRAGMA wal_checkpoint(TRUNCATE)`` the -wal is 0 bytes; one
post-checkpoint UPDATE grows it to 4152 bytes; and a copy of the main file
alone still reads the OLD value while the source reads the new one.

The fix copies through SQLite's online backup API inside a read transaction
that is held across the verification too, so the destination is by
construction the source's state at one instant.

Nothing here mocks SQLite. Every case builds a real WAL database on tmp_path
with a real writer that stays connected, because a writer's ``close()`` is
what triggers SQLite's implicit checkpoint — closing it would fold the change
into the main file and make the buggy implementation pass.
"""
import os
import shutil
import sqlite3

import pytest

import backend.config as cfg


# ── helpers ──────────────────────────────────────────────────────────────

def _open_writer(path):
    """A writer whose commits STAY in the -wal.

    wal_autocheckpoint=0 matters: it removes SQLite's own size-triggered fold
    as a variable, so "the main file is stale" is a property of the test setup
    and not a coincidence of how few pages were written.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _read(path, sql="SELECT id, v FROM t ORDER BY id"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _main_file_only(legacy_path, dest_path):
    """What ``shutil.copy2`` of the main DB file alone observes.

    A WAL-mode main file opened without its -wal simply shows the last
    checkpointed state, so this is the counterfactual the pre-fix code
    certified as good.
    """
    shutil.copy2(legacy_path, dest_path)
    return _read(dest_path)


class _BackupHookedConn:
    """Passes everything through to a real connection, but routes ``backup``
    to a test hook.

    ``sqlite3.Connection`` is an immutable C type — ``monkeypatch.setattr(
    sqlite3.Connection, "backup", ...)`` raises TypeError — so the only
    available seam is ``sqlite3.connect`` itself.
    """

    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def backup(self, target, *args, **kwargs):
        # The destination came through the same patched connect(), so unwrap
        # it: the C backup() rejects anything that is not a real Connection.
        target = getattr(target, "_real", target)
        return self._hook(self._real, target, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _hook_backup(monkeypatch, hook):
    """Route every connection's ``backup`` through ``hook`` for this test.

    Returns the UNPATCHED ``sqlite3.connect``. A hook that needs its own
    connection must use it: going through the patched one hands back another
    hooked wrapper, so calling ``.backup()`` on it re-enters the hook and
    recurses until the stack blows.
    """
    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        return _BackupHookedConn(real_connect(*args, **kwargs), hook)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    return real_connect


class TestRelocationSnapshotConsistency:

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        # SQLite burns the whole busy timeout per attempt while a reader holds
        # the WAL; the production defaults would add ~6s to the blocked cases.
        monkeypatch.setattr(cfg, "_MIGRATION_CHECKPOINT_BUSY_MS", 50)
        monkeypatch.setattr(cfg, "_MIGRATION_CHECKPOINT_ATTEMPTS", 2)

    def _paths(self, tmp_path):
        legacy = str(tmp_path / "appdata" / "crawler.db")
        new = str(tmp_path / "vol" / "crawler.db")
        os.makedirs(os.path.dirname(legacy))
        os.makedirs(os.path.dirname(new))
        return legacy, new

    def _seed(self, path, rows=((1, "v1"),)):
        w = _open_writer(path)
        w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        w.executemany("INSERT INTO t VALUES (?, ?)", rows)
        return w

    def _mutate_after_checkpoint(self, monkeypatch, writer, statements):
        """Run ``statements`` in the window the finding is about: strictly
        AFTER the pre-copy checkpoint and BEFORE the copy.

        The real checkpoint gate runs first and unchanged, so the migration
        still believes it is looking at a fully folded database — that belief
        is exactly what went stale.
        """
        real_gate = cfg._assert_wal_fully_checkpointed

        def _gate_then_write(legacy_path):
            real_gate(legacy_path)
            for sql in statements:
                writer.execute(sql)

        monkeypatch.setattr(cfg, "_assert_wal_fully_checkpointed", _gate_then_write)

    # ── the finding ──────────────────────────────────────────────────────

    def test_update_after_checkpoint_is_not_certified_stale(self, tmp_path, monkeypatch):
        """An UPDATE in the checkpoint→copy window changes no row count, so the
        pre-fix verification passed it and froze the OLD value in place."""
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "v1"),))
        try:
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # main file holds v1
            self._mutate_after_checkpoint(
                monkeypatch, w, ["UPDATE t SET v = 'v2' WHERE id = 1"])

            assert cfg._checkpoint_and_copy(legacy, new) is True

            # Setup self-check: the staleness the finding describes really did
            # exist. Without this the test could pass vacuously on a run where
            # the change happened to be folded into the main file anyway.
            assert _main_file_only(legacy, str(tmp_path / "bare.db")) == [(1, "v1")], (
                "setup: the main file must still be the pre-update snapshot, "
                "otherwise there is nothing for the migration to get wrong")

            assert _read(new) == [(1, "v2")], (
                "the migrated DB certified the pre-update main file")
        finally:
            w.close()

    def test_paired_delete_insert_after_checkpoint_is_not_certified_stale(
            self, tmp_path, monkeypatch):
        """DISAGREEING CASE for a fix that only tightened the row-count check.

        A DELETE paired with an INSERT leaves the count identical while
        changing both the contents and the set of rowids, so no count-based
        verification — however strict — can distinguish stale from current.
        Only taking the copy through a consistent read can.
        """
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "a"), (2, "b")))
        try:
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._mutate_after_checkpoint(monkeypatch, w, [
                "DELETE FROM t WHERE id = 2",
                "INSERT INTO t VALUES (3, 'c')",
            ])

            assert cfg._checkpoint_and_copy(legacy, new) is True
            assert _main_file_only(legacy, str(tmp_path / "bare.db")) == [
                (1, "a"), (2, "b")], "setup: main file must still be stale"
            assert _read(new) == [(1, "a"), (3, "c")]
        finally:
            w.close()

    def test_writer_committing_after_the_backup_does_not_void_the_migration(
            self, tmp_path, monkeypatch):
        """DISAGREEING CASE for the other half of the fix: the read snapshot has
        to be HELD across the verification, not just across the copy.

        An implementation that copied correctly but then re-read the source
        through a fresh connection would see this post-copy INSERT, find a row
        count the copy cannot have, and reject a faithful migration — measured
        here as 1 row inside the snapshot vs 2 outside it. Rejection is safe
        but permanent-ish in effect: on any busy machine the relocation would
        keep failing and the DB would stay on the bind mount forever.
        """
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "v1"),))
        try:
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            def _backup_then_commit(source, target, *args, **kwargs):
                result = source.backup(target, *args, **kwargs)
                w.execute("INSERT INTO t VALUES (2, 'landed_after_copy')")
                return result

            _hook_backup(monkeypatch, _backup_then_commit)
            assert cfg._checkpoint_and_copy(legacy, new) is True
        finally:
            w.close()

        # A defined, consistent value: the snapshot as of the copy. The row
        # committed afterwards is simply not part of it (it is still in the
        # legacy DB, which the relocation does not delete).
        assert _read(new) == [(1, "v1")]
        assert _read(legacy) == [(1, "v1"), (2, "landed_after_copy")]

    # ── positive controls: a fix that disabled relocation must fail here ──

    def test_healthy_migration_still_relocates_via_resolve_db_path(
            self, tmp_path, monkeypatch):
        """POSITIVE CONTROL at the consumer. Refusing to migrate, or always
        falling back to legacy, would satisfy every failure case above; this
        pins that the uncontended path still moves the DB and its contents."""
        legacy, new = self._paths(tmp_path)
        self._seed(legacy, rows=[(i, f"v{i}") for i in range(600)]).close()

        monkeypatch.setattr(cfg, "_DATA_DIR", os.path.dirname(legacy))
        monkeypatch.setattr(cfg, "_DB_DIR", os.path.dirname(new))

        assert cfg._resolve_db_path("crawler.db") == new
        assert _read(new, "SELECT COUNT(*) FROM t") == [(600,)]
        # No debris the next boot could misread.
        assert not os.path.exists(new + "-wal")
        assert not os.path.exists(new + "-shm")
        assert not os.path.exists(new + ".migrating")

    def test_wal_only_rows_survive_the_migration(self, tmp_path):
        """POSITIVE CONTROL on the WAL path itself: rows that exist ONLY in the
        -wal must reach the destination. This is the property the backup API
        provides directly, and the one a plain file copy of the main file
        cannot."""
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "v1"),))
        try:
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            w.executemany("INSERT INTO t VALUES (?, ?)",
                          [(i, f"wal{i}") for i in range(2, 302)])
            assert os.path.getsize(legacy + "-wal") > 0, "setup: WAL must be hot"

            assert cfg._checkpoint_and_copy(legacy, new) is True
            assert _read(new, "SELECT COUNT(*) FROM t") == [(301,)]
        finally:
            w.close()

    def test_legacy_db_is_still_writable_after_the_migration(self, tmp_path):
        """The held read transaction must be released. Leaking it would leave a
        pinned snapshot on the legacy DB for the life of the process, which is
        precisely the condition that makes later checkpoints partial."""
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "v1"),))
        try:
            assert cfg._checkpoint_and_copy(legacy, new) is True
            w.execute("INSERT INTO t VALUES (2, 'after')")
            # A pinned reader would make this fold only some frames.
            cfg._assert_wal_fully_checkpointed(legacy)
        finally:
            w.close()

    # ── failure handling: _resolve_db_path's fallback contract ───────────

    def test_backup_failure_falls_back_to_legacy_and_leaves_new_path_empty(
            self, tmp_path, monkeypatch):
        """A failed copy must never crash startup and never leave anything at
        new_path — a partial file there would trip the idempotency guard on the
        next boot and skip migration forever."""
        legacy, new = self._paths(tmp_path)
        w = self._seed(legacy, rows=((1, "v1"),))
        w.close()

        monkeypatch.setattr(cfg, "_DATA_DIR", os.path.dirname(legacy))
        monkeypatch.setattr(cfg, "_DB_DIR", os.path.dirname(new))

        def _boom(source, target, *args, **kwargs):
            raise sqlite3.OperationalError("simulated disk I/O error")

        _hook_backup(monkeypatch, _boom)

        assert cfg._resolve_db_path("crawler.db") == legacy
        assert not os.path.exists(new)
        # The temp sibling may survive; it is a different filename, so the
        # exists(new_path) guard still retries on the next boot.
        assert _read(legacy) == [(1, "v1")]

    def test_mismatched_destination_bytes_are_still_rejected(self, tmp_path, monkeypatch):
        """The verification net must survive the change of copy mechanism.

        Re-homed coverage: tests/test_db_integrity_audit_pass2.py's
        ``test_stale_copy_is_rejected_even_when_the_checkpoint_looks_clean``
        injected this by patching ``shutil.copy2``, which the backup API no
        longer calls. Same property, current seam — if whatever lands at the
        destination does not match the source snapshot, the migration must fail
        rather than freeze it in. ``PRAGMA integrity_check`` passes on the
        substituted file, so nothing downstream could catch it.
        """
        legacy, new = self._paths(tmp_path)
        self._seed(legacy, rows=[(i, f"v{i}") for i in range(600)]).close()

        # A structurally valid, internally consistent, WRONG database.
        stale_src = str(tmp_path / "stale.db")
        s = sqlite3.connect(stale_src)
        s.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        s.commit()
        s.close()
        assert _read(stale_src, "PRAGMA integrity_check") == [("ok",)]

        holder = {}

        def _write_wrong_db(source, target, *args, **kwargs):
            # Substitute the wrong database for the real backup's output, via
            # the unpatched connect so this does not re-enter the hook.
            wrong = holder["connect"](stale_src)
            try:
                wrong.backup(target)
            finally:
                wrong.close()

        holder["connect"] = _hook_backup(monkeypatch, _write_wrong_db)

        with pytest.raises(RuntimeError, match="row-count mismatch"):
            cfg._checkpoint_and_copy(legacy, new)
        assert not os.path.exists(new)

    # REMOVED: test_stale_migrating_leftover_is_replaced_not_merged
    #
    # It was VACUOUS. Adversarial verification deleted the leftover-sidecar
    # cleanup it names, outright, and the test still passed -- the backup API
    # truncates and overwrites the destination regardless. It also asserted a
    # hazard ("an orphaned -wal would be replayed into the fresh copy") that
    # could not be reproduced: a SIGKILLed writer leaving a 2 MB leftover -wal
    # with 498 junk rows produced an identical, clean result with the cleanup
    # present and absent.
    #
    # A test that passes with its subject removed is worse than no test, because
    # it certifies the code. The cleanup itself is kept as cheap belt-and-braces
    # with an honest comment (see _fold_and_drop_copy_sidecars) rather than the
    # false justification it carried.

