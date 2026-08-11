import importlib.util
import os
import types

import pytest


@pytest.fixture(autouse=True)
def _restore_shared_dv_detect():
    """Undo any stubbing of the SHARED backend.rename.dv_detect module.

    _load() hands back a fresh dv_host_scan each time, but `m.dv_detect` is the
    real shared module -- so a test assigning to it leaks stub lambdas into
    every LATER test file. This suite passed only because pytest's alphabetical
    order happens to run test_dv_detect.py before this file; running them the
    other way round failed 27 of its tests against stubs left behind here.

    A suite whose result depends on collection order is a suite that can lie,
    which is the one thing these tests exist not to do. Autouse so a new test
    cannot reintroduce the leak by forgetting to clean up.
    """
    from backend.rename import dv_detect as _shared
    saved = (_shared.available, _shared.detect_layer)
    try:
        yield
    finally:
        _shared.available, _shared.detect_layer = saved

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


def test_post_urls_have_no_api_prefix():
    # The router mounts at bare /rename (no /api prefix) — see
    # backend/api/routes/rename.py's APIRouter(prefix="/rename", ...) and its
    # inclusion in backend/api/main.py. The POST targets must hit that path,
    # not /api/rename/... (which 404s).
    m = _load()
    for path in (m.DV_ROWS_PATH, m.DV_IMPORT_PATH):
        url = "http://localhost:9721".rstrip("/") + path
        assert url.startswith("http://localhost:9721/rename/")
        assert "/api/" not in url
    assert m.DV_ROWS_PATH.endswith("/rename/dv-host-rows")


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


def _main_harness(tmp_path, n_files, clock_step, argv_extra,
                  detect=None, post_ok=True):
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
    m._iter_files = lambda roots: iter(paths)
    m._post_rows = lambda api, rows: (posts.append(api) or post_ok)
    m.time = _Clock(clock_step)

    m.dv_detect.available = lambda: True
    m.dv_detect.detect_layer = detect or (
        lambda p, **kw: {"layer": "fel", "error": None, "evidence": "bounded"})

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
    m._post_rows = lambda api, rows: None

    m.main(["--config", "x", "--db", str(db), "--api", "http://x",
            "--mode", "steady", "--max-runtime-minutes", "0"])
    assert seen == [str(fresh)], "steady mode must not re-attempt known failures"


# --- POST-rows transport (round-4 redesign) ---------------------------------

def test_interim_post_does_not_close_the_connection(tmp_path, monkeypatch):
    """POST-rows redesign: the container never reads dv_host.db now, so the
    interim POST must NOT close/reopen the connection.

    The old close/reopen dance existed ONLY to release the file for the
    container's cross-OS read (which failed on the Windows bind mount's WAL
    mmap). Sending rows in the request body removes that whole failure class —
    and reintroducing a mid-scan close would be pure cost. This asserts the
    interim POSTs happen with the connection OPEN, and each carries the rows
    scanned so far; only the final POST follows the single close.
    """
    m = _load()
    closes = {"n": 0}
    real_open = m._open_db

    class _Tracked:
        def __init__(self, inner):
            self._inner = inner
        def close(self):
            closes["n"] += 1
            return self._inner.close()
        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(m, "_open_db", lambda p: _Tracked(real_open(p)))

    seen = []
    monkeypatch.setattr(
        m, "_post_rows",
        lambda api, rows: seen.append((closes["n"], len(rows))) or True)

    root = tmp_path / "lib"; root.mkdir()
    paths = []
    for i in range(6):
        f = root / ("m%d.mkv" % i); f.write_bytes(b"x" * 32); paths.append(str(f))
    monkeypatch.setattr(m, "load_host_config",
                        lambda _p: {"dv_detection": True, "dv_library_roots": str(root)})
    monkeypatch.setattr(m, "_iter_files", lambda roots: iter(paths))
    monkeypatch.setattr(m.dv_detect, "available", lambda: True)
    monkeypatch.setattr(m.dv_detect, "detect_layer",
                        lambda p, **kw: {"layer": "fel", "error": None, "evidence": "b"})

    rc = m.main(["--config", "x", "--db", str(tmp_path / "h.db"),
                 "--api", "http://127.0.0.1:9", "--import-every", "2",
                 "--max-runtime-minutes", "0"])
    assert rc == 0
    # 6 files at every-2 => interim POSTs after 2, 4, 6, plus the final one.
    assert len(seen) >= 4, seen
    interim, final = seen[:-1], seen[-1]
    assert all(c == 0 for c, _ in interim), (
        f"interim POSTs must run with the connection OPEN (no close): {seen}")
    assert final[0] == 1, f"only the final POST follows the single close: {seen}"
    # Cumulative snapshot: each POST carries the rows accumulated so far.
    assert [n for _, n in seen] == [2, 4, 6, 6], seen


