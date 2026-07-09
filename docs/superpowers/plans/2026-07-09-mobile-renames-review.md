# Mobile Renames Review + Conflict Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Renames screen a phone-native, one-at-a-time review of the items that need a decision, and give "a file already exists" a real resolution: a side-by-side technical-spec comparison of both files followed by Overwrite / Keep-both.

**Architecture:** A frontend `isPhone` fork replaces the crammed desktop list with a summary hero + full-screen review deck. The backend adds an `ffprobe` spec probe, a no-persistence `conflict-preview` endpoint, an optional `conflict_strategy` on apply (overwrite trashes-not-deletes via the existing `_trash`; keep_both dedupes the filename), ranking that judges probed specs, and an on-demand FEL/MEL background scan. All built on existing primitives.

**Tech Stack:** SvelteKit 5 (runes), FastAPI, `ffprobe` + `dovi_tool` (already in the image), existing `renames` store / `api` client / `RematchModal` / WebSocket events. Deploy via `docker compose up -d --build`.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- The desktop `/renames` page and desktop navigation must be UNCHANGED; the mobile UI fork is gated strictly on `isPhone`.
- **Data safety is absolute:** Overwrite MUST route the displaced file through `fileops._trash()` (recoverable, same-volume). NEVER `os.remove` a destination file.
- **The apply request body MUST be optional/defaulted.** The current bodyless `POST /rename/jobs/{id}/apply` must keep validating (a required body would 422 every existing caller).
- "Ready to apply" = `match_confidence >= 100`, via the shared `classifyJob` helper — not the server's ≥95% apply-confident threshold.
- `ffprobe` probes are fail-safe and time-boxed (return null on missing file / missing binary / timeout ~20–30s). `dovi_tool` (FEL/MEL) is NEVER run synchronously in a request — cache-only in the preview; a background button resolves it.
- Reuse existing modules — `conflicts.py`, `fileops._trash`, `RematchModal`, the `renames` store actions. Do not duplicate their logic.
- Rename Pydantic models use Pydantic v2 default `extra='ignore'`; adding fields is safe. Do not add `extra='forbid'`.
- Works in both the responsive web app and the Tauri/Android wrapper.
- TDD: each task writes a failing test first, then the minimal code. Backend: `pytest`. Frontend: `vitest` (`npm run test`), plus `npm run check` and `npm run build` before deploy.
- Backend test runner note: the prod image lacks pytest; run the changed-module subset in a throwaway `scanhound:latest` container with the code `docker cp`'d onto the overlay fs (per project convention), or in any env with `requirements-docker.txt` + `pytest`/`httpx` installed. Do NOT run the full suite (network tests in `test_api_routes.py` hang) — run the named test files with `--timeout=60`.

---

## File Structure

**Backend (new):**
- `backend/rename/mediainfo.py` — `probe_specs(path)`: one structured ffprobe call → spec dict.

**Backend (modify):**
- `backend/rename/fileops.py` — extract `dedupe_dest(dst)` from the `_trash` suffix loop.
- `backend/rename/conflicts.py` — `_quality_score` accepts explicit probed spec fields; add `rank_conflict(existing, incoming)`.
- `backend/rename/service.py` — `apply(conflict_strategy=…)` branch; `queue_apply(conflict_strategy=…)`; `undo()` restores an overwritten original; new `conflict_preview(job_id)` and `scan_conflict_dv(job_id)`.
- `backend/api/routes/rename.py` — `ApplyRequest` body on `apply_job`; new `POST /jobs/{id}/conflict-preview` and `POST /jobs/{id}/scan-dv-conflict`.

**Frontend (new):**
- `frontend/src/lib/renames/review.ts` — `classifyJob`, `partitionJobs`, `hasDestinationConflict`, `matchesQuery` (moved from the page).
- `frontend/src/lib/components/renames/RenameReviewCard.svelte`
- `frontend/src/lib/components/renames/RenameReviewDeck.svelte`
- `frontend/src/lib/components/renames/MobileRenamesView.svelte`

**Frontend (modify):**
- `frontend/src/lib/api/types.ts` — `FileSpec`, `ConflictComparison`.
- `frontend/src/lib/api/client.ts` — `conflictPreview`, `scanConflictDv`, `applyRename` optional body.
- `frontend/src/lib/stores/renames.ts` — `applyJob(id, strategy?)`.
- `frontend/src/routes/renames/+page.svelte` — `isPhone` fork; import `matchesQuery` from `review.ts`.

---

## Task B1: `probe_specs()` ffprobe spec probe

**Files:**
- Create: `backend/rename/mediainfo.py`
- Test: `tests/test_mediainfo.py`

**Interfaces:**
- Produces: `probe_specs(path: str, timeout: int = 30, db=None) -> dict | None` returning
  `{present, size_bytes, container, duration_min, bitrate, resolution, video_codec, hdr, dv_layer, audio}`.
  `dv_layer` is read ONLY from the `dv_scan` cache via `db` (never shells `dovi_tool`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mediainfo.py
import json
from unittest.mock import patch, MagicMock
from backend.rename import mediainfo

FFPROBE_JSON = json.dumps({
    "format": {"format_name": "matroska,webm", "size": "42000000000",
               "duration": "7200.0", "bit_rate": "46000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "color_transfer": "smpte2084",
         "side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"codec_type": "audio", "codec_name": "truehd", "channels": 8,
         "channel_layout": "7.1"},
    ],
})

def test_probe_specs_parses_ffprobe(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))
    assert s["present"] is True
    assert s["resolution"] == "2160p"
    assert s["video_codec"] == "HEVC"
    assert s["hdr"] == "Dolby Vision"          # DOVI side_data wins over PQ
    assert s["audio"].startswith("TrueHD")
    assert s["size_bytes"] == 42000000000
    assert s["duration_min"] == 120

def test_probe_specs_missing_file_returns_not_present():
    assert mediainfo.probe_specs("/no/such.mkv")["present"] is False

