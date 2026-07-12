# Richer Audio Profile + HDR10+ Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `probe_specs()` detects Atmos/DTS sub-profiles (via track-title/profile fields) and HDR10+ (via a cheap frame-level ffprobe call); `_quality_score()`'s existing `hdr`/`audio` tiers become probed-data-first (filename-regex fallback unchanged) so this new data influences duplicate-comparison ranking.

**Architecture:** Two backend changes: (1) `mediainfo.py`'s `probe_specs()` gains richer audio detection (existing ffprobe call, no extra cost) plus one new lightweight frame-level ffprobe call for HDR10+ (skipped when Dolby Vision is already detected). (2) `conflicts.py`'s `_quality_score()` reads the new probed fields first, falling back to today's exact filename-regex logic when absent — tuple length/position unchanged (7-tuple, same order), only the VALUE RANGE of `hdr` (0/1 → 0/1/2) and the SOURCE of `audio` (filename-only → probed-first) change.

**Tech Stack:** Python (ffprobe subprocess), pytest.

## Global Constraints

- Verified technical grounding (from real files in this deployment's library, not assumed): Atmos/DTS sub-profile signal lives in `audio.tags.title` (e.g. `"TrueHD 7.1 Atmos"`) and sometimes `audio.profile` — NOT reliably in `codec_name` alone. HDR10+ requires a SEPARATE frame-level probe (`-show_frames -read_intervals '%+#1' -select_streams v:0`) — the stream-level probe never surfaces it. Measured cost: stream probe 0.09s, frame probe 0.1s (both on a real 4K file) — cheap, run unconditionally except when DV is already detected.
- `_quality_score`'s tuple `(res_rank, dv, dv_layer_rank, hdr, source, audio, edition)` — length and position MUST stay unchanged (existing regression tests index into positions). Only `hdr`'s value range and `audio`'s data source change.
- A Dolby Vision file's `hdr` value must stay `1` (unchanged) — DV's own precedence is carried by the separate `dv`/`dv_layer_rank` fields ahead of `hdr`; `hdr=2` is reserved for probed/filename HDR10+ ONLY, never double-counted with DV.
- Existing test `test_probe_specs_parses_ffprobe` mocks a SINGLE `subprocess.run` call returning a stream-shaped JSON with no `"frames"` key — the new frame-probe code path MUST tolerate this gracefully (empty/missing `frames` → no HDR10+ found, not a crash), so this existing test keeps passing unmodified. Verify this explicitly.
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, docker cp in, pip install pytest, run, docker rm -f). Work on `main`, commit only when green.
- **Mandatory adversarial rigor** (per this project's established, hard-won practice for `_quality_score` changes): after implementation, a dedicated review pass on the most capable available model MUST re-trace, by execution: a DV file's hdr/dv values unchanged; an HDR10+ file outranks otherwise-identical plain HDR10; an Atmos-tagged file (via track title only, no filename hint) outranks otherwise-identical non-Atmos; a file with zero probed data falls back to byte-identical filename-regex behavior (full `test_conflicts_rank.py` regression run).

---

### Task 1: `probe_specs()` — richer audio detection + HDR10+ frame probe

**Files:**
- Modify: `backend/rename/mediainfo.py`
- Test: `tests/test_mediainfo.py`

**Interfaces:**
- Produces: `probe_specs()`'s returned dict gains `audio_profile: str | None` (e.g. `"TrueHD 7.1 Atmos"`, `"DTS-HD MA 5.1"`); `hdr` can now be `"HDR10+"` (new possible value) alongside existing `"Dolby Vision"`/`"HDR10"`/`"HLG"`/`None`. Existing `audio`/`resolution`/`video_codec`/`dv_layer` fields unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mediainfo.py`:
```python
def test_probe_specs_detects_atmos_from_track_title(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "bt709"},
            {"codec_type": "audio", "codec_name": "truehd", "channels": 8,
             "channel_layout": "7.1", "tags": {"title": "TrueHD 7.1 Atmos"}},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert s["audio_profile"] == "TrueHD 7.1 Atmos"

def test_probe_specs_detects_dts_hd_profile(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "dts", "channels": 6,
             "channel_layout": "5.1", "profile": "DTS-HD MA"},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert "DTS-HD MA" in (s["audio_profile"] or "")

def test_probe_specs_no_atmos_no_special_profile_returns_none_audio_profile(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo"},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert s["audio_profile"] is None

def test_probe_specs_detects_hdr10_plus_via_frame_probe(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "smpte2084"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1"},
        ],
    })
    frame_json = json.dumps({
        "frames": [{"side_data_list": [
            {"side_data_type": "Mastering display metadata"},
            {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
        ]}]
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    fake_frame = MagicMock(returncode=0, stdout=frame_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", side_effect=[fake_stream, fake_frame]):
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "HDR10+"

def test_probe_specs_plain_hdr10_stays_hdr10_when_no_hdr10_plus_metadata(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "smpte2084"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1"},
        ],
    })
    frame_json = json.dumps({"frames": [{"side_data_list": []}]})
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    fake_frame = MagicMock(returncode=0, stdout=frame_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", side_effect=[fake_stream, fake_frame]):
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "HDR10"

def test_probe_specs_dolby_vision_skips_frame_probe_entirely(tmp_path):
    """DV outranks/precludes an HDR10+ frame-probe call — only ONE subprocess.run
    should fire when the stream-level probe already found DOVI side_data."""
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    fake_stream = MagicMock(returncode=0, stdout=FFPROBE_JSON)  # existing DV fixture
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream) as mock_run:
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "Dolby Vision"
    assert mock_run.call_count == 1  # frame probe was skipped

def test_probe_specs_parses_ffprobe_still_passes_with_frame_probe_added(tmp_path):
    """Regression: the ORIGINAL fixture/test (single-mock, no 'frames' key) must
    keep passing once a second subprocess.run call exists in probe_specs — the
    frame-probe path must tolerate a stream-shaped mock gracefully."""
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))
    assert s["present"] is True
    assert s["hdr"] == "Dolby Vision"  # DV path — frame probe skipped, no crash
```

- [ ] **Step 2: Run to verify RED**

Throwaway container: `python3 -m pytest tests/test_mediainfo.py -v`
Expected: the 6 new tests FAIL (no `audio_profile` key / `hdr` never `"HDR10+"` / etc). `test_probe_specs_dolby_vision_skips_frame_probe_entirely` and the regression test may currently PASS trivially (no second call exists yet) — that's fine, they'll stay meaningful once Step 3 lands.

- [ ] **Step 3: Implement**

In `backend/rename/mediainfo.py`:

1. Add near the top, alongside `_AUDIO_LABEL`:
```python
_ATMOS_RE = __import__("re").compile(r"atmos", __import__("re").IGNORECASE)
_DTS_HD_RE = __import__("re").compile(r"dts[-\s]?hd|dts[:\s]?x", __import__("re").IGNORECASE)
```
(Use a proper top-of-file `import re` instead of the inline `__import__` shown above — check whether `re` is already imported in this file first; add it cleanly if not.)

2. After computing `audio_label` (existing code, ~line 147), add:
```python
    audio_signal = f"{acodec or ''} {(audio.get('profile') or '')} {((audio.get('tags') or {}).get('title') or '')}"
    audio_profile = None
    if _ATMOS_RE.search(audio_signal):
        base = acodec or "Audio"
        audio_profile = f"{base} {chans} Atmos".strip() if chans else f"{base} Atmos"
    elif _DTS_HD_RE.search(audio_signal):
        # Prefer the real profile string when ffprobe supplied one (e.g. "DTS-HD MA");
        # otherwise fall back to a generic label.
        audio_profile = (audio.get("profile") or "DTS-HD").strip()
        if chans:
            audio_profile = f"{audio_profile} {chans}"
```

3. After computing `hdr` (existing code, ~line 141), add the frame-level HDR10+ check — ONLY when not already Dolby Vision and the stream-level transfer already indicated HDR10:
```python
    if hdr == "HDR10":
        try:
            fr = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_frames", "-read_intervals", "%+#1",
                 "-select_streams", "v:0", path],
                capture_output=True, text=True, timeout=timeout)
            if fr.returncode == 0:
                frame_data = json.loads(fr.stdout)
                frames = frame_data.get("frames") or []
                for fr_entry in frames:
                    for sd in (fr_entry.get("side_data_list") or []):
                        sdt = str(sd.get("side_data_type", ""))
                        if "HDR10+" in sdt or "SMPTE2094-40" in sdt:
                            hdr = "HDR10+"
                            break
                    if hdr == "HDR10+":
                        break
        except Exception:
            pass  # frame probe failure must never fail the whole probe — stays plain HDR10
```

4. Add `"audio_profile": audio_profile,` to the returned `result` dict (alongside the existing `"audio": audio_label,` line).
5. Add `"audio_profile": None,` to the not-present early-return dict (~line 73-76) for shape consistency.

- [ ] **Step 4: Run to verify GREEN**

Re-copy, run: `python3 -m pytest tests/test_mediainfo.py -v` → all pass (existing + 6 new).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/mediainfo.py tests/test_mediainfo.py
git commit -m "feat(mediainfo): detect Atmos/DTS-HD audio profile + HDR10+ via frame probe"
```

---

### Task 2: `_quality_score()` — probed-first `hdr`/`audio` tiers

**Files:**
- Modify: `backend/rename/conflicts.py`
- Test: `tests/test_conflicts_rank.py`

**Interfaces:**
- Consumes: Task 1's `probe_specs()` output shape (`hdr` can be `"HDR10+"`; new `audio_profile` field) — jobs passed to `_quality_score` may carry these as `job.get('hdr')`/`job.get('audio_profile')` when probed data has been attached to the job dict (mirrors how `dv_layer`/`hdr` are already attached today).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_conflicts_rank.py` (match its existing bare-function-call style, e.g. `conflicts._quality_score(job_dict)`):
```python
def test_quality_score_hdr10_plus_outranks_plain_hdr10():
    base = {"original_filename": "Movie.2024.2160p.mkv", "resolution": "2160p"}
    hdr10 = {**base, "hdr": "HDR10"}
    hdr10_plus = {**base, "hdr": "HDR10+"}
    assert conflicts._quality_score(hdr10_plus) > conflicts._quality_score(hdr10)

def test_quality_score_dolby_vision_hdr_value_unchanged():
    dv = {"original_filename": "Movie.2024.2160p.DV.mkv", "resolution": "2160p",
          "hdr": "Dolby Vision"}
    # hdr tier itself must be 1, not double-counted with the separate dv/dv_layer_rank fields.
    assert conflicts._quality_score(dv)[3] == 1

def test_quality_score_probed_atmos_outranks_non_atmos_same_base_codec():
    base = {"original_filename": "Movie.2024.1080p.mkv", "resolution": "1080p"}
    plain = {**base, "audio_profile": None}
    atmos = {**base, "audio_profile": "TrueHD 7.1 Atmos"}
    assert conflicts._quality_score(atmos) > conflicts._quality_score(plain)

def test_quality_score_probed_dts_hd_ranks_between_ddp_and_atmos():
    base = {"original_filename": "Movie.2024.1080p.mkv", "resolution": "1080p"}
    ddp = {**base, "audio_profile": None}  # filename has no audio hint -> tier 0
    dts_hd = {**base, "audio_profile": "DTS-HD MA 5.1"}
    atmos = {**base, "audio_profile": "TrueHD 7.1 Atmos"}
    assert conflicts._quality_score(atmos) > conflicts._quality_score(dts_hd) > conflicts._quality_score(ddp)

def test_quality_score_no_probed_data_falls_back_to_filename_regex_unchanged():
    """Regression: a job with NO audio_profile/hdr probed fields must score
    byte-identically to today's pure-filename behavior."""
    truehd_filename = {"original_filename": "Movie.2024.1080p.TrueHD.7.1.mkv", "resolution": "1080p"}
    hdr_filename = {"original_filename": "Movie.2024.2160p.HDR.mkv", "resolution": "2160p"}
    assert conflicts._quality_score(truehd_filename)[5] == 3  # audio tier, filename-only path
    assert conflicts._quality_score(hdr_filename)[3] == 1     # hdr tier, filename-only path
```

- [ ] **Step 2: Run to verify RED**

`python3 -m pytest tests/test_conflicts_rank.py -v` → the new tests fail (HDR10+ not distinguished, `audio_profile` not consulted).

- [ ] **Step 3: Implement**

In `backend/rename/conflicts.py`'s `_quality_score()`:

Replace the `hdr` computation:
```python
    explicit_hdr = job.get("hdr")
    if explicit_hdr == "Dolby Vision":
        dv = 1
    if explicit_hdr == "HDR10+":
        hdr = 2
    elif explicit_hdr:
        hdr = 1
    elif _re.search(r"\bhdr10\+|\bhdr10plus\b", name):
        hdr = 2
    elif _re.search(r"\bhdr(10)?(\+|plus)?\b|\bhlg\b", name):
        hdr = 1
    else:
        hdr = 0
```
(Note: the `dv` bit assignment from `explicit_hdr == "Dolby Vision"` must stay BEFORE this block, exactly where it already is in the existing code — only the `hdr` variable's own computation changes.)

Replace the `audio` computation:
```python
    audio_profile = str(job.get("audio_profile") or "").lower()
    if audio_profile:
        if "atmos" in audio_profile or "truehd" in audio_profile:
            audio = 3
        elif "dts-hd" in audio_profile or "dts:x" in audio_profile or "dts hd" in audio_profile:
            audio = 2
        elif "ddp" in audio_profile or "eac3" in audio_profile or "dd+" in audio_profile:
            audio = 1
        else:
            audio = 0
    elif _re.search(r"\b(truehd|atmos)\b", name):
        audio = 3
    elif _re.search(r"\bdts[.\s-]?hd\b|\bdts[.\s-]?x\b", name):
        audio = 2
    elif _re.search(r"\b(ddp|eac3|dd\+)\b", name):
        audio = 1
    else:
        audio = 0
```

- [ ] **Step 4: Run to verify GREEN + full regression**

Re-copy, run: `python3 -m pytest tests/test_conflicts_rank.py tests/test_mediainfo.py tests/test_conflict_analyzer.py -v`
Expected: all pass, including every PRE-EXISTING test in `test_conflicts_rank.py` (byte-identical scoring for anything without new probed fields).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/conflicts.py tests/test_conflicts_rank.py
git commit -m "feat(conflicts): probed-first hdr/audio ranking tiers (HDR10+, Atmos, DTS-HD)"
```

---

### Task 3: Frontend types + Compare modal display

**Files:**
- Modify: `frontend/src/lib/api/types.ts` (`FileSpec` interface, ~line 592)
- Modify: `frontend/src/lib/renames/conflictView.ts` (`specRows()`)
- Test: `frontend/src/lib/renames/conflictView.test.ts`

**Interfaces:**
- Consumes: Task 1's `probe_specs()` shape (`audio_profile`, `hdr` can be `"HDR10+"`).

- [ ] **Step 1: Write the failing test**

Add to `conflictView.test.ts` a case asserting `specRows()` includes an "Audio Profile" row when `audio_profile` is present on either side, and that `hdr` displays `"HDR10+"` distinctly (check the existing `hdrLabel`-equivalent rendering logic — HDR10+ should render as-is, not collapse into generic "HDR10").

- [ ] **Step 2: Run to verify RED**

`cd frontend && npx vitest run src/lib/renames/conflictView.test.ts` → new case fails.

- [ ] **Step 3: Implement**

Add `audio_profile: string | null;` to `FileSpec` in `types.ts`. Add an "Audio Profile" row to `specRows()` in `conflictView.ts`, following the exact pattern of the existing rows (resolution/hdr/video_codec/audio/size) — only rendered when at least one side has a non-null `audio_profile`.

- [ ] **Step 4: Verify + full checks**

```bash
cd frontend && npm run check && npm run build && npx vitest run
```
Grep for curly quotes.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/renames/conflictView.ts frontend/src/lib/renames/conflictView.test.ts
git commit -m "feat(renames): surface audio profile + HDR10+ in the Compare modal"
```

---

### Task 4: Mandatory adversarial re-verification (most capable model)

Per this project's established practice for `_quality_score` changes: dispatch a dedicated adversarial review, independent of Tasks 1-3's own per-task reviews, on the most capable available model. It must EXECUTE (not just read) at minimum:
1. A DV file's `hdr`/`dv` tuple values unchanged before/after this plan (no double-counting).
2. An HDR10+ file outranks an otherwise-identical plain-HDR10 file (real `_quality_score` call, real comparison).
3. An Atmos-tagged file (via probed `audio_profile` only, no filename hint) outranks an otherwise-identical non-Atmos file of the same base codec.
4. A file with zero probed data (`hdr`/`audio_profile` both absent from the job dict) scores byte-identically to pre-plan filename-only behavior — full `test_conflicts_rank.py` suite, every pre-existing test, not just the new ones.
5. The frame-probe skip-when-DV behavior (Task 1) — confirm via `mock_run.call_count` tracing that a DV file never triggers the extra subprocess call.

Fix any findings before considering this plan complete.

## Deployment

Does NOT deploy — joins the queue awaiting a combined deploy after user review.
