import importlib.util
import os
import types

HERE = os.path.dirname(__file__)
SCRIPT = os.path.abspath(os.path.join(
    HERE, "..", "scripts", "host-detector", "dv_host_scan.py"))


def _load():
    spec = importlib.util.spec_from_file_location("dv_host_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stat(mtime, size):
    s = types.SimpleNamespace()
    s.st_mtime = mtime
    s.st_size = size
    return s


def test_signature_skip_2s_boundary():
    m = _load()
    assert m.DV_MTIME_TOL >= 2.0
    # within tolerance + same size -> current (skip)
    assert m.sig_is_current(100.0, 5000, 101.9, 5000) is True
    # 2.0s exactly is within (<=)
    assert m.sig_is_current(100.0, 5000, 102.0, 5000) is True
    # beyond tolerance -> not current
    assert m.sig_is_current(100.0, 5000, 103.0, 5000) is False
    # size mismatch always rescans
    assert m.sig_is_current(100.0, 5000, 100.0, 5001) is False
    # NULL stored signature always rescans
    assert m.sig_is_current(None, 5000, 100.0, 5000) is False
    assert m.sig_is_current(100.0, None, 100.0, 5000) is False


def test_classify_to_row():
    m = _load()
    st = _stat(123.5, 9999)
    row = m.classify_to_row("Y:/M/a.mkv", "fel", st)
    assert row["path"] == "Y:/M/a.mkv"
    assert row["dv_layer"] == "fel"
    assert row["sig_mtime"] == 123.5
    assert row["sig_size"] == 9999
    # unknown -> NULL mtime so the next run retries
    row2 = m.classify_to_row("Y:/M/b.mkv", "unknown", st)
    assert row2["sig_mtime"] is None


def test_tag_name_map():
    m = _load()
    assert m.tag_name_for("fel") == "Dolby Vision Profile 7 FEL"
    assert m.tag_name_for("mel") == "Dolby Vision Profile 7 MEL"
    assert m.tag_name_for("profile8") == "Dolby Vision Profile 8"
    assert m.tag_name_for("profile5") == "Dolby Vision Profile 5"
    assert m.tag_name_for("none") is None
    assert m.tag_name_for("unknown") is None


def test_should_run_config_gates(tmp_path):
    m = _load()
    # detection off -> no-op
    assert m.should_run({"dv_detection": False, "dv_library_roots": "Y:/M"}) is False
    # detection on but no roots -> no-op
    assert m.should_run({"dv_detection": True, "dv_library_roots": ""}) is False
    # detection on + roots -> run
    assert m.should_run({"dv_detection": True, "dv_library_roots": "Y:/M"}) is True


def test_load_host_config_missing(tmp_path):
    m = _load()
    cfg = m.load_host_config(str(tmp_path / "nope.json"))
    assert cfg == {}


def test_parse_roots_splits_semicolon_and_newline():
    m = _load()
    cfg = {"dv_library_roots": "Y:\\M ; E:\\4K\n\\\\SRV\\Share"}
    roots = m.parse_roots(cfg)
    assert roots == ["Y:\\M", "E:\\4K", "\\\\SRV\\Share"]


def test_script_never_imports_database_manager():
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()
    assert "DatabaseManager" not in src
    assert "crawler.db" not in src


def test_post_import_default_url_has_no_api_prefix():
    # The router mounts at bare /rename (no /api prefix) — see
    # backend/api/routes/rename.py's APIRouter(prefix="/rename", ...) and its
    # inclusion in backend/api/main.py. _post_import must target that path,
    # not /api/rename/dv-import (which 404s).
    m = _load()
    url = "http://localhost:9721".rstrip("/") + m.DV_IMPORT_PATH
    assert url.endswith("/rename/dv-import")
    assert "/api/" not in url


def test_default_db_path_resolves_to_shared_data_dir():
    # backend/rename/dv_import.py's container-side default is /data/dv_host.db,
    # bind-mounted from <repo-root>/data on the host (see docker-compose.yml's
    # ./data:/data). The script's own --db default must resolve to the same
    # file so the automatic post-scan import finds it without an explicit flag.
    m = _load()
    repo_root = m.Path(SCRIPT).resolve().parents[2]
    expected = repo_root / "data" / "dv_host.db"
    assert m.Path(m.DEFAULT_DB_PATH).resolve() == expected


# ── retry backoff ──────────────────────────────────────────────────────

def test_retry_delay_escalates_then_caps():
    m = _load()
    assert m.retry_delay_hours(0) == 0
    assert m.retry_delay_hours(1) == 6
    assert m.retry_delay_hours(2) == 24
    assert m.retry_delay_hours(3) == 72
    assert m.retry_delay_hours(4) == 168
    # Escalation stops rather than growing without bound: a file must stay
    # eligible forever, just never constantly.
    assert m.retry_delay_hours(99) == 168


def test_is_retry_due():
    m = _load()
    now = 1_000_000.0
    assert m.is_retry_due(None, now) is True          # pre-migration rows
    assert m.is_retry_due(now - 1, now) is True
    assert m.is_retry_due(now + 1, now) is False
    assert m.is_retry_due("garbage", now) is True     # unreadable -> eligible


def test_failed_row_records_attempt_and_schedules_retry():
    m = _load()
    st = _stat(500.0, 42)
    row = m.classify_to_row("Y:/M/a.mkv", "unknown", st, attempts=1,
                            error="stalled", now=1000.0)
    assert row["sig_mtime"] is None and row["sig_size"] is None
    assert row["attempts"] == 2
    assert row["last_error"] == "stalled"
    assert row["next_retry_at"] == 1000.0 + 24 * 3600


def test_successful_row_clears_failure_state():
    m = _load()
    st = _stat(500.0, 42)
    row = m.classify_to_row("Y:/M/a.mkv", "fel", st, attempts=3, now=1000.0)
    assert row["sig_mtime"] == 500.0 and row["sig_size"] == 42
    assert row["attempts"] == 0
    assert row["next_retry_at"] is None
    assert row["last_error"] is None


# ── work ordering ──────────────────────────────────────────────────────

def _row(layer="fel", mtime=100.0, size=10, attempts=0, next_retry_at=None):
    return {"dv_layer": layer, "sig_mtime": mtime, "sig_size": size,
            "attempts": attempts, "next_retry_at": next_retry_at}


class TestPartitionWork:
    def test_current_files_are_not_work(self):
        m = _load()
        st = _stat(100.0, 10)
        work = m.partition_work([("a.mkv", st, _row(mtime=100.0, size=10))], 0.0)
        assert work == []

    def test_order_is_never_scanned_then_changed_then_retries(self):
        m = _load()
        now = 10_000.0
        cands = [
            ("retry.mkv", _stat(1.0, 10), _row(layer="unknown", mtime=None,
                                               size=None, attempts=1,
                                               next_retry_at=now - 5)),
            ("changed.mkv", _stat(2.0, 99), _row(mtime=1.0, size=10)),
            ("new.mkv", _stat(3.0, 10), None),
        ]
        assert [p for p, _, _ in m.partition_work(cands, now)] == [
            "new.mkv", "changed.mkv", "retry.mkv"]

    def test_retry_not_yet_due_is_excluded(self):
        m = _load()
        now = 10_000.0
        cands = [("wedged.mkv", _stat(1.0, 10),
                  _row(layer="unknown", mtime=None, size=None, attempts=4,
                       next_retry_at=now + 3600))]
        assert m.partition_work(cands, now) == []

    def test_fresh_acquisition_beats_the_backlog(self):
        """The starvation regression, stated as the behaviour that failed.

        Two titles that wedge dovi_tool sat at the FRONT of every run purely
        because os.walk reaches their root first, and a newly-acquired file
        queued behind hundreds of never-scanned backlog entries for the same
        reason. Ordering the buckets alone does not fix that -- a fresh
        acquisition and a two-month-old backlog entry are BOTH 'never
        scanned'. Sorting that bucket newest-first is what does, so this
        asserts on mtime order and not merely on bucket order.
        """
        m = _load()
        backlog = [(f"old{i}.mkv", _stat(1000.0 + i, 10), None) for i in range(200)]
        fresh = ("just-grabbed.mkv", _stat(9_000_000.0, 10), None)
        work = m.partition_work(backlog + [fresh], 9_000_100.0)
        assert work[0][0] == "just-grabbed.mkv"
        assert len(work) == 201

    def test_longest_waiting_failure_retries_first(self):
        m = _load()
        now = 10_000.0
        cands = [
            ("recent.mkv", _stat(1.0, 10), _row(layer="unknown", mtime=None,
                                                size=None, next_retry_at=now - 1)),
            ("ancient.mkv", _stat(1.0, 10), _row(layer="unknown", mtime=None,
                                                 size=None, next_retry_at=now - 999)),
        ]
        assert [p for p, _, _ in m.partition_work(cands, now)] == [
            "ancient.mkv", "recent.mkv"]


# ── schema migration ───────────────────────────────────────────────────

def test_open_db_adds_retry_columns_to_a_preexisting_table(tmp_path):
    """An existing dv_host.db predates these columns and must not be rebuilt."""
    import sqlite3
    db = tmp_path / "dv_host.db"
    conn = sqlite3.connect(str(db))
    conn.execute('''CREATE TABLE dv_host (
        path TEXT PRIMARY KEY, dv_layer TEXT, sig_mtime REAL,
        sig_size INTEGER, title TEXT,
        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute("INSERT INTO dv_host (path, dv_layer) VALUES ('keep.mkv','fel')")
    conn.commit()
    conn.close()

    m = _load()
    conn = m._open_db(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dv_host)")}
    assert {"attempts", "last_error", "next_retry_at"} <= cols
    # The pre-existing row survives, and reads back as retry-eligible.
    rows = m._load_rows(conn)
    assert rows["keep.mkv"]["dv_layer"] == "fel"
    assert rows["keep.mkv"]["next_retry_at"] is None
    # Idempotent: opening again must not raise "duplicate column".
    conn.close()
    m._open_db(str(db)).close()


# ── main(): graceful budget + progress publishing ──────────────────────

class _Clock:
    """Deterministic monotonic clock; every read advances by *step*."""

    def __init__(self, step):
        self.step = step
        self.t = 0.0

    def monotonic(self):
        self.t += self.step
        return self.t

    def time(self):
        return 1_700_000_000.0


def _main_harness(tmp_path, n_files, clock_step, argv_extra):
    """Run main() against real temp files with dovi_tool fully stubbed."""
    m = _load()
    root = tmp_path / "lib"
    root.mkdir()
    paths = []
    for i in range(n_files):
        p = root / f"m{i:02d}.mkv"
        p.write_bytes(b"x" * 64)
        paths.append(str(p))

    posts = []
    m.load_host_config = lambda _p: {"dv_detection": True,
                                     "dv_library_roots": str(root)}
    m.dv_detect.available = lambda: True
    m.dv_detect.detect_layer = lambda p, **kw: {"layer": "fel", "error": None,
                                                "evidence": "bounded"}
    m._iter_files = lambda roots: iter(paths)
    m._post_import = lambda api: posts.append(api)
    m.time = _Clock(clock_step)

    db = tmp_path / "dv_host.db"
    rc = m.main(["--config", "ignored", "--db", str(db),
                 "--api", "http://x"] + argv_extra)
    conn = m._open_db(str(db))
    rows = m._load_rows(conn)
    conn.close()
    return rc, rows, posts, paths


def test_budget_stops_between_files_and_still_imports(tmp_path):
    """A hard kill loses the file in flight AND skips the dv-import POST.

    That is why the container's dv_scan gained nothing for two weeks while the
    host database kept growing: every run died at its Task Scheduler limit
    before reaching the handoff at the end of main(). Stopping ourselves must
    therefore do BOTH things -- stop early, and still publish.
    """
    # 5 s per clock read, 0.1 min (6 s) of budget: one file, then stop.
    rc, rows, posts, _ = _main_harness(
        tmp_path, 10, 5.0, ["--max-runtime-minutes", "0.1"])
    assert rc == 0
    assert 0 < len(rows) < 10, f"expected an early stop, scanned {len(rows)}"
    assert posts, "an early stop must still POST dv-import"


def test_zero_budget_scans_everything(tmp_path):
    rc, rows, posts, paths = _main_harness(
        tmp_path, 6, 5.0, ["--max-runtime-minutes", "0"])
    assert rc == 0
    assert len(rows) == len(paths)
    assert posts


def test_progress_is_published_during_a_long_run(tmp_path):
    """Not only at the end: a backfill that is killed must leave results behind."""
    rc, rows, posts, _ = _main_harness(
        tmp_path, 9, 0.0, ["--max-runtime-minutes", "0", "--import-every", "3"])
    assert rc == 0 and len(rows) == 9
    # 3 periodic posts (after 3, 6, 9) plus the final one.
    assert len(posts) == 4, posts


def test_steady_mode_skips_the_retry_sweep(tmp_path):
    m = _load()
    root = tmp_path / "lib"
    root.mkdir()
    wedged = root / "wedged.mkv"
    wedged.write_bytes(b"x" * 64)
    fresh = root / "fresh.mkv"
    fresh.write_bytes(b"x" * 64)

    db = tmp_path / "dv_host.db"
    conn = m._open_db(str(db))
    m._upsert(conn, {"path": str(wedged), "dv_layer": "unknown",
                     "sig_mtime": None, "sig_size": None, "attempts": 4,
                     "last_error": "stalled", "next_retry_at": None})
    conn.close()

    seen = []
    m.load_host_config = lambda _p: {"dv_detection": True,
                                     "dv_library_roots": str(root)}
    m.dv_detect.available = lambda: True

    def _detect(p, **kw):
        seen.append(p)
        return {"layer": "fel", "error": None, "evidence": "bounded"}

    m.dv_detect.detect_layer = _detect
    m._iter_files = lambda roots: iter([str(wedged), str(fresh)])
    m._post_import = lambda api: None

    m.main(["--config", "x", "--db", str(db), "--api", "http://x",
            "--mode", "steady", "--max-runtime-minutes", "0"])
    assert seen == [str(fresh)], "steady mode must not re-attempt known failures"
