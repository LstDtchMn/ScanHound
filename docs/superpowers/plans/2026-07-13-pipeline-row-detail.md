# Pipeline Row Detail Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a source-page link, real (non-fabricated) grabbed/renamed timestamps, and title-sized inline season display to each Pipeline row.

**Architecture:** Extend `get_pipeline_verdicts()`'s existing per-category `poster_path` `CASE` block (backend/database.py) to also select `renamed_at` from the identical matched `rename_jobs` row each branch already resolves — reuse, not a new join, deliberately avoiding the stale-sibling-row bug class the prior feature shipped and fixed. Add `grabbed_at` as a plain top-level column. Frontend renders both via the existing `checkedAgo()` helper and reworks the row template.

**Tech Stack:** Python 3.12 / FastAPI / sqlite3 (backend), SvelteKit 5 runes (frontend), pytest + vitest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-pipeline-row-detail-design.md`.
- Do NOT add `downloaded_at`/`extracted_at`/`verified_at` — out of scope, no persisted data backs them.
- `renamed_at` MUST come from the same row `poster_path` already selects per category (same `WHERE`, same `ORDER BY r.id DESC LIMIT 1`) — never a separate/looser query.
- Reuse `checkedAgo()` (frontend/src/lib/components/pipeline/pipelineDisplay.ts) for both new timestamps — do not write a second formatter.
- Backend tests run in a throwaway `scanhound:latest` container with code docker-cp'd in (`pip install pytest pytest-timeout httpx` first); frontend tests run on host node.
- Commit after the green test cycle. Work lands on `main` (repo practice — no feature branches).

---

### Task 1: grabbed_at + renamed_at columns, source link, inline season

**Files:**
- Modify: `backend/database.py:1113-1153` (`get_pipeline_verdicts`)
- Modify: `frontend/src/lib/api/types.ts` (`PipelineItem` interface, ~line 681-695)
- Modify: `frontend/src/lib/components/pipeline/PipelineList.svelte:131-161` (row template)
- Test: `tests/test_pipeline_service.py` (extend `TestPipelineVerdictsPosterPath`, class starts line 676)

**Interfaces:**
- Consumes: existing `poster_path` CASE block (backend/database.py:1124-1153), existing `checkedAgo(sqliteTs, now?)` helper (frontend/src/lib/components/pipeline/pipelineDisplay.ts), existing `PipelineItem` type.
- Produces: `PipelineItem.grabbed_at: string | null`, `PipelineItem.renamed_at: string | null` — no downstream consumers, this is the final UI surface.

- [ ] **Step 1: Write the failing backend tests**

Add to `tests/test_pipeline_service.py`'s existing `TestPipelineVerdictsPosterPath` class (after the last existing test in that class — find the class's final method and append below it, keeping indentation consistent):

```python
    def test_verdicts_include_grabbed_at(self, db_manager):
        db_manager.add_to_history("http://example.com/grabbed", "Heat", year=1995,
                                  resolution="1080p",
                                  package_name="Heat (1995) [1080p]")
        db_manager.upsert_pipeline_verdict("http://example.com/grabbed", "downloading",
                                           package_uuid="uuid1")
        rows = db_manager.get_pipeline_verdicts()
        assert rows
        assert rows[0]["grabbed_at"] is not None  # add_to_history sets last_grabbed_at

    def test_verdicts_include_renamed_at_from_processed_at(self, db_manager):
        db_manager.add_to_history("http://example.com/renamed_movie", "Heat", year=1995,
                                  resolution="1080p",
                                  package_name="Heat (1995) [1080p]")
        db_manager.upsert_pipeline_verdict("http://example.com/renamed_movie", "verified",
                                           package_uuid="uuid1")
        db_manager.create_rename_job({
            "package_name": "Heat (1995) [1080p]",
            "original_path": "/downloads/Heat.mkv",
            "status": "applied",
            "media_type": "movie",
            "title": "Heat",
            "year": 1995,
            "resolution": "1080p",
            "processed_at": "2026-07-13 10:00:00",
            "detected_at": "2026-07-13 09:00:00",
        })
        rows = db_manager.get_pipeline_verdicts()
        assert rows[0]["renamed_at"] == "2026-07-13 10:00:00"  # processed_at wins over detected_at

    def test_verdicts_renamed_at_falls_back_to_detected_at(self, db_manager):
        db_manager.add_to_history("http://example.com/pending_renamed_at", "Dune",
                                  year=2021, resolution="1080p",
                                  package_name="Dune (2021) [1080p]")
        db_manager.upsert_pipeline_verdict("http://example.com/pending_renamed_at",
                                           "pending_rename", package_uuid="uuid1")
        db_manager.create_rename_job({
            "package_name": "Dune (2021) [1080p]",
            "original_path": "/downloads/Dune.mkv",
            "status": "pending",
            "media_type": "movie",
            "title": "Dune",
            "year": 2021,
            "resolution": "1080p",
            "detected_at": "2026-07-13 08:00:00",
        })
        rows = db_manager.get_pipeline_verdicts()
        assert rows[0]["renamed_at"] == "2026-07-13 08:00:00"  # no processed_at yet: falls back

    def test_renamed_at_not_leaked_from_stale_sibling(self, db_manager):
        # Same adversarial scenario as the poster_path regression test above,
        # asserting renamed_at gets the same not-a-stale-sibling protection
        # for free, because it is selected from the identical matched row.
        db_manager.add_to_history("http://example.com/dune_stale_ts", "Dune",
                                  year=2021, resolution="1080p",
                                  package_name="Dune (2021) [1080p]")
        conn = db_manager.get_connection()
        conn.execute(
            "INSERT INTO rename_jobs (package_name, original_path, status, media_type, "
            "title, year, resolution, processed_at, detected_at) "
            "VALUES (?, ?, 'reverted', 'movie', ?, ?, ?, datetime('now', '-1 hour'), datetime('now', '-2 hour'))",
            ("Dune (2021) [1080p]", "/old/Dune.mkv", "Dune", 2021, "1080p"))
        conn.execute(
            "INSERT INTO rename_jobs (package_name, original_path, status, media_type, "
            "title, year, resolution, processed_at, detected_at) "
            "VALUES (?, ?, 'pending', 'movie', ?, ?, ?, NULL, ?)",
            ("Dune (2021) [1080p]", "/new/Dune.mkv", "Dune", 2021, "1080p", "2026-07-13 12:00:00"))
        conn.commit()
        db_manager.upsert_pipeline_verdict("http://example.com/dune_stale_ts", "pending_rename",
                                           package_uuid="uuid1")
        rows = db_manager.get_pipeline_verdicts()
        assert rows[0]["renamed_at"] == "2026-07-13 12:00:00"  # the current pending row's detected_at