def test_post_rows_rejects_a_count_mismatch(tmp_path, monkeypatch):
    """_post_rows accepts the result ONLY when the response validates."""
    m = _load()
    import urllib.request

    class _Resp:
        def __init__(self, body):
            self._b = body.encode("utf-8")
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    # processed != source_rows -> must be rejected even on HTTP 200.
    monkeypatch.setattr(m._INGEST_OPENER, "open",
                        lambda *a, **k: _Resp(
                            '{"ok": true, "source_rows": 2, "processed": 1, '
                            '"failed": 0}'))
    assert m._post_rows("http://x", [{"path": "a"}, {"path": "b"}]) is False

    # A fully-valid response is accepted.
    monkeypatch.setattr(m._INGEST_OPENER, "open",
                        lambda *a, **k: _Resp(
                            '{"ok": true, "source_rows": 2, "processed": 2, '
                            '"failed": 0}'))
    assert m._post_rows("http://x", [{"path": "a"}, {"path": "b"}]) is True


def test_post_rows_rejects_a_non_2xx(tmp_path, monkeypatch):
    m = _load()
    import io
    import urllib.error
    import urllib.request

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "http://x", 500, "err", {}, io.BytesIO(b'{"ok": false}'))

    monkeypatch.setattr(m._INGEST_OPENER, "open", _raise)
    assert m._post_rows("http://x", [{"path": "a"}]) is False


def test_post_rows_rejects_a_non_object_body(tmp_path, monkeypatch):
    """Valid JSON that is not an object (null, list) must fail, not raise on
    .get (round-4 cleanup: isinstance(body, dict) guard)."""
    m = _load()
    import urllib.request

    class _Resp:
        def __init__(self, body):
            self._b = body.encode("utf-8")
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    for payload in ("null", "[]", "42"):
        monkeypatch.setattr(m._INGEST_OPENER, "open",
                            lambda *a, _p=payload, **k: _Resp(_p))
        assert m._post_rows("http://x", [{"path": "a"}]) is False, payload


def test_post_rows_sends_schema_version(tmp_path, monkeypatch):
    """The producer stamps schema_version and source_rows into the body so the
    container can reject an unrecognised version (round-4 cleanup)."""
    m = _load()
    import json
    import urllib.request

    captured = {}

    class _Resp:
        def read(self):
            return (b'{"ok": true, "source_rows": 2, "processed": 2, '
                    b'"failed": 0}')
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _capture(req, *a, **k):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(m._INGEST_OPENER, "open", _capture)
    assert m._post_rows("http://x", [{"path": "a"}, {"path": "b"}]) is True
    assert captured["body"]["schema_version"] == m.DV_ROWS_SCHEMA_VERSION == 1
    assert captured["body"]["source_rows"] == 2


def _capture_req(monkeypatch, m):
    """Capture the urllib Request _post_rows builds, returning a 2-row OK reply."""
    import urllib.request
    box = {}

    class _Resp:
        def read(self):
            return (b'{"ok": true, "source_rows": 2, "processed": 2, '
                    b'"failed": 0}')
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _cap(req, *a, **k):
        box["req"] = req
        return _Resp()

    monkeypatch.setattr(m._INGEST_OPENER, "open", _cap)
    return box


def test_post_rows_sends_ingest_key_header_when_configured(tmp_path, monkeypatch):
    """The scoped machine credential rides on X-DV-Ingest-Key when the host has
    SCANHOUND_DV_INGEST_KEY set (peer review: least-privilege ingest key)."""
    m = _load()
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY", "  the-secret  ")  # trimmed
    box = _capture_req(monkeypatch, m)
    assert m._post_rows("http://x", [{"path": "a"}, {"path": "b"}]) is True
    # urllib capitalizes header keys; get_header uses that normalized form.
    assert box["req"].get_header("X-dv-ingest-key") == "the-secret"


def test_post_rows_omits_ingest_key_header_when_unset(tmp_path, monkeypatch):
    """Unset => no header => the server 401s and the POST fails loudly, which is
    the correct 'not configured' outcome (not a silent open call)."""
    m = _load()
    monkeypatch.delenv("SCANHOUND_DV_INGEST_KEY", raising=False)
    box = _capture_req(monkeypatch, m)
    assert m._post_rows("http://x", [{"path": "a"}, {"path": "b"}]) is True
    assert box["req"].get_header("X-dv-ingest-key") is None


