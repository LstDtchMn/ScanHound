"""The TV share is verified at the WRITE, not by stopping the app.

Background: /library/tv is a bind of a NAS share mounted into the Docker VM.
If the share is absent when the container is created, /library/tv is a plain
directory inside the VM that looks exactly like the share, and every TV rename
"succeeds" into it (2026-07-26). Until 2026-09-01 the only protection was the
host task stopping the whole container, which made a NAS outage an app outage.

These tests pin the guard that replaces that: the same identity rule the host
task applies (mountpoint + fstype 9p + expected UNC origin), applied by the
placement layer before it creates anything, refusing with a reason when the
root is not verified, and passing paths that are not share-backed through.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from backend import share_identity as si
from backend.rename import fileops
from backend.runtime_lock import _unlocked_fileops_for_tests

SHARE = "TURTLELANDSRV2\\k"


def _esc(field: str) -> str:
    # Real mountinfo escaping: backslash and space as octal.
    return field.replace("\\", "\\134").replace(" ", "\\040")


def _entry(mountpoint: str, fstype: str = "9p", share: str = SHARE) -> str:
    source = "\\\\" + share.split("\\")[0] + "\\" + share.split("\\")[1]
    superopts = "rw,dirsync,aname=drvfs;path=UNC\\%s;symlinkroot=/mnt/,mmap,access=client,msize=262144,trans=fd" % share
    return "100 90 0:60 / %s rw,relatime - %s %s %s" % (_esc(mountpoint), fstype, _esc(source), _esc(superopts))


ROOT_ENTRY = "20 1 0:22 / / rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y,workdir=/z"


def _table(*entries: str) -> str:
    return "\n".join((ROOT_ENTRY,) + entries) + "\n"


@pytest.fixture(autouse=True)
def _restore_default_roots():
    yield
    si.configure(None)


# ----------------------------------------------------------------- config --

def test_the_default_root_is_the_tv_share_and_nothing_else():
    si.configure(None)
    assert si.configured_roots() == ["/library/tv"]


def test_spec_parsing_accepts_pairs_and_bare_roots():
    roots = si.parse_share_backed_roots("/library/tv => SRV\\k; /other\n# comment\n")
    assert {v[0] for v in roots.values()} == {"/library/tv", "/other"}
    expected = {v[0]: v[1] for v in roots.values()}
    assert expected["/library/tv"] == "SRV\\k"
    assert expected["/other"] is None


@pytest.mark.parametrize("bad", ["/library/tv =>", " => SRV\\k"])
def test_a_malformed_spec_falls_back_to_the_default_not_to_nothing(bad):
    """A typo in settings must not remove the guard from the TV share."""
    with pytest.raises(ValueError):
        si.parse_share_backed_roots(bad)
    si.configure(bad)
    assert si.configured_roots() == ["/library/tv"]


def test_root_matching_respects_the_path_boundary():
    si.configure("/library/tv => SRV\\k")
    assert si.classify("/library/tvx/file.mkv", mountinfo=_table()).state == "not-share-backed"
    assert si.classify("/library/tv/Show/ep.mkv", mountinfo=_table()).state != "not-share-backed"
    assert si.classify("/library/tv", mountinfo=_table()).state != "not-share-backed"


# -------------------------------------------------------------- mountinfo --

def test_mountinfo_parser_decodes_octal_escapes_and_skips_junk():
    text = _table(_entry("/library/tv shows"), "garbage line without separator", "1 2 3 - x")
    entries = si.parse_mountinfo(text)
    mps = [e["mountpoint"] for e in entries]
    assert "/library/tv shows" in mps
    tv = [e for e in entries if e["mountpoint"] == "/library/tv shows"][0]
    assert tv["fstype"] == "9p"
    assert tv["source"] == "\\\\TURTLELANDSRV2\\k"
    assert "path=UNC\\TURTLELANDSRV2\\k" in tv["superopts"]


# --------------------------------------------------------------- classify --

def test_verified_when_root_is_a_9p_mount_of_the_expected_share():
    si.configure("/library/tv => " + SHARE)
    v = si.classify("/library/tv/Show/ep.mkv", mountinfo=_table(_entry("/library/tv")))
    assert v.state == "verified", v
    assert v.ok
    assert v.fstype == "9p"


def test_blind_when_root_is_not_a_mountpoint_at_all():
    """The 2026-07-26 shape: the bind resolved to an empty VM directory."""
    si.configure("/library/tv => " + SHARE)
    v = si.classify("/library/tv/Show/ep.mkv", mountinfo=_table())
    assert v.state == "blind"
    assert not v.ok
    assert "not a mountpoint" in v.reason


def test_blind_when_root_is_mounted_but_not_9p():
    si.configure("/library/tv => " + SHARE)
    v = si.classify("/library/tv/x", mountinfo=_table(_entry("/library/tv", fstype="ext4")))
    assert v.state == "blind"
    assert "ext4" in v.reason


def test_blind_when_it_is_a_9p_mount_of_a_different_share():
    si.configure("/library/tv => " + SHARE)
    v = si.classify("/library/tv/x", mountinfo=_table(_entry("/library/tv", share="TURTLELANDSRV2\\other")))
    assert v.state == "blind"
    assert "wrong share" in v.reason


def test_a_bare_root_accepts_any_9p_share():
    si.configure("/library/tv")
    v = si.classify("/library/tv/x", mountinfo=_table(_entry("/library/tv", share="ANY\\thing")))
    assert v.state == "verified"


def test_the_last_entry_at_the_root_wins_like_the_host_task():
    si.configure("/library/tv => " + SHARE)
    stacked_good = _table(_entry("/library/tv", fstype="ext4"), _entry("/library/tv"))
    stacked_bad = _table(_entry("/library/tv"), _entry("/library/tv", fstype="ext4"))
    assert si.classify("/library/tv/x", mountinfo=stacked_good).state == "verified"
    assert si.classify("/library/tv/x", mountinfo=stacked_bad).state == "blind"


def test_unknown_when_the_mount_table_cannot_be_read(monkeypatch):
    """Unknown is not clean: an unreadable table verifies nothing."""
    si.configure("/library/tv => " + SHARE)

    def boom():
        raise OSError("no /proc here")
    monkeypatch.setattr(si, "_read_mountinfo", boom)
    v = si.classify("/library/tv/x")
    assert v.state == "unknown"
    assert not v.ok


def test_unknown_when_the_mount_table_is_empty():
    si.configure("/library/tv => " + SHARE)
    assert si.classify("/library/tv/x", mountinfo="").state == "unknown"


def test_paths_outside_every_root_are_not_this_modules_business():
    si.configure("/library/tv => " + SHARE)
    v = si.classify("/library/movies/x.mkv", mountinfo="")
    assert v.state == "not-share-backed"
    assert v.ok


# ---------------------------------------------------------------- require --

def test_require_refuses_a_blind_root_with_the_reason(monkeypatch):
    si.configure("/library/tv => " + SHARE)
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())
    with pytest.raises(si.ShareNotVerifiedError) as exc:
        si.require_share_backed("/library/tv/Show/ep.mkv", operation="place_file")
    msg = str(exc.value)
    assert "place_file refused" in msg
    assert "not a mountpoint" in msg
    assert "Nothing was written" in msg


def test_require_passes_a_verified_root(monkeypatch):
    si.configure("/library/tv => " + SHARE)
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table(_entry("/library/tv")))
    assert si.require_share_backed("/library/tv/x", operation="place_file").state == "verified"


def test_the_test_bypass_is_scoped(monkeypatch):
    si.configure("/library/tv => " + SHARE)
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())
    with si._unverified_shares_for_tests():
        assert si.require_share_backed("/library/tv/x", operation="t").state == "bypassed"
    with pytest.raises(si.ShareNotVerifiedError):
        si.require_share_backed("/library/tv/x", operation="t")


def test_status_reports_every_root_and_the_guard_version(monkeypatch):
    si.configure("/library/tv => " + SHARE)
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())
    s = si.status()
    assert s["guard_version"] == si.GUARD_VERSION
    assert s["roots"]["/library/tv"]["state"] == "blind"


# ------------------------------------------------------- the placement layer --

def _configure_tmp_root(tmp_path: Path) -> Path:
    root = tmp_path / "tv"
    si.configure("%s => %s" % (root, SHARE))
    return root


def test_place_file_refuses_a_blind_root_BEFORE_creating_anything(tmp_path, monkeypatch):
    """The whole point. A makedirs into a blind root is already the accident,
    so the refusal must come before the destination folder exists."""
    root = _configure_tmp_root(tmp_path)
    src = tmp_path / "incoming.mkv"
    src.write_bytes(b"x" * 10)
    dst = root / "Show" / "Show - S01E01.mkv"
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())      # root absent: blind
    with _unlocked_fileops_for_tests():
        with pytest.raises(si.ShareNotVerifiedError):
            fileops.place_file(str(src), str(dst), "copy")
    assert not (root / "Show").exists(), "the destination folder was created inside a blind root"
    assert not root.exists(), "the blind root itself was created"
    assert src.exists(), "the source was consumed by a refused placement"


def test_place_file_proceeds_on_a_verified_root(tmp_path, monkeypatch):
    root = _configure_tmp_root(tmp_path)
    src = tmp_path / "incoming.mkv"
    src.write_bytes(b"y" * 10)
    dst = root / "Show" / "Show - S01E01.mkv"
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table(_entry(str(root))))
    with _unlocked_fileops_for_tests():
        used = fileops.place_file(str(src), str(dst), "copy")
    assert used == "copy"
    assert dst.read_bytes() == b"y" * 10


def test_place_file_ignores_destinations_that_are_not_share_backed(tmp_path, monkeypatch):
    """Movie renames on the local drives must not start failing because the
    NAS is away."""
    _configure_tmp_root(tmp_path)
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"z")
    dst = tmp_path / "movies" / "Movie (2026).mkv"
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())      # everything blind
    with _unlocked_fileops_for_tests():
        assert fileops.place_file(str(src), str(dst), "copy") == "copy"
    assert dst.exists()


def test_undo_place_refuses_a_blind_root(tmp_path, monkeypatch):
    root = _configure_tmp_root(tmp_path)
    monkeypatch.setattr(si, "_read_mountinfo", lambda: _table())
    with _unlocked_fileops_for_tests():
        with pytest.raises(si.ShareNotVerifiedError):
            fileops.undo_place(str(tmp_path / "src.mkv"), str(root / "Show" / "ep.mkv"), "copy")
    assert not root.exists()


def test_the_guard_is_the_second_statement_of_both_placement_entry_points():
    """Executable contract, in the style of the writer-lock contract: the
    share check sits directly behind the writer lock and ahead of every
    statement that could touch the filesystem."""
    tree = ast.parse(Path(fileops.__file__).read_text(encoding="utf-8"))
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("place_file", "undo_place"):
        body = list(functions[name].body)
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        second = body[1]
        assert isinstance(second, ast.Expr) and isinstance(second.value, ast.Call), name
        assert getattr(second.value.func, "id", None) == "require_share_backed", name
