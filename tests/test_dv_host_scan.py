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