def _serve(handler_cls):
    """Start a throwaway localhost HTTP server on a free port; returns (srv, base)."""
    import http.server
    import threading
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def test_post_rows_refuses_redirect_and_never_leaks_key(tmp_path, monkeypatch):
    """A 302 from the endpoint must FAIL the post, and the raw ingest key must
    never reach the redirect target (peer review MEDIUM blocker, 2026-08-10)."""
    import http.server
    m = _load()
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY", "TOP-SECRET")

    sink = {"hit": False, "saw_key": None}

    class Sink(http.server.BaseHTTPRequestHandler):
        def _h(self):
            sink["hit"] = True
            sink["saw_key"] = self.headers.get("X-DV-Ingest-Key")
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        do_GET = do_POST = _h
        def log_message(self, *a): pass

    sink_srv, sink_base = _serve(Sink)
    try:
        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", sink_base + "/anything")
                self.end_headers()
            def log_message(self, *a): pass

        red_srv, red_base = _serve(Redirector)
        try:
            # The endpoint 302s to the sink; the detector must refuse it.
            assert m._post_rows(red_base, [{"path": "a"}]) is False
        finally:
            red_srv.shutdown()
    finally:
        sink_srv.shutdown()

    assert sink["hit"] is False, "the redirect was followed — request reached the sink"
    assert sink["saw_key"] is None, "the ingest key leaked to the redirect target"


def test_post_rows_ignores_ambient_proxy(tmp_path, monkeypatch):
    """The credential must reach ONLY the configured origin — never an ambient
    HTTP proxy. build_opener installs a default ProxyHandler that honours
    http_proxy; the detector clears it with ProxyHandler({}) (peer review
    round-2 blocker, 2026-08-10)."""
    import http.server

    proxy = {"hit": False}

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            proxy["hit"] = True
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass

    got = {"key": None}

    class Direct(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["key"] = self.headers.get("X-DV-Ingest-Key")
            body = (b'{"ok": true, "source_rows": 1, "processed": 1, '
                    b'"failed": 0}')
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass

    proxy_srv, proxy_base = _serve(Proxy)
    direct_srv, direct_base = _serve(Direct)
    # Set the ambient proxy BEFORE loading the module, so the opener is built
    # with it in the environment (the mutation's default ProxyHandler reads env
    # at construction; our ProxyHandler({}) ignores it regardless).
    monkeypatch.setenv("http_proxy", proxy_base)
    monkeypatch.setenv("HTTP_PROXY", proxy_base)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY", "TOP-SECRET")
    m = _load()
    try:
        assert m._post_rows(direct_base, [{"path": "a"}]) is True
    finally:
        proxy_srv.shutdown(); direct_srv.shutdown()
    assert proxy["hit"] is False, "the credential was routed through an ambient proxy"
    assert got["key"] == "TOP-SECRET", "the request must reach the configured origin directly"


def test_post_rows_direct_success_delivers_key(tmp_path, monkeypatch):
    """Positive control: with NO redirect, the key reaches the configured host
    and the post succeeds — so the redirect test above isn't vacuously green."""
    import http.server
    m = _load()
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY", "TOP-SECRET")

    got = {"key": None}

    class Direct(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["key"] = self.headers.get("X-DV-Ingest-Key")
            body = (b'{"ok": true, "source_rows": 1, "processed": 1, '
                    b'"failed": 0}')
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass

    srv, base = _serve(Direct)
    try:
        assert m._post_rows(base, [{"path": "a"}]) is True
    finally:
        srv.shutdown()
    assert got["key"] == "TOP-SECRET", "the key must reach the configured endpoint"


def test_a_failed_detection_prints_no_rate(tmp_path, caplog):
    """No throughput number may appear for a detection that failed.

    On a stall dovi_tool may have read any fraction of the file, so dividing the
    whole file size by elapsed time is a guess wearing a unit -- and it would be
    printed on exactly the titles that already wedge. Consolidation blocker 1.
    """
    caplog.set_level("INFO")
    stalled = lambda p, **kw: {"layer": "unknown", "error": "stalled after 180s"}
    rc, _rows, _posts, _paths = _main_harness(
        tmp_path, 2, 0.0, ["--max-runtime-minutes", "0"], detect=stalled)
    assert rc == 0
    assert "MB/s" not in caplog.text, "a failed detection must not print a rate"
    assert "rate unavailable" in caplog.text
    assert "stalled after 180s" in caplog.text, "the error must stay diagnosable"


def test_a_successful_detection_still_reports_its_rate(tmp_path, caplog):
    """The positive control: without this, blocker 1's test could pass vacuously
    by the rate never being printed at all."""
    caplog.set_level("INFO")
    _main_harness(tmp_path, 2, 0.0, ["--max-runtime-minutes", "0"])
    assert "MB/s effective scan rate" in caplog.text


def test_a_failed_final_import_is_a_failed_run(tmp_path):
    """_post_import used to swallow its outcome, so a scan could complete, exit
    0, and leave the container with none of its results. Consolidation
    blocker 2."""
    rc, _rows, posts, _paths = _main_harness(
        tmp_path, 2, 0.0, ["--max-runtime-minutes", "0"], post_ok=False)
    assert rc == 1, "a final import that failed must not report success"
    assert posts, "the final import must still be attempted"