def test_probe_specs_no_ffprobe_returns_none(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    with patch("shutil.which", return_value=None):
        assert mediainfo.probe_specs(str(f)) is None

def test_probe_specs_dv_layer_from_cache_only(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.get_dv_scan.return_value = {"dv_layer": "fel", "sig_mtime": None, "sig_size": None}
    db.dv_scan_is_current.return_value = True
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f), db=db)
    assert s["dv_layer"] == "fel"
    db.get_dv_scan.assert_called_once()          # cache read, no dovi_tool
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_mediainfo.py -v --timeout=60`
Expected: FAIL (module `backend.rename.mediainfo` does not exist).

- [ ] **Step 3: Write the implementation**

```python
# backend/rename/mediainfo.py
"""Structured video technical-spec probe (ffprobe) for the conflict compare.

One ffprobe call per file → a stable spec dict. Fail-safe: returns None when
ffprobe is unavailable / errors / times out, and {"present": False} when the
file does not exist. The DV FEL/MEL layer is read ONLY from the dv_scan cache
(never shells the slow dovi_tool) — an on-demand scan resolves it separately.
Mirrors the fail-safe pattern of llm_identify.probe_video_width.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

_RES_LADDER = ((2160, "2160p"), (1440, "1440p"), (1080, "1080p"),
               (720, "720p"), (480, "480p"), (0, None))
_CODEC_LABEL = {"hevc": "HEVC", "h265": "HEVC", "avc": "H.264", "h264": "H.264",
                "av1": "AV1", "vc1": "VC-1", "mpeg2video": "MPEG-2"}
_AUDIO_LABEL = {"truehd": "TrueHD", "eac3": "EAC3", "ac3": "AC3", "dts": "DTS",
                "aac": "AAC", "flac": "FLAC", "opus": "Opus"}


def _res_label(width: Optional[int], height: Optional[int]) -> Optional[str]:
    h = height or 0
    w = width or 0
    key = h if h else (w * 9 // 16 if w else 0)
    for floor, label in _RES_LADDER:
        if key >= floor:
            return label
    return None


def _cached_dv_layer(path: str, size: Optional[int], db) -> Optional[str]:
    if db is None:
        return None
    try:
        row = db.get_dv_scan(path)
        if not row or not row.get("dv_layer"):
            return None
        return row.get("dv_layer")
    except Exception:
        return None


def probe_specs(path: str, timeout: int = 30, db=None) -> Optional[dict]:
    if not path:
        return None
    if not os.path.exists(path):
        return {"present": False, "path": path, "size_bytes": None,
                "container": None, "duration_min": None, "bitrate": None,
                "resolution": None, "video_codec": None, "hdr": None,
                "dv_layer": None, "audio": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    try:
        size = int(fmt.get("size")) if fmt.get("size") else os.path.getsize(path)
    except (TypeError, ValueError, OSError):
        size = None
    try:
        dur = float(fmt.get("duration")) if fmt.get("duration") else None
        duration_min = round(dur / 60) if dur and dur > 0 else None
    except (TypeError, ValueError):
        duration_min = None
    try:
        bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        bitrate = None

    vcodec = _CODEC_LABEL.get(str(video.get("codec_name") or "").lower(),
                              (video.get("codec_name") or None))
    resolution = _res_label(video.get("width"), video.get("height"))

    # HDR: Dolby Vision (DOVI side_data) outranks PQ/HLG.
    hdr = None
    sd = video.get("side_data_list") or []
    if any("dovi" in str(x.get("side_data_type", "")).lower()
           or "dolby vision" in str(x.get("side_data_type", "")).lower() for x in sd):
        hdr = "Dolby Vision"
    else:
        ct = str(video.get("color_transfer") or "").lower()
        if ct == "smpte2084":
            hdr = "HDR10"
        elif ct in ("arib-std-b67", "bt2020-10", "bt2020-12"):
            hdr = "HLG"

    acodec = _AUDIO_LABEL.get(str(audio.get("codec_name") or "").lower(),
                              (audio.get("codec_name") or None))
    chans = audio.get("channel_layout") or (
        f"{audio.get('channels')}ch" if audio.get("channels") else None)
    audio_label = f"{acodec} {chans}".strip() if acodec else None

    return {
        "present": True, "path": path, "size_bytes": size,
        "container": (fmt.get("format_name") or None),
        "duration_min": duration_min, "bitrate": bitrate,
        "resolution": resolution, "video_codec": vcodec, "hdr": hdr,
        "dv_layer": _cached_dv_layer(path, size, db), "audio": audio_label,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_mediainfo.py -v --timeout=60`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/mediainfo.py tests/test_mediainfo.py
git commit -m "feat(rename): probe_specs ffprobe technical-spec probe (cache-only DV)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B2: `dedupe_dest()` for Keep-both

**Files:**
- Modify: `backend/rename/fileops.py` (extract the suffix loop from `_trash`, lines ~224-227)
- Test: `tests/test_fileops_dedupe.py`

**Interfaces:**
- Produces: `dedupe_dest(dst: str) -> str` — returns `dst` if free, else `"{base} ({n}){ext}"` at the next free integer, checked case-insensitively (NTFS).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fileops_dedupe.py
from backend.rename import fileops

def test_dedupe_dest_free_path_unchanged(tmp_path):
    p = tmp_path / "Movie (2024) [2160p].mkv"
    assert fileops.dedupe_dest(str(p)) == str(p)

def test_dedupe_dest_suffixes_and_preserves_ext(tmp_path):
    p = tmp_path / "Movie (2024).mkv"; p.write_bytes(b"x")
    out = fileops.dedupe_dest(str(p))
    assert out.endswith("Movie (2024) (1).mkv")

def test_dedupe_dest_case_insensitive(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x")
    out = fileops.dedupe_dest(str(tmp_path / "MOVIE.mkv"))
    assert out.endswith("(1).mkv")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_fileops_dedupe.py -v --timeout=60`
Expected: FAIL (`dedupe_dest` not defined).

- [ ] **Step 3: Write the implementation**

Add to `backend/rename/fileops.py` (near `_trash`), and refactor `_trash`'s loop (lines 224-227) to call it:

```python
def dedupe_dest(dst: str) -> str:
    """Return ``dst`` if free, else the next ``"{base} ({n}){ext}"`` that does not
    exist. Case-insensitive existence check via ``os.path.lexists`` (NTFS mounts
    are case-insensitive); the extension is preserved. Used by Keep-both."""
    if not os.path.lexists(dst):
        return dst
    directory = os.path.dirname(dst)
    base, ext = os.path.splitext(os.path.basename(dst))
    n = 1
    while True:
        candidate = os.path.join(directory, f"{base} ({n}){ext}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1
```

Then in `_trash`, replace the inline loop (currently `name/base/ext`/`while os.path.lexists(dst)`) so the bucket path uses `dedupe_dest`:

```python
    dst = dedupe_dest(os.path.join(bucket, name))
```

(Delete the now-redundant `base, ext = os.path.splitext(name)` and the `n = 1 / while` block in `_trash`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_fileops_dedupe.py tests/test_rename_fileops.py -v --timeout=60`
Expected: PASS (dedupe tests green; existing fileops/trash tests still green).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/fileops.py tests/test_fileops_dedupe.py
git commit -m "feat(rename): dedupe_dest helper for Keep-both (extracted from _trash)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B3: Rank on probed specs — `_quality_score` extension + `rank_conflict`

**Files:**
- Modify: `backend/rename/conflicts.py`
- Test: `tests/test_conflicts_rank.py`

**Interfaces:**
- Consumes: `probe_specs` output shape (B1) for the explicit-spec fields.
- Produces: `rank_conflict(existing: dict|None, incoming: dict) -> dict` →
  `{recommended: 'existing'|'incoming'|'tie'|None, reason: str|None}`. Both inputs
  are job-like dicts that MAY carry explicit `dv_layer`/`hdr`/`audio`/`resolution`.
- `_quality_score` gains optional consumption of explicit `dv_layer` (rank
  `fel:3, mel:2, profile8:1, profile5:1` inserted below resolution, above the
  binary DV bit) and explicit `hdr`; absent keys reproduce today's behavior.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conflicts_rank.py
from backend.rename import conflicts

def test_explicit_dv_layer_breaks_tie_same_resolution():
    fel = {"id": 1, "original_filename": "a.mkv", "resolution": "2160p", "dv_layer": "fel"}
    mel = {"id": 2, "original_filename": "b.mkv", "resolution": "2160p", "dv_layer": "mel"}
    assert conflicts._quality_score(fel) > conflicts._quality_score(mel)

def test_absent_explicit_fields_reproduce_filename_behaviour():
    remux = {"id": 1, "original_filename": "X.2160p.BluRay.REMUX.DV.mkv", "resolution": "2160p"}
    web = {"id": 2, "original_filename": "X.2160p.WEB-DL.mkv", "resolution": "2160p"}
    assert conflicts._quality_score(remux) > conflicts._quality_score(web)

def test_rank_conflict_recommends_incoming_when_better():
    existing = {"resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "old.mkv"}
    incoming = {"id": 9, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "new.mkv"}
    out = conflicts.rank_conflict(existing, incoming)
    assert out["recommended"] == "incoming"
    assert "2160p" in (out["reason"] or "")

def test_rank_conflict_keeps_existing_dv_remux_over_tag_rich_lower_res():
    # The correctness trap: existing is a Plex-named 2160p DV file (tags stripped),
    # incoming is a tag-rich 1080p. Judged on probed specs, existing wins.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "Movie (2024) [2160p].mkv"}
    incoming = {"id": 9, "resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "Movie.2024.1080p.BluRay.REMUX.Atmos.mkv"}
    assert conflicts.rank_conflict(existing, incoming)["recommended"] == "existing"

def test_rank_conflict_no_existing_is_incoming():
    out = conflicts.rank_conflict(None, {"id": 9, "resolution": "2160p",
                                          "original_filename": "n.mkv"})
    assert out["recommended"] == "incoming"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_conflicts_rank.py -v --timeout=60`
Expected: FAIL (`rank_conflict` missing; explicit `dv_layer` ignored).

- [ ] **Step 3: Implement**

In `_quality_score` (after `res_rank` is computed, before `dv`), add explicit-spec consumption and a DV-layer rank; keep the existing filename fallbacks intact:

```python
    # Explicit probed DV layer (from probe_specs) outranks the binary filename DV
    # bit; absent → 0 so pure-filename callers are unchanged.
    _DV_LAYER_RANK = {"fel": 3, "mel": 2, "profile8": 1, "profile5": 1}
    dv_layer_rank = _DV_LAYER_RANK.get(str(job.get("dv_layer") or "").lower(), 0)

    dv = 1 if _re.search(_DV_RE, name) else 0
    # An explicit DV layer also implies the binary DV bit for filename-only rivals.
    if dv_layer_rank:
        dv = 1
    explicit_hdr = job.get("hdr")
    if explicit_hdr:
        hdr = 0 if explicit_hdr in (None,) else 1
    else:
        hdr = 1 if _re.search(r"\bhdr(10)?(\+|plus)?\b|\bhlg\b", name) else 0
```

Insert `dv_layer_rank` into the returned tuple, just after `res_rank`:

```python
    return (res_rank, dv_layer_rank, dv, hdr, source, audio, edition)
```

Add `rank_conflict` at module end:

```python
def rank_conflict(existing: Optional[dict], incoming: dict) -> dict:
    """Recommend which of an existing on-disk file vs an incoming release to keep,
    judging on explicit probed spec fields when present (so a tag-stripped library
    file isn't unfairly beaten by a tag-rich lower-quality release). Returns
    {recommended: 'existing'|'incoming'|'tie'|None, reason: str|None}."""
    if not existing or existing.get("present") is False:
        return {"recommended": "incoming", "reason": _quality_reason(incoming) or None}
    se, si = _quality_score(existing), _quality_score(incoming)
    if si > se:
        return {"recommended": "incoming", "reason": _quality_reason(incoming) or None}
    if se > si:
        return {"recommended": "existing", "reason": _quality_reason(existing) or None}
    return {"recommended": "tie", "reason": None}
```

Extend `_quality_reason` to prefer explicit fields (so the reason shows the real DV/HDR of a tag-stripped file):

```python
    if job.get("dv_layer") or _re.search(_DV_RE, name):
        bits.append("Dolby Vision")
    elif job.get("hdr"):
        bits.append(job["hdr"])
    elif _re.search(r"\bhdr(10)?(\+|plus)?\b", name):
        bits.append("HDR")
```
(Replace the existing DV/HDR lines in `_quality_reason` with the block above.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_conflicts_rank.py tests/test_rename_service.py -v --timeout=60`
Expected: PASS. **The existing `test_rename_service.py` ranking tests (DV>non-DV, remux>web-dl, res>source, identical→tie) MUST stay green** — the new tuple slot is 0 for all filename-only jobs, so their ordering is unchanged.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/conflicts.py tests/test_conflicts_rank.py
git commit -m "feat(rename): rank conflicts on probed specs; rank_conflict()

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B4: Apply `conflict_strategy` (overwrite / keep_both / skip)

**Files:**
- Modify: `backend/rename/service.py` (`apply` collision guard ~1224-1241; `queue_apply` ~1459)
- Modify: `backend/api/routes/rename.py` (`ApplyRequest` model; `apply_job` body)
- Test: `tests/test_apply_conflict_strategy.py`

**Interfaces:**
- Consumes: `fileops._trash` (overwrite capture), `fileops.dedupe_dest` (B2).
- Produces: `RenameService.apply(job_id, automatic=False, conflict_strategy=None)`;
  `queue_apply(ids=None, confident_only=False, conflict_strategy=None)`.
  `conflict_strategy ∈ {None,'overwrite','keep_both','skip'}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_apply_conflict_strategy.py
# Uses the project's RenameService test harness (see tests/test_rename_service.py
# for construction of a service with a temp DB + configured library roots).
import os
from backend.rename import fileops

def _make_conflict(svc, db, tmp_path):
    src = tmp_path / "incoming" / "New.2160p.DV.mkv"
    src.parent.mkdir(parents=True); src.write_bytes(b"NEW")
    dst_dir = tmp_path / "lib"; dst_dir.mkdir()
    existing = dst_dir / "Movie (2024).mkv"; existing.write_bytes(b"OLD")
    jid = db.create_rename_job(original_path=str(src), original_filename="New.2160p.DV.mkv",
        new_filename="Movie (2024).mkv", destination_path=str(dst_dir),
        status="needs_review", match_confidence=100)
    return jid, src, existing

def test_overwrite_trashes_existing_then_places(svc, db, tmp_path):
    jid, src, existing = _make_conflict(svc, db, tmp_path)
    out = svc.apply(jid, conflict_strategy="overwrite")
    assert out["ok"] is True
    assert os.path.exists(str(existing))            # dst path occupied by the NEW file
    assert open(existing, "rb").read() == b"NEW"    # it's the incoming content
    # the OLD file is in trash, not deleted
    roots = fileops.trash_roots_for_test(tmp_path)   # or list_trash_entries over tmp_path
    assert any(e["original_path"] == str(existing) for e in fileops.list_trash_entries(roots))

def test_keep_both_dedupes_and_rewrites_new_filename(svc, db, tmp_path):
    jid, src, existing = _make_conflict(svc, db, tmp_path)
    out = svc.apply(jid, conflict_strategy="keep_both")
    assert out["ok"] is True
    assert os.path.exists(str(existing))            # original untouched
    job = db.get_rename_job(jid)
    assert job["new_filename"].endswith("(1).mkv")  # rewritten to the deduped name
    assert os.path.exists(os.path.join(job["destination_path"], job["new_filename"]))

def test_skip_leaves_job_unplaced(svc, db, tmp_path):
    jid, src, existing = _make_conflict(svc, db, tmp_path)
    out = svc.apply(jid, conflict_strategy="skip")
    assert out["ok"] is False
    assert open(existing, "rb").read() == b"OLD"    # unchanged
    assert db.get_rename_job(jid)["status"] == "needs_review"

def test_default_none_holds_for_review(svc, db, tmp_path):
    jid, src, existing = _make_conflict(svc, db, tmp_path)
    out = svc.apply(jid)                              # no strategy → today's behavior
    assert out["ok"] is False
    assert "already exists" in out["error"]
```

*(If the harness exposes trash roots differently, assert non-deletion by checking `existing`'s bytes are `b"NEW"` for overwrite and that a `.scanhound-trash` bucket under `tmp_path` contains a file — the key invariant is "old content is recoverable, not gone.")*

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_apply_conflict_strategy.py -v --timeout=60`
Expected: FAIL (`apply` has no `conflict_strategy`).

- [ ] **Step 3: Implement**

`service.py` — change the `apply` signature and branch inside the collision guard (`if os.path.lexists(dst):`, line 1224). Replace the guard body:

```python
    def apply(self, job_id, automatic=False, conflict_strategy=None):
        ...
        if os.path.lexists(dst):
            # Same-inode re-apply (already hardlinked) is a no-op success, not a
            # conflict — never trash a file onto itself.
            try:
                if os.path.samefile(src, dst):
                    db.update_rename_job(job_id, status="applied", processed_at=_now())
                    self._broadcast(job_id)
                    return {"ok": True, "already": True}
            except OSError:
                pass
            if conflict_strategy == "overwrite":
                _fileops._trash(dst)                       # recoverable, same-volume
                # fall through to place_file (dst is now free)
            elif conflict_strategy == "keep_both":
                new_dst = _fileops.dedupe_dest(dst)
                db.update_rename_job(job_id,
                    new_filename=os.path.basename(new_dst))
                dst = new_dst
            else:
                # None → hold for review (existing behavior); 'skip' → same, explicit.
                msg = f"A file already exists at the destination: {dst}"
                try:
                    msg += (f" (existing {os.path.getsize(dst)} bytes vs. candidate "
                            f"{os.path.getsize(src)} bytes)")
                except OSError:
                    pass
                msg += " — review to replace or keep the existing file."
                existing = job.get("warning_message")
                combined = f"{existing}; {msg}" if existing else msg
                db.update_rename_job(job_id, status="needs_review",
                                     warning_message=combined)
                self._broadcast(job_id)
                return {"ok": False, "error": msg}
```

`queue_apply` — thread the strategy to the worker's `apply` call:

```python
    def queue_apply(self, ids=None, confident_only=False, conflict_strategy=None):
        ...
        def _worker(job_ids):
            ...
                    try:
                        self.apply(jid, conflict_strategy=conflict_strategy)
```

`routes/rename.py` — add the optional body and thread it:

```python
from fastapi import Body

class ApplyRequest(BaseModel):
    conflict_strategy: Optional[Literal["overwrite", "keep_both", "skip"]] = None

@router.post("/jobs/{job_id}/apply")
def apply_job(job_id: int, body: ApplyRequest = Body(default=ApplyRequest()),
              reg: ServiceRegistry = Depends(get_registry)):
    out = _service(reg).queue_apply([job_id], conflict_strategy=body.conflict_strategy)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Apply failed"))
    if not out.get("queued"):
        raise HTTPException(status_code=400,
                            detail="Job is not applicable (already applied or in progress)")
    return out
```

Add `from typing import Literal` and `from fastapi import Body` to the imports if not present.

**Note:** `queue_apply` marks a `needs_review` job `applying` before the worker runs. `'skip'` returns `ok:False` from `apply`, so the worker's crash handler is NOT triggered, but the job would be left `applying`. Guard it: for `conflict_strategy == 'skip'`, short-circuit in `apply` BEFORE placement to restore `needs_review` (the branch above already sets `needs_review`), which the worker's per-job `apply` call handles. Verify `test_skip_leaves_job_unplaced` ends in `needs_review`, not `applying`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_apply_conflict_strategy.py tests/test_rename_service.py -v --timeout=60`
Expected: PASS (new strategy tests green; existing apply tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py backend/api/routes/rename.py tests/test_apply_conflict_strategy.py
git commit -m "feat(rename): apply conflict_strategy overwrite/keep_both/skip (trash-not-delete)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B5: Undo restores an overwritten original

**Files:**
- Modify: `backend/rename/service.py` (`undo`, ~1304)
- Test: `tests/test_apply_conflict_strategy.py` (add)

**Interfaces:**
- Consumes: `fileops.undo_place`, `fileops.list_trash_entries`, `fileops.restore_trash_entry`.

- [ ] **Step 1: Write the failing test**

```python
def test_undo_of_overwrite_restores_original(svc, db, tmp_path):
    jid, src, existing = _make_conflict(svc, db, tmp_path)
    svc.apply(jid, conflict_strategy="overwrite")
    assert open(existing, "rb").read() == b"NEW"
    svc.undo(jid)
    assert open(existing, "rb").read() == b"OLD"   # original restored from trash
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_apply_conflict_strategy.py::test_undo_of_overwrite_restores_original -v --timeout=60`
Expected: FAIL (undo leaves the NEW file / doesn't restore OLD).

- [ ] **Step 3: Implement**

In `undo`, after `undo_place` frees `dst`, attempt to restore the most-recent trash entry whose `original_path` casefold-equals `dst`:

```python
        # After undo_place removes the placed file, dst is free — if this apply
        # overwrote a prior file (captured in trash), restore it so undo is
        # symmetric and no data is stranded.
        try:
            from backend.rename import fileops as _fo
            dst_key = os.path.normcase(os.path.abspath(dst))
            roots = _fo.trash_roots(dst)  # helper that yields the file's volume trash + app-data root
            cands = [e for e in _fo.list_trash_entries(roots)
                     if e.get("original_path")
                     and os.path.normcase(os.path.abspath(e["original_path"])) == dst_key
                     and e.get("restorable")]
            cands.sort(key=lambda e: e.get("trashed_at") or "", reverse=True)
            if cands:
                _fo.restore_trash_entry(cands[0]["bucket"], cands[0]["name"], roots)
        except Exception:
            logger.exception("undo: overwrite-original restore best-effort failed")
```

If a `trash_roots(path)` accessor does not already exist, add a tiny public helper in `fileops.py` returning `[_trash_root_for(path), _TRASH_ROOT]` (dedup handled by `list_trash_entries`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_apply_conflict_strategy.py -v --timeout=60`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py backend/rename/fileops.py tests/test_apply_conflict_strategy.py
git commit -m "feat(rename): undo of an overwrite restores the displaced original from trash

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B6: `conflict_preview` service + endpoint

**Files:**
- Modify: `backend/rename/service.py` (add `conflict_preview`)
- Modify: `backend/api/routes/rename.py` (add route)
- Test: `tests/test_conflict_preview.py`

**Interfaces:**
- Consumes: `mediainfo.probe_specs` (B1), `conflicts.rank_conflict` (B3).
- Produces: `conflict_preview(job_id) -> dict` = `{existing: spec|None, incoming: spec, recommended, reason}`;
  `POST /rename/jobs/{id}/conflict-preview` returning that dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conflict_preview.py
def test_conflict_preview_existing_vs_free(svc, db, tmp_path, monkeypatch):
    # Stub probe_specs so no real ffprobe is needed.
    from backend.rename import service as svcmod
    specs = {
        "/inc/new.mkv": {"present": True, "resolution": "2160p", "hdr": "Dolby Vision",
                         "dv_layer": "fel", "size_bytes": 40_000_000_000,
                         "original_filename": "new.mkv"},
    }
    monkeypatch.setattr(svcmod, "probe_specs",
                        lambda p, **k: specs.get(p, {"present": False, "path": p}))
    # ... create a job whose dst is free → existing.present False, recommended 'incoming'
    out = svc.conflict_preview(jid)
    assert out["existing"]["present"] is False
    assert out["recommended"] == "incoming"

def test_conflict_preview_recommends_existing_dv_over_tag_rich_lower(svc, db, tmp_path, monkeypatch):
    # existing = probed 2160p DV FEL; incoming = probed 1080p. recommended 'existing'.
    ...
    assert svc.conflict_preview(jid)["recommended"] == "existing"

def test_apply_bodyless_still_ok(client):
    # The existing bodyless POST must not 422 after ApplyRequest was added (B4).
    r = client.post("/rename/jobs/999999/apply")
    assert r.status_code in (400,)  # 400 (not applicable), never 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_conflict_preview.py -v --timeout=60`
Expected: FAIL (`conflict_preview` missing).

- [ ] **Step 3: Implement**

`service.py` (import `from backend.rename.mediainfo import probe_specs` and `from backend.rename.conflicts import rank_conflict`):

```python
    def conflict_preview(self, job_id: int) -> dict:
        """Compute a two-file spec comparison for a destination conflict WITHOUT
        persisting. existing = probed dst-on-disk (or {present:False}); incoming =
        probed source. Recommendation judges probed specs (see rank_conflict)."""
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"existing": None, "incoming": None,
                    "recommended": None, "reason": "Job not found"}
        dest_dir = job.get("destination_path") or ""
        dst = os.path.join(dest_dir, job.get("new_filename")
                           or os.path.basename(job.get("original_path") or "")) \
            if dest_dir else None
        incoming = probe_specs(job.get("original_path"), db=db) or {
            "present": os.path.exists(job.get("original_path") or ""),
            "path": job.get("original_path")}
        incoming["original_filename"] = job.get("original_filename")
        incoming["resolution"] = incoming.get("resolution") or job.get("resolution")
        existing = None
        if dst and os.path.lexists(dst):
            existing = probe_specs(dst, db=db) or {"present": True, "path": dst}
            existing["original_filename"] = os.path.basename(dst)
        rec = rank_conflict(existing, {**incoming, "id": job_id})
        return {"existing": existing, "incoming": incoming,
                "recommended": rec["recommended"], "reason": rec["reason"]}
```

`routes/rename.py` (register with the other `/{job_id}/…` routes):

```python
@router.post("/jobs/{job_id}/conflict-preview")
def conflict_preview(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    return _service(reg).conflict_preview(job_id)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_conflict_preview.py -v --timeout=60`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py backend/api/routes/rename.py tests/test_conflict_preview.py
git commit -m "feat(rename): conflict-preview endpoint (two-file spec compare, no persist)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task B7: On-demand FEL/MEL scan for the two conflict files

**Files:**
- Modify: `backend/rename/service.py` (add `scan_conflict_dv`)
- Modify: `backend/api/routes/rename.py` (add route)
- Test: `tests/test_scan_conflict_dv.py`

**Interfaces:**
- Produces: `scan_conflict_dv(job_id) -> dict` (detects both files' DV layers, upserts `dv_scan`);
  `POST /rename/jobs/{id}/scan-dv-conflict` → `{status: 'scanning'}`, background thread, broadcasts `dv:scan_done`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_conflict_dv.py
from unittest.mock import patch

def test_scan_conflict_dv_detects_and_upserts(svc, db, tmp_path):
    # job with an on-disk incoming + existing dst
    with patch("backend.rename.dv_detect.detect_layer", return_value={"layer": "fel"}):
        out = svc.scan_conflict_dv(jid)
    assert out["scanned"] >= 1
    assert db.get_dv_scan(job_incoming_path)["dv_layer"] == "fel"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_scan_conflict_dv.py -v --timeout=60`
Expected: FAIL (`scan_conflict_dv` missing).

- [ ] **Step 3: Implement**

`service.py` (mirror `scan_folder_dv`, but for exactly the two paths; reuse `_dv.detect_layer`, `db.upsert_dv_scan`):

```python
    def scan_conflict_dv(self, job_id: int) -> dict:
        """Detect + cache the DV FEL/MEL layer of a conflict's two files (incoming
        source + existing destination). Reuses dv_detect + the dv_scan cache.
        Intended to run on a background thread (see the route)."""
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"error": "Job not found", "scanned": 0}
        if not _dv.available():
            return {"error": "dovi_tool is not installed in this build", "scanned": 0}
        dest_dir = job.get("destination_path") or ""
        dst = os.path.join(dest_dir, job.get("new_filename")
                           or os.path.basename(job.get("original_path") or "")) \
            if dest_dir else None
        paths = [p for p in (job.get("original_path"), dst)
                 if p and os.path.isfile(p)]
        scanned = 0
        for path in paths:
            try:
                st = os.stat(path)
                if db.dv_scan_is_current(path, st.st_mtime, st.st_size):
                    continue
                layer = _dv.detect_layer(path).get("layer", _dv.LAYER_UNKNOWN)
                title = parse_filename(os.path.basename(path)).get("title") or None
                db.upsert_dv_scan(path, layer, title=title,
                                  sig_mtime=st.st_mtime, sig_size=st.st_size,
                                  source="scan")
                scanned += 1
            except Exception:
                logger.exception("scan_conflict_dv failed on %s", path)
        return {"job_id": job_id, "scanned": scanned}
```

`routes/rename.py` (mirror the `dv-scan-folder` background+broadcast at lines 447-482):

```python
@router.post("/jobs/{job_id}/scan-dv-conflict")
def scan_dv_conflict(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    svc = _service(reg)
    def _run():
        try:
            result = svc.scan_conflict_dv(job_id)
            ws_manager.broadcast_sync({"type": "dv:scan_done", "data": result})
        except Exception:
            logger.exception("scan-dv-conflict failed")
    threading.Thread(target=_run, name="scan-dv-conflict", daemon=True).start()
    return {"status": "scanning", "job_id": job_id}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_scan_conflict_dv.py -v --timeout=60`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py backend/api/routes/rename.py tests/test_scan_conflict_dv.py
git commit -m "feat(rename): on-demand FEL/MEL scan for a conflict's two files

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F1: `review.ts` classification helper

**Files:**
- Create: `frontend/src/lib/renames/review.ts`
- Modify: `frontend/src/routes/renames/+page.svelte` (import `matchesQuery` from `review.ts` instead of the inline copy)
- Test: `frontend/src/lib/renames/review.test.ts`

**Interfaces:**
- Produces: `classifyJob`, `partitionJobs`, `hasDestinationConflict`, `matchesQuery` (all pure; typed against `RenameJob`).

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/renames/review.test.ts
import { describe, it, expect } from 'vitest';
import { classifyJob, partitionJobs, hasDestinationConflict } from './review';
import type { RenameJob } from '$lib/api/types';

const job = (o: Partial<RenameJob>): RenameJob => ({
  id: 1, package_name: null, original_path: '/x', original_filename: 'x', new_filename: 'y',
  destination_path: '/d', status: 'matched', media_type: 'movie', title: 'X', year: 2024,
  season: null, episode: null, tmdb_id: null, imdb_id: null, resolution: '2160p',
  match_confidence: 100, match_source: 'deterministic', move_method: null,
  warning_message: null, error_message: null, plex_sort_title: null, detected_at: null,
  processed_at: null, reverted_at: null, ...o,
}) as RenameJob;

describe('classifyJob', () => {
  it('matched 100, clean → ready', () => expect(classifyJob(job({}))).toBe('ready'));
  it('matched 100 with warning → needsReview',
    () => expect(classifyJob(job({ warning_message: 'A file already exists' }))).toBe('needsReview'));
  it('matched 99 → needsReview', () => expect(classifyJob(job({ match_confidence: 99 }))).toBe('needsReview'));
  it('needs_review → needsReview', () => expect(classifyJob(job({ status: 'needs_review' }))).toBe('needsReview'));
  it('failed → needsReview', () => expect(classifyJob(job({ status: 'failed' }))).toBe('needsReview'));
  it('applied → inactive', () => expect(classifyJob(job({ status: 'applied' }))).toBe('inactive'));
  it('pending → inactive', () => expect(classifyJob(job({ status: 'pending' }))).toBe('inactive'));
});

describe('partitionJobs', () => {
  it('orders needsReview by confidence ascending, nulls first', () => {
    const { needsReview } = partitionJobs([
      job({ id: 1, status: 'needs_review', match_confidence: 80 }),
      job({ id: 2, status: 'needs_review', match_confidence: null }),
      job({ id: 3, status: 'needs_review', match_confidence: 50 }),
    ]);
    expect(needsReview.map((j) => j.id)).toEqual([2, 3, 1]);
  });
});

describe('hasDestinationConflict', () => {
  it('true on already-exists warning', () =>
    expect(hasDestinationConflict(job({ warning_message: 'A file already exists at /d/y' }))).toBe(true));
  it('true on destination_conflict flag', () =>
    expect(hasDestinationConflict(job({ destination_conflict: true } as Partial<RenameJob>))).toBe(true));
  it('false otherwise', () => expect(hasDestinationConflict(job({}))).toBe(false));
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm run test -- review.test`
Expected: FAIL (`review.ts` missing).

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/renames/review.ts
import type { RenameJob } from '$lib/api/types';

export type ReviewBucket = 'ready' | 'needsReview' | 'inactive';

export function classifyJob(job: RenameJob): ReviewBucket {
  const status = job.status;
  if (status === 'applied' || status === 'reverted' || status === 'pending') return 'inactive';
  const conf = job.match_confidence ?? 0;
  const clean = status === 'matched' && conf >= 100 && !job.warning_message && !job.destination_conflict;
  return clean ? 'ready' : 'needsReview';
}

function byConfidenceAsc(a: RenameJob, b: RenameJob): number {
  const av = a.match_confidence, bv = b.match_confidence;
  if (av == null && bv == null) return 0;
  if (av == null) return -1;   // nulls first (most-needing-scrutiny lead)
  if (bv == null) return 1;
  return av - bv;
}

export function partitionJobs(jobs: RenameJob[]): { ready: RenameJob[]; needsReview: RenameJob[] } {
  const ready: RenameJob[] = [], needsReview: RenameJob[] = [];
  for (const j of jobs) {
    const b = classifyJob(j);
    if (b === 'ready') ready.push(j);
    else if (b === 'needsReview') needsReview.push(j);
  }
  needsReview.sort(byConfidenceAsc);
  ready.sort(byConfidenceAsc);
  return { ready, needsReview };
}

export function hasDestinationConflict(job: RenameJob): boolean {
  if (job.destination_conflict) return true;
  return /already exists/i.test(job.warning_message ?? '');
}

export function matchesQuery(j: RenameJob, q: string): boolean {
  if (!q) return true;
  const hay = `${j.title ?? ''} ${j.original_filename ?? ''} ${j.new_filename ?? ''}`.toLowerCase();
  return hay.includes(q.toLowerCase());
}
```

Then in `+page.svelte`, delete the inline `matchesQuery` function and `import { matchesQuery } from '$lib/renames/review'`.

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && npm run test -- review.test && npm run check`
Expected: PASS; no new type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/renames/review.ts frontend/src/lib/renames/review.test.ts frontend/src/routes/renames/+page.svelte
git commit -m "feat(renames): review.ts classification helper (classifyJob/partitionJobs)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F2: Types + API client + store

**Files:**
- Modify: `frontend/src/lib/api/types.ts` (add `FileSpec`, `ConflictComparison`)
- Modify: `frontend/src/lib/api/client.ts` (add `conflictPreview`, `scanConflictDv`; extend `applyRename`)
- Modify: `frontend/src/lib/stores/renames.ts` (`applyJob(id, strategy?)`)
- Test: `frontend/src/lib/api/client.test.ts` (or extend an existing client test)

**Interfaces:**
- Produces: `api.conflictPreview(id): Promise<ConflictComparison>`;
  `api.scanConflictDv(id): Promise<{status:string}>`;
  `api.applyRename(id, body?: { conflict_strategy?: 'overwrite'|'keep_both'|'skip' })`;
  `applyJob(id: number, strategy?: 'overwrite'|'keep_both'|'skip')`.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/api/client.test.ts (add cases)
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api } from './client';

describe('conflict apis', () => {
  beforeEach(() => { vi.restoreAllMocks(); });
  it('applyRename sends conflict_strategy body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await api.applyRename(5, { conflict_strategy: 'overwrite' });
    const [, opts] = fetchMock.mock.calls[0];
    expect(JSON.parse(opts!.body as string)).toEqual({ conflict_strategy: 'overwrite' });
  });
  it('applyRename with no body sends none', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await api.applyRename(5);
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts!.body).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm run test -- client.test`
Expected: FAIL (`applyRename` ignores a body arg).

- [ ] **Step 3: Implement**

`types.ts` (beside `RematchPreviewResponse`):

```ts
export interface FileSpec {
  present: boolean; path: string | null; size_bytes: number | null;
  resolution: string | null; video_codec: string | null; hdr: string | null;
  dv_layer: string | null; audio: string | null;
  duration_min: number | null; bitrate: number | null;
}
export interface ConflictComparison {
  existing: FileSpec | null;
  incoming: FileSpec;
  recommended: 'existing' | 'incoming' | 'tie' | null;
  reason: string | null;
}
```

`client.ts`:

```ts
  applyRename: (id: number, body?: { conflict_strategy?: 'overwrite' | 'keep_both' | 'skip' }) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}/apply`,
      { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  conflictPreview: (id: number) =>
    request<ConflictComparison>(`/rename/jobs/${id}/conflict-preview`, { method: 'POST' }),
  scanConflictDv: (id: number) =>
    request<{ status: string }>(`/rename/jobs/${id}/scan-dv-conflict`, { method: 'POST' }),
```
(Import `ConflictComparison` into `client.ts`'s type imports.)

`stores/renames.ts`:

```ts
export async function applyJob(id: number, strategy?: 'overwrite' | 'keep_both' | 'skip') {
  await api.applyRename(id, strategy ? { conflict_strategy: strategy } : undefined);
  await refresh();
}
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && npm run test -- client.test && npm run check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts frontend/src/lib/stores/renames.ts frontend/src/lib/api/client.test.ts
git commit -m "feat(renames): conflict preview/scan/apply-strategy client + types + store

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F3: `RenameReviewCard.svelte`

**Files:**
- Create: `frontend/src/lib/components/renames/RenameReviewCard.svelte`
- Test: `frontend/src/lib/components/renames/RenameReviewCard.test.ts`

**Interfaces:**
- Consumes: `RenameJob`, `busy: boolean`, `hasDestinationConflict`/`classifyJob` (F1), `api.conflictPreview`/`api.scanConflictDv` (F2), `confidenceVariant`/DV badge map (`$lib/constants`).
- Produces (callback props): `onApply`, `onOverwrite`, `onKeepBoth`, `onSkip`, `onRematch`, `onReidentify`, `onAcceptCombined`, `onAcceptCorrection`, `onRemove`.

**Behavior (complete spec):**
- Runes props: `let { job, busy = false, onApply, onOverwrite, onKeepBoth, onSkip, onRematch, onReidentify, onAcceptCombined, onAcceptCorrection, onRemove } = $props();`
- Header: poster (`job.poster_url`, fallback box), `title (year)`, big confidence via `confidenceVariant(Math.round(job.match_confidence))`, `match_source`, `dv_layer` badge. Confidence is a `<button>` toggling a `reasonsOpen` block that lists `job.match_reasons`.
- From→To: two monospace, wrapped rows (`break-all`).
- `let conflict = $derived(hasDestinationConflict(job));`
- **No conflict:** an Apply button → `onApply()`.
- **Conflict:** the compare view:
  - `let preview = $state<ConflictComparison | null>(null); let previewSeq = 0; let previewError = $state<string | null>(null);`
  - `$effect(() => { if (conflict) loadPreview(); });` — but guard against refetch loops: only fetch when `job.id` changes or `preview` is null. Use the `previewSeq` guard exactly like `RematchModal.loadPreview`:
    ```ts
    async function loadPreview() {
      const seq = ++previewSeq;
      previewError = null;
      try {
        const p = await api.conflictPreview(job.id);
        if (seq !== previewSeq) return;
        preview = p;
      } catch (e) {
        if (seq !== previewSeq) return;
        previewError = e instanceof Error ? e.message : String(e);
      }
    }
    ```
  - Render a two-column table (Existing | Incoming) over rows: Resolution, HDR/DV, Video, Audio, Bitrate, Size, Duration. A small helper formats each `FileSpec` field (size via a `formatBytes`, bitrate as `Mbps`). Emphasize the better cell per row (simple: highlight the recommended column). Show `preview.reason` + a "★ Recommended keep" chip on `preview.recommended` column. If `preview.existing?.present === false`, show "destination is free" and just the Apply/Overwrite path collapses to Apply.
  - **DV layer row:** if a side is `hdr === 'Dolby Vision'` and `dv_layer == null`, show "Dolby Vision" + a **Scan DV layers** button → `scanDv()`:
    ```ts
    let dvScanning = $state(false);
    async function scanDv() { dvScanning = true; try { await api.scanConflictDv(job.id); } catch {} }
    ```
    Subscribe to the store's DV event: reuse the existing `dv:scan_done` handling by re-fetching on a store signal. Simplest: export a `dvScanTick` store bumped by the WS handler in `stores/renames.ts` (see note) and `$effect(() => { if ($dvScanTick && conflict) loadPreview(); dvScanning = false; });`.
  - Actions: **Overwrite** (danger) → `onOverwrite()`; **Keep both** → `onKeepBoth()`; **Skip** → `onSkip()`.
- Secondary actions (always): **Rematch** `onRematch()`; **Re-identify** (`needs_review`/`failed`) `onReidentify()`; **Accept {code}** when `job.combined_episode`/`job.suggested_correction`; **Remove** `onRemove()`. All disabled while `busy`.
- Style: mirror the existing `RenameCard.svelte` / `BadgeCluster.svelte` classes (`var(--…)` tokens, tailwind) so it matches the app. No new design system.

**Store note (WS → DV tick):** in `stores/renames.ts`, in the existing WS message handler add — on `type === 'dv:scan_done'` — `dvScanTick.update((n) => n + 1);` and `export const dvScanTick = writable(0);`. (One line; keep the existing DV-scan handling intact.)

- [ ] **Step 1: Write the failing test**

```ts
// RenameReviewCard.test.ts
import { render, screen } from '@testing-library/svelte';
import { vi } from 'vitest';
import RenameReviewCard from './RenameReviewCard.svelte';
// mock api.conflictPreview
vi.mock('$lib/api/client', () => ({ api: {
  conflictPreview: vi.fn().mockResolvedValue({
    existing: { present: true, resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'fel',
      size_bytes: 40e9, video_codec: 'HEVC', audio: 'TrueHD 7.1', bitrate: 46e6,
      duration_min: 120, path: '/d/y' },
    incoming: { present: true, resolution: '1080p', hdr: null, dv_layer: null,
      size_bytes: 8e9, video_codec: 'H.264', audio: 'EAC3 5.1', bitrate: 10e6,
      duration_min: 120, path: '/x' },
    recommended: 'existing', reason: 'Existing: 2160p · Dolby Vision' }) } }));

it('conflict card fetches preview and offers Overwrite/Keep both', async () => {
  const onOverwrite = vi.fn();
  render(RenameReviewCard, { job: /* a needs_review job with an already-exists warning */,
    onOverwrite, /* other noop callbacks */ });
  expect(await screen.findByText(/Overwrite/i)).toBeInTheDocument();
  expect(screen.getByText(/Keep both/i)).toBeInTheDocument();
});

it('non-conflict card shows plain Apply, no compare fetch', async () => {
  render(RenameReviewCard, { job: /* matched 100, no warning */, /* callbacks */ });
  expect(screen.getByText(/^Apply$/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm run test -- RenameReviewCard`
Expected: FAIL (component missing).

- [ ] **Step 3: Implement the component** per the Behavior spec above, mirroring `RenameCard.svelte`/`BadgeCluster.svelte` for markup/classes.

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && npm run test -- RenameReviewCard && npm run check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/renames/RenameReviewCard.svelte frontend/src/lib/components/renames/RenameReviewCard.test.ts frontend/src/lib/stores/renames.ts
git commit -m "feat(renames): RenameReviewCard with two-file compare + overwrite/keep-both

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F4: `RenameReviewDeck.svelte`

**Files:**
- Create: `frontend/src/lib/components/renames/RenameReviewDeck.svelte`
- Test: `frontend/src/lib/components/renames/RenameReviewDeck.test.ts`

**Interfaces:**
- Props: `let { jobs, initialScope = 'needsReview', onClose } = $props();`
- Consumes: `partitionJobs` (F1), `RenameReviewCard` (F3), `RematchModal`, store actions `applyJob`/`deleteJob`/`acceptCombinedJob`/`acceptCorrectionJob` + `api.reidentifyRename`, `gestures.ts` (swipe), `addToast`.

**Behavior:**
- `let scope = $state(initialScope); let index = $state(0); let busyId = $state<number | null>(null); let rematchJob = $state<RenameJob | null>(null);`
- `let queue = $derived(scope === 'needsReview' ? partitionJobs(jobs).needsReview : [...partitionJobs(jobs).ready, ...partitionJobs(jobs).needsReview]);`
- Clamp: `$effect(() => { if (index >= queue.length) index = Math.max(0, queue.length - 1); });`
- `let current = $derived(queue[index] ?? null);`
- Header: close (×) → `onClose()`; segmented scope toggle (`Under 100%` / `All` with counts); `index+1 / queue.length`.
- Nav: prev/next buttons (guard bounds); horizontal swipe via `gestures.ts` (mirror the Scan `SwipeDeck`/`SwipeableTile` usage) → prev/next.
- Action wrappers (mirror the page's `run()`): set `busyId = current.id`, await, `addToast`, clear; on a resolving action (`apply`/`overwrite`/`keepBoth`/`remove`/`accept*`) **auto-advance**: the `rename:job` WS upsert removes it from `jobs` (reactive), so `queue` shrinks; keep `index` (now points at the next item) or clamp.
  ```ts
  async function act(fn: () => Promise<void>, ok: string) {
    if (!current) return; busyId = current.id;
    try { await fn(); addToast('Renames', ok); }
    catch (e) { addToast('Renames', e instanceof Error ? e.message : 'Action failed', 'error'); }
    finally { busyId = null; }
  }
  ```
- Skip: `index = Math.min(index + 1, queue.length - 1)` (no server call).
- Completion: when `queue.length === 0` show "All reviewed" + a Done button (`onClose`) and, if `partitionJobs(jobs).ready.length`, an Apply-all shortcut (`api.bulkApply(readyIds)`).
- Render `RenameReviewCard` for `current`, wiring each callback to an `act(...)`:
  - `onApply={() => act(() => applyJob(current!.id), 'Applied')}`
  - `onOverwrite={() => act(() => applyJob(current!.id, 'overwrite'), 'Overwriting…')}`
  - `onKeepBoth={() => act(() => applyJob(current!.id, 'keep_both'), 'Keeping both')}`
  - `onSkip={skip}`
  - `onReidentify={() => act(async () => { await api.reidentifyRename(current!.id); await refreshRenames(); }, 'Re-identifying')}`
  - `onAcceptCombined`/`onAcceptCorrection` → the store actions
  - `onRemove={() => act(() => deleteJob(current!.id), 'Removed')}`
  - `onRematch={() => (rematchJob = current)}`
- `{#if rematchJob}<RematchModal job={rematchJob} onClose={() => { rematchJob = null; }} />{/if}` (RematchModal already calls `refreshRenames()`).
- Full-screen overlay: `fixed inset-0 z-50`, safe-area padding — mirror `DetailSheet.svelte`.

- [ ] **Step 1: Write the failing test**

```ts
// RenameReviewDeck.test.ts — queue derivation + auto-advance + completion
it('derives the needsReview queue and shows position', () => { /* render, expect '1 / N' */ });
it('scope toggle to All includes ready items', () => { /* toggle, expect longer queue */ });
it('shows completion state when queue empty', () => { /* jobs=[], expect 'All reviewed' */ });
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npm run test -- RenameReviewDeck` → FAIL.
- [ ] **Step 3: Implement** per Behavior.
- [ ] **Step 4: Run** — `cd frontend && npm run test -- RenameReviewDeck && npm run check` → PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/renames/RenameReviewDeck.svelte frontend/src/lib/components/renames/RenameReviewDeck.test.ts
git commit -m "feat(renames): RenameReviewDeck one-at-a-time review with scope + swipe

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F5: `MobileRenamesView.svelte`

**Files:**
- Create: `frontend/src/lib/components/renames/MobileRenamesView.svelte`
- Test: `frontend/src/lib/components/renames/MobileRenamesView.test.ts`

**Interfaces:**
- Consumes: `renameJobs`, `renameQuery`, `renameQueue`, `refreshRenames`, `api.bulkApply`, `partitionJobs`/`matchesQuery` (F1), `RenameReviewDeck` (F4), plus the kept `RenamesHeader`, DV surface, `TrashPanel`.

**Behavior:**
- `let deckOpen = $state(false); let scope = $state<'needsReview'|'all'>('needsReview');`
- `let filtered = $derived($renameJobs.filter((j) => matchesQuery(j, $renameQuery)));`
- `let parts = $derived(partitionJobs(filtered));`
- Keep the `$renameQueue` apply-progress banner (copy the markup from `+page.svelte`).
- Search input bound to `$renameQuery`.
- Ready card: `parts.ready.length` + "Apply all" → `await api.bulkApply(parts.ready.map((j) => j.id)); refreshRenames();` (hidden when 0).
- Needs-review card: `parts.needsReview.length` + "Review" → `scope = 'needsReview'; deckOpen = true;` (hidden when 0).
- Scope toggle: `Under 100% · {parts.needsReview.length}` / `All · {parts.ready.length + parts.needsReview.length}` — sets `scope`.
- Empty states: no jobs → "No rename jobs yet…"; none needing review → "All clear — N ready" + Apply-all.
- Below: `<RenamesHeader … />`, the DV scan surface, `<TrashPanel />` (import + render as the desktop page does; pass the same props).
- `{#if deckOpen}<RenameReviewDeck jobs={filtered} initialScope={scope} onClose={() => (deckOpen = false)} />{/if}`

- [ ] **Step 1: Failing test**

```ts
// MobileRenamesView.test.ts
it('shows ready + needs-review counts and Apply-all wires bulkApply', async () => { /* ... */ });
it('Review opens the deck', async () => { /* ... */ });
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per Behavior; mirror existing renames component classes.
- [ ] **Step 4: Run** — `cd frontend && npm run test -- MobileRenamesView && npm run check` → PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/renames/MobileRenamesView.svelte frontend/src/lib/components/renames/MobileRenamesView.test.ts
git commit -m "feat(renames): MobileRenamesView summary hero + review deck entry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F6: Fork the route on `isPhone`

**Files:**
- Modify: `frontend/src/routes/renames/+page.svelte`

**Interfaces:**
- Consumes: `isPhone` (`$lib/stores/viewport`), `MobileRenamesView` (F5).

- [ ] **Step 1: Implement the fork**

Import `import { isPhone } from '$lib/stores/viewport';` and `import MobileRenamesView from '$lib/components/renames/MobileRenamesView.svelte';`. Keep the existing `onMount` loaders. Wrap the review chrome — the block containing `StatusDashboard`, `RenameFilterBar`, `BulkBar`, and the grid/list `{#if shown.length === 0}…{:else if $viewMode === 'grid'}…{:else}…{/if}` — so:

```svelte
{#if $isPhone}
  <MobileRenamesView />
{:else}
  <StatusDashboard {statusFilter} onFilter={(s) => (statusFilter = s)} />
  <RenameFilterBar />
  <BulkBar {shownIds} />
  <!-- existing grid/list block unchanged -->
{/if}
```

Leave `RenamesHeader`, the `$renameQueue` banner, the DV scan surface, and `TrashPanel` OUTSIDE the fork (shared) OR, if `MobileRenamesView` renders its own copies, ensure they are not double-rendered on phone — prefer: keep `RenamesHeader` + `$renameQueue` banner shared at the top; move the DV surface + `TrashPanel` INTO the `{:else}` branch and let `MobileRenamesView` render its own (as specified in F5). Verify no duplicate DV/Trash panels on phone.

- [ ] **Step 2: Typecheck + build**

Run: `cd frontend && npm run check && npm run build`
Expected: PASS, clean build.

- [ ] **Step 3: Manual smoke (desktop unchanged)** — load `/renames` at desktop width: identical to before. (No automated step; visual.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/routes/renames/+page.svelte
git commit -m "feat(renames): fork /renames to MobileRenamesView on isPhone

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F7: Full verification + deploy

**Files:** none (verification only)

- [ ] **Step 1: Backend suite (changed modules)**

Run: `pytest tests/test_mediainfo.py tests/test_fileops_dedupe.py tests/test_conflicts_rank.py tests/test_apply_conflict_strategy.py tests/test_conflict_preview.py tests/test_scan_conflict_dv.py tests/test_rename_service.py -v --timeout=60`
Expected: all PASS (including the untouched `test_rename_service.py` ranking tests).

- [ ] **Step 2: Frontend suite + typecheck + build**

Run: `cd frontend && npm run test && npm run check && npm run build`
Expected: all PASS, clean build.

- [ ] **Step 3: Deploy**

Run: `docker compose up -d --build`
Expected: container rebuilds and comes up healthy.

- [ ] **Step 4: Device checklist (phone viewport / real phone)**
  - `/renames` shows the summary hero (ready + needs-review counts); Apply-all clears the 100% ones.
  - Review opens the full-screen deck; swipe / arrows move; scope toggle switches Under-100%/All.
  - A conflict card shows the two-column spec table with a recommended keeper; Overwrite moves the old file to Trash (verify it appears in the Trash panel and the new file is in place); Keep-both adds a `(1)` version; Skip advances.
  - "Scan DV layers" appears when a DV file's layer is uncached; after it runs, FEL/MEL fills in.
  - Desktop `/renames` is visually unchanged.

- [ ] **Step 5: Changelog + version bump** — add an entry to `frontend/src/lib/changelog.ts` (next version) summarizing the mobile Renames review + conflict resolution, and commit.

```bash
git add frontend/src/lib/changelog.ts
git commit -m "chore: changelog — mobile Renames review + conflict resolution

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes (author)

- **Spec coverage:** probe (B1), keep-both dedupe (B2), spec-aware ranking + trap (B3), overwrite/keep-both/skip apply with trash-not-delete (B4), symmetric undo (B5), conflict-preview endpoint (B6), on-demand FEL/MEL (B7); review.ts (F1), types/client/store (F2), card+compare (F3), deck (F4), summary view (F5), fork (F6), verify+deploy (F7). All spec sections map to a task.
- **Type consistency:** `conflict_strategy` values `overwrite|keep_both|skip` are identical across `ApplyRequest`, `applyRename`, `applyJob`. `FileSpec`/`ConflictComparison` fields match `probe_specs` + `conflict_preview` output.
- **Data safety:** overwrite trashes (B4) and undo restores (B5); tests assert non-deletion.
- **Backward compat:** `apply_job` body is `Body(default=ApplyRequest())`; F2 test asserts bodyless apply sends no body and B6 test asserts no 422.
- **Regression guard:** B3 keeps the untouched `test_rename_service.py` ranking tests green (new tuple slot is 0 for filename-only jobs).
