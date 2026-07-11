# Flat Movie Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `auto_rename_movie_flat` setting that places a single-file movie directly in the library root instead of a per-movie `Title (Year)/` subfolder; split (multi-part) movies and all TV keep their folders.

**Architecture:** A new default-`False` config flag threaded through `naming.build_target` → `_destination`, which is the single place movie/TV destination directories are computed. Service call sites pass the flag from config. A Settings-page checkbox toggles it. Filename logic is untouched — only the directory changes.

**Tech Stack:** Python (FastAPI backend), SvelteKit 5 (Svelte runes) frontend. Backend tests via pytest in a throwaway `scanhound:latest` container; frontend `npm run check`/`build` on host node.

## Global Constraints

- `auto_rename_movie_flat` defaults to `False` — zero behavior change until the user opts in.
- Flat placement applies to **movies only**; TV always keeps `Show (Year)/Season NN/`.
- A movie with a truthy `meta["part"]` (split/multi-file) keeps its `Title (Year)/` subfolder even when flat is on.
- The **filename** is unchanged in both modes; flat mode changes only the destination directory.
- Backend tests run in a throwaway container (production image has no pytest): `docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. <c>:/app/backend`, `docker cp tests/. <c>:/app/tests`, `docker exec <c> pip install -q pytest`, run, `docker rm -f <c>`. Re-copy `backend/.` and `tests/.` after every edit. Use `docker cp localdir/. <c>:/app/backend` (trailing `/.`) to overwrite contents, never nest.
- Frontend checks run on host node: `cd frontend && npm run check && npm run build`.
- Work directly on `main` (this project's convention). Commit only when green; new commit per task.

---

### Task 1: Backend — config flag, flat placement in naming, service plumbing

**Files:**
- Modify: `backend/config.py` (`AppConfig` TypedDict ~line 118-128; `_DEFAULT_CONFIG` ~line 462-471)
- Modify: `backend/rename/naming.py` (`_destination` line 148-154; `build_target` signature line 157-158 and its `_destination` call line 202)
- Modify: `backend/rename/service.py` (every `_naming.build_target(` call: lines 735, 1189, 1615, 1662, 2092, 2096, 2190)
- Test: `tests/test_naming.py` (add cases)

**Interfaces:**
- Consumes: existing `meta` dict keys (`media_type`, `title`, `year`, `season`, `part`), existing `_DEFAULT_CONFIG`.
- Produces: `build_target(meta, *, movie_root="", tv_root="", template=None, flat=False) -> (fname, dest)` and `_destination(meta, *, movie_root, tv_root, title, year, flat=False) -> str`; config key `auto_rename_movie_flat: bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_naming.py` (it already imports `build_target` from `backend.rename.naming` — reuse that import; match the existing tests' style for constructing `meta`):

```python
def test_flat_single_movie_goes_to_library_root():
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "original_filename": "Sinners.2025.1080p.mkv"}
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    assert dest == "/lib/movies"
    assert fname == "Sinners (2025) [1080p].mkv"


def test_flat_split_movie_keeps_subfolder():
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "part": 2, "original_filename": "Sinners.2025.CD2.mkv"}
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    import os
    assert dest == os.path.join("/lib/movies", "Sinners (2025)")
    assert "Part 2" in fname


def test_flat_off_movie_keeps_subfolder():
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "original_filename": "Sinners.2025.1080p.mkv"}
    import os
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv")  # flat defaults False
    assert dest == os.path.join("/lib/movies", "Sinners (2025)")


def test_flat_does_not_affect_tv():
    meta = {"media_type": "tv", "title": "Severance", "year": 2022, "season": 2,
            "episode": 1, "original_filename": "Severance.S02E01.mkv"}
    import os
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    assert dest == os.path.join("/lib/tv", "Severance (2022)", "Season 02")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (throwaway container per Global Constraints):
```bash
MSYS_NO_PATHCONV=1 docker exec <c> sh -c "cd /app && python3 -m pytest tests/test_naming.py -q"
```
Expected: the 4 new tests FAIL with `TypeError: build_target() got an unexpected keyword argument 'flat'` (and the flat-off test may pass already).

- [ ] **Step 3: Add the config flag**

In `backend/config.py`, inside the `AppConfig` TypedDict near the other `auto_rename_*` fields (around line 118-128), add:
```python
    auto_rename_movie_flat: bool
```
In `_DEFAULT_CONFIG` near the other `auto_rename_*` defaults (around line 462-471), add:
```python
    "auto_rename_movie_flat": False,
```

- [ ] **Step 4: Add `flat` to naming**

In `backend/rename/naming.py`, replace `_destination` (currently lines 148-154):
```python
def _destination(meta, *, movie_root, tv_root, title, year, flat=False) -> str:
    if meta.get("media_type") == "tv":
        show = f"{title} ({year})" if year else title
        season = int(meta.get("season") or 1)
        return os.path.join(tv_root, show, f"Season {season:02d}")
    # Flat movie placement (opt-in): a single-file movie goes straight into the
    # library root. A split/multi-file movie (truthy `part`) keeps its own
    # folder so the parts stay grouped.
    if flat and not meta.get("part"):
        return movie_root
    folder = f"{title} ({year})" if year else title
    return os.path.join(movie_root, folder)
```

Change the `build_target` signature (line 157-158) to add the keyword-only `flat` param:
```python
def build_target(meta: dict, *, movie_root: str = "", tv_root: str = "",
                 template: Optional[str] = None, flat: bool = False) -> tuple[str, str]:
```

Change its `_destination` call (line 202) to forward `flat`:
```python
    dest = _destination(meta, movie_root=movie_root, tv_root=tv_root, title=title, year=year, flat=flat)
```

- [ ] **Step 5: Run the tests to verify they pass**

Re-copy then run:
```bash
docker cp backend/. <c>:/app/backend && docker cp tests/. <c>:/app/tests
MSYS_NO_PATHCONV=1 docker exec <c> sh -c "cd /app && python3 -m pytest tests/test_naming.py -q"
```
Expected: all pass (16 existing + 4 new = 20).

- [ ] **Step 6: Thread the flag through service.py call sites**

In `backend/rename/service.py`, add the keyword argument `flat=self._cfg.get("auto_rename_movie_flat", False)` to **every** `_naming.build_target(` call (lines 735, 1189, 1615, 1662, 2092, 2096, 2190). Passing it on a TV-only path is harmless — the TV branch of `_destination` ignores `flat`. Example for the call at line 1189-1193:
```python
        fname, dest = _naming.build_target(
            match,
            movie_root=self._movie_root(match.get("resolution")),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            flat=self._cfg.get("auto_rename_movie_flat", False),
        )
```
Apply the same `flat=self._cfg.get("auto_rename_movie_flat", False)` addition to each of the other six calls, preserving whatever other arguments (e.g. `template=...`) each already passes.

- [ ] **Step 7: Add a service-level regression test**

In `tests/test_rename_service.py` (reuse its existing `_service`/job helpers — grep `^def _service`, `^def _matched_job`, `^def _extracted` and an existing apply/preview test for the exact shapes), add a test that with `auto_rename_movie_flat=True` in the service config, a matched single-file movie's built destination is the movie library root (no `Title (Year)` subfolder). If the file's helpers make a full apply awkward, assert against the job's computed `destination_path` after the identify/preview step instead — whatever the sibling tests in that file already do. If a clean fixture isn't achievable, note it and rely on the naming unit tests from Steps 1-5 (which already prove the core behavior).

- [ ] **Step 8: Run the affected suites**

Re-copy then run:
```bash
docker cp backend/. <c>:/app/backend && docker cp tests/. <c>:/app/tests
MSYS_NO_PATHCONV=1 docker exec <c> sh -c "cd /app && python3 -m pytest tests/test_naming.py tests/test_rename_service.py tests/test_rename_core.py --timeout=90 -q"
```
Expected: all pass, no regressions.

- [ ] **Step 9: Commit**

```bash
git add backend/config.py backend/rename/naming.py backend/rename/service.py tests/test_naming.py tests/test_rename_service.py
git commit -m "feat(rename): opt-in flat movie folders (auto_rename_movie_flat)"
```

---

### Task 2: Frontend — Settings type + checkbox

**Files:**
- Modify: `frontend/src/lib/api/types.ts` (the settings/config interface, near `auto_rename_plex_sort_titles?: boolean` at line 386)
- Modify: `frontend/src/routes/settings/+page.svelte` (auto-rename section, near the `auto_rename_plex_sort_titles` checkbox at lines 1456-1461)

**Interfaces:**
- Consumes: Task 1's `auto_rename_movie_flat` config key (boolean, default False).
- Produces: a Settings checkbox writing `auto_rename_movie_flat` into the settings store (persisted via the existing settings save path — no new plumbing).

- [ ] **Step 1: Add the type field**

In `frontend/src/lib/api/types.ts`, immediately after `auto_rename_plex_sort_titles?: boolean;` (line 386), add:
```typescript
  auto_rename_movie_flat?: boolean;
```

- [ ] **Step 2: Add the Settings checkbox**

In `frontend/src/routes/settings/+page.svelte`, immediately after the `auto_rename_plex_sort_titles` `<label>` block (ends line 1461), add a matching checkbox:
```svelte
          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_movie_flat ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_movie_flat: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <Tooltip text="When on, a single-file movie is placed directly in the library folder (no per-movie subfolder). Split (multi-part) movies still get their own folder. TV shows are unaffected.">
              <span class="text-sm cursor-help underline decoration-dotted">Place movies directly in the library folder ⓘ</span>
            </Tooltip>
          </label>
```
(`Tooltip` is already imported and used in this section — confirm via `grep -n "import Tooltip" frontend/src/routes/settings/+page.svelte`; if for some reason it is not, use a plain `<span class="text-sm">Place movies directly in the library folder</span>` instead.)

- [ ] **Step 3: Verify the frontend build**

```bash
cd frontend && npm run check && npm run build
```
Expected: `0 ERRORS` (pre-existing a11y warnings are fine), build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/routes/settings/+page.svelte
git commit -m "feat(settings): flat movie folders toggle"
```

---

## Deployment

This plan does NOT deploy. After both tasks are merged and reviewed, deployment (`docker compose up -d --build`) is a separate step — the user has been deploying explicitly this session.
