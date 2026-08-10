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


def _run_main(m, tmp_path, monkeypatch, *, files, import_every=10,
              post_ok=True):
    """Drive the real main() over *files* with dovi_tool and the API stubbed.

    Returns (exit_code, posts) where posts is the ordered list of label strings
    _post_import was called with -- so cadence is asserted from the actual call
    sequence rather than from a counter the test maintains itself.
    """
    root = tmp_path / "lib"
    root.mkdir()
    for name in files:
        (root / name).write_bytes(b"\0" * 16)

    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"dv_detection": true, "dv_library_roots": "%s"}'
                   % str(root).replace("\\", "/"), encoding="utf-8")

    posts = []

    def fake_post(api_base, label="dv-import"):
        posts.append(label)
        return post_ok

    monkeypatch.setattr(m, "_post_import", fake_post)
    monkeypatch.setattr(m.dv_detect, "available", lambda: True)
    monkeypatch.setattr(m.dv_detect, "detect_layer",
                        lambda p, **kw: {"layer": "fel", "error": None})

    code = m.main(["--config", str(cfg), "--db", str(tmp_path / "h.db"),
                   "--api", "http://127.0.0.1:9",
                   "--import-every", str(import_every)])
    return code, posts


def test_import_fires_during_the_walk_not_only_at_the_end(tmp_path, monkeypatch):
    # THE BUG THIS FIXES. _post_import() was the last statement of main(), and
    # the walk never finishes inside the task's PT6H limit, so a killed run
    # delivered nothing however many rows it had committed. Measured 2026-08-10:
    # host 622 rows, container 466, watermark frozen at 2026-07-26.
    m = _load()
    files = ["m%02d.mkv" % i for i in range(25)]
    code, posts = _run_main(m, tmp_path, monkeypatch, files=files,
                            import_every=10)
    assert code == 0
    interim = [p for p in posts if p == "interim dv-import"]
    # 25 files at every-10 -> interim at 10 and 20, then the final one.
    assert len(interim) == 2, posts
    assert posts[-1] == "final dv-import"


def test_a_killed_run_would_still_have_handed_off(tmp_path, monkeypatch):
    # The property that actually matters: an interim import happens BEFORE the
    # walk ends, so work already committed survives a mid-loop kill.
    m = _load()
    files = ["m%02d.mkv" % i for i in range(12)]
    _, posts = _run_main(m, tmp_path, monkeypatch, files=files, import_every=5)
    assert posts[0] == "interim dv-import"
    assert posts.count("interim dv-import") == 2       # at 5 and 10
    assert posts[-1] == "final dv-import"


def test_import_every_zero_restores_end_only_behaviour(tmp_path, monkeypatch):
    m = _load()
    files = ["m%02d.mkv" % i for i in range(25)]
    code, posts = _run_main(m, tmp_path, monkeypatch, files=files,
                            import_every=0)
    assert code == 0
    assert posts == ["final dv-import"]


def test_final_import_runs_even_when_nothing_was_scanned(tmp_path, monkeypatch):
    # Rows committed by earlier KILLED runs sit in the host store unexported --
    # that backlog is how the 622/466 gap accumulated. Gating the final import
    # on scanned>0 would strand it forever.
    m = _load()
    code, posts = _run_main(m, tmp_path, monkeypatch, files=[])
    assert code == 0
    assert posts == ["final dv-import"]


def test_a_failed_final_import_is_a_failed_run(tmp_path, monkeypatch):
    # _post_import() used to swallow its outcome, so a scan could complete,
    # exit 0, and leave the container with none of its results.
    m = _load()
    code, posts = _run_main(m, tmp_path, monkeypatch, files=["a.mkv"],
                            post_ok=False)
    assert code == 1, "a final import that failed must not report success"
    assert posts[-1] == "final dv-import"


def test_post_import_reports_success_and_failure(monkeypatch):
    m = _load()

    class _Resp:
        def read(self):
            return b'{"imported": 3, "updated": 0}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert m._post_import("http://localhost:9721") is True

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(m.urllib.request, "urlopen", _boom)
    assert m._post_import("http://localhost:9721") is False


def test_default_db_path_resolves_to_shared_data_dir():
    # backend/rename/dv_import.py's container-side default is /data/dv_host.db,
    # bind-mounted from <repo-root>/data on the host (see docker-compose.yml's
    # ./data:/data). The script's own --db default must resolve to the same
    # file so the automatic post-scan import finds it without an explicit flag.
    m = _load()
    repo_root = m.Path(SCRIPT).resolve().parents[2]
    expected = repo_root / "data" / "dv_host.db"
    assert m.Path(m.DEFAULT_DB_PATH).resolve() == expected