```

- [ ] **Step 2: Run to verify failure**

Run (throwaway container): `python -m pytest tests/test_pipeline_service.py -k "grabbed_at or renamed_at" -v`
Expected: FAIL — `KeyError: 'grabbed_at'` / `KeyError: 'renamed_at'`.

- [ ] **Step 3: Implement the SQL extension**

In `backend/database.py`, replace `get_pipeline_verdicts()`'s body (lines 1113-1153) with:

```python
    def get_pipeline_verdicts(self, category=None, include_dismissed=False):
        """Return pipeline verdicts, joined with their downloads
        display fields, most-recently-checked first."""
        clauses = []
        params = []
        if not include_dismissed:
            clauses.append("v.dismissed = 0")
        if category:
            clauses.append("v.category = ?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._query_dicts(f'''
            SELECT v.url, v.category, v.detail, v.package_uuid, v.excluded_uuid,
                   v.plex_rating_key, v.checked_at, v.dismissed,
                   d.title, d.year, d.season, d.resolution, d.package_name,
                   d.last_grabbed_at AS grabbed_at,
                   CASE
                     WHEN v.category = 'pending_rename'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('pending', 'matched', 'applying')
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category = 'rename_failed'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('failed', 'needs_review', 'reverted')
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category IN ('awaiting_plex_refresh', 'verified', 'not_in_plex')
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status = 'applied'
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     ELSE NULL
                   END AS poster_path,
                   CASE
                     WHEN v.category = 'pending_rename'
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('pending', 'matched', 'applying')
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category = 'rename_failed'
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('failed', 'needs_review', 'reverted')
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category IN ('awaiting_plex_refresh', 'verified', 'not_in_plex')
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status = 'applied'
                           ORDER BY r.id DESC LIMIT 1)
                     ELSE NULL
                   END AS renamed_at
            FROM pipeline_verdicts v
            JOIN downloads d ON d.url = v.url
            {where}
            ORDER BY v.checked_at DESC
        ''', tuple(params))
```

(Note: the two `CASE` blocks share identical `WHERE`/`ORDER BY` per branch by
construction — this IS the "same matched row" guarantee the spec requires. If
`create_rename_job` in this codebase doesn't accept `detected_at`/`processed_at`
as insert kwargs, check its signature — `tests/test_pipeline_service.py`'s
existing tests already pass `processed_at` to it, so it should already work.)

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_pipeline_service.py -k "TestPipelineVerdictsPosterPath" -v`
Expected: PASS (all existing + 4 new).

- [ ] **Step 5: Frontend type + template**

`frontend/src/lib/api/types.ts` — add to `PipelineItem` (near `poster_url`):

```typescript
  /** downloads.last_grabbed_at — when this grab was last sent. */
  grabbed_at: string | null;
  /** rename_jobs.processed_at if applied, else detected_at — when the file
   *  was renamed or matched. Null until a rename job exists for this item. */
  renamed_at: string | null;
```

`frontend/src/lib/components/pipeline/PipelineList.svelte` — replace lines 131-161
(the `{#each items as item (item.url)}` block through its closing `{/each}`) with:

```svelte
      {#each items as item (item.url)}
        {@const ago = checkedAgo(item.checked_at)}
        {@const grabbedAgo = checkedAgo(item.grabbed_at ?? '')}
        {@const renamedAgo = checkedAgo(item.renamed_at ?? '')}
        <li class="p-3 flex items-center gap-3">
          {#if item.category && POSTER_CATEGORIES.has(item.category)}
            <RenamePoster posterUrl={item.poster_url} alt={item.title ?? ''} class="w-10 rounded" />
          {/if}
          <div class="flex-1 min-w-0">
            <div class="font-medium truncate">
              <a href={item.url} target="_blank" rel="noopener noreferrer"
                class="hover:underline">
                {item.title || item.package_name || item.url}
                {#if item.season != null}<span> S{String(item.season).padStart(2, '0')}</span>{/if}
              </a>
              {#if item.year}<span class="text-[var(--text-secondary)] font-normal"> ({item.year})</span>{/if}
            </div>
            <div class="text-xs text-[var(--text-secondary)] flex flex-wrap gap-x-2">
              <span style="color: {categoryColor(item.category)}">{categoryLabel(item.category)}</span>
              {#if item.resolution}<span>{item.resolution}</span>{/if}
              {#if grabbedAgo}<span>grabbed {grabbedAgo}</span>{/if}
              {#if renamedAgo}<span>renamed {renamedAgo}</span>{/if}
              {#if ago}<span>checked {ago}</span>{/if}
            </div>
            {#if item.detail}
              <div class="text-xs text-[var(--error)] truncate" title={item.detail}>{item.detail}</div>
            {/if}
          </div>
          {#if item.category && ACTIONABLE.includes(item.category)}
            <button class="px-2 py-1 text-xs rounded bg-[var(--accent)] text-white disabled:opacity-50"
              disabled={busy === item.url} onclick={() => regrab(item)}>Re-grab</button>
            <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
              disabled={busy === item.url} onclick={() => (searchModalUrl = item.url)}>Search sources</button>
          {/if}
          <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={busy === item.url} onclick={() => dismiss(item)}>Dismiss</button>
        </li>
      {/each}
```

(`checkedAgo('')` already returns `''` per the prior fix — safe for null timestamps
coerced to `''` via `?? ''`. The `<a>` wraps title+season only, not the year span,
so the year's lighter color stays visually distinct from the clickable link.)

- [ ] **Step 6: Verify + commit**

Run: `cd frontend && npm run check && npm run build && npx vitest run`
Expected: 0 errors, all tests pass (330 existing, no new frontend unit tests needed
per spec — `checkedAgo` is reused, not extended).

Run: `python -m pytest tests/test_pipeline_service.py -v --timeout=60`
Expected: ALL PASS.

```bash
git add backend/database.py frontend/src/lib/api/types.ts frontend/src/lib/components/pipeline/PipelineList.svelte tests/test_pipeline_service.py
git commit -m "feat(pipeline): source link, grabbed/renamed timestamps, inline season"
```
