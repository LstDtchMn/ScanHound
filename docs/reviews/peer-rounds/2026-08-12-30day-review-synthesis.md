# ScanHound Review Synthesis — `a4090c3..9227578` (24 PRs, 388 files, ~4 weeks)

---

## 1. VERDICT

The shipped work is broadly sound. Seven independent reviews plus adversarial re-verification produced **19 confirmed findings out of 27 candidates** (8 were disproven and dropped), and **not one of them is corrupting data or losing files right now**. Two things are silently broken in production today — the RSS shadow-mode readiness gate can never open, and the DV auto-sync does a full Plex library reconcile every hour forever — but both fail toward "wasted work" rather than "wrong result."

The one genuine landmine is in the rename Undo path: under a specific sequence it permanently deletes a media file with no trash entry and reports success. It has not fired (it needs a file whose download source was already cleaned up, plus an explicit Unarchive click), but it is the only finding in this review that can destroy something you cannot get back.

---

## 2. WHAT NEEDS FIXING

### HIGH — 4 findings

**H1. Undo can permanently delete your only copy of a movie**
`backend/rename/fileops.py:1757`

*What you'd see:* You apply a rename. Downloads live on P:, the library on X: — different drives, so ScanHound copies rather than hardlinks and records the method as `copy`. A week later JDownloader cleans up the P: download. You go to the Renames page, un-archive the job, click Undo expecting the library file to go back to the download folder. Instead the library file is deleted outright — not moved to trash, no recovery entry — and ScanHound reports "Reverted" and returns success. Both copies are now gone.

The verifier proved this by execution and found it is **worse than reported**: the same code path fires on the default same-volume `hardlink` method too, where unlinking the destination removes the last remaining link to the file. The code's own comment says *"The original src still exists"* — nothing anywhere checks that.

*Fix:* In `undo_place`, before removing `dst` on the hardlink/symlink/copy branch, require `os.path.exists(src)` and raise if it is missing; and route the removal through `_trash(dst)` instead of `_unlink_durable(dst)`, so an undo is recoverable like every other destructive path in that module (whose own docstring at `fileops.py:7-16` forbids exactly this). Add a test that applies with `method="copy"`, deletes the source, calls undo, and asserts the destination survives.

---

**H2. The RSS readiness gate can never open — it has been recording "listing incomplete" on every cycle since it shipped**
`backend/background_scanner.py:521`

*What you'd see:* The RSS Operations page reports `insufficient_comparison_cycles` and `miss_resolution_pending` forever, no matter how many healthy hourly cycles run. It reads as "keep waiting," so nothing looks broken — you just never get to promote RSS to primary.

*Why:* A cycle only counts as trustworthy evidence if the crawl terminated as `"complete"`. But the sole production caller crawls with `early_stop=True`, and in steady state the crawler stops early the moment it reaches already-cached posts — which stamps the termination `"early_stopped"`, not `"complete"`. So every recorded cycle stores `listing_complete=0`. That freezes `successful_cycles`, `observed_days`, `request_reduction_pct` and `recovery_cycles`, makes every recorded miss grade `not_yet_assessable`, and makes every unattributed candidate permanently un-clearable.

Note the interaction: because this gate can never open, **H4 below is currently unreachable** — one bug is masking the other.

*Fix:* Split the two early-stop causes into distinct termination values. A cached-frontier stop (reached known content) means the frontier *was* fully observed and should count as listing-complete; keep `listing_complete=False` only for terminations that genuinely truncate observation — `cancelled`, `page_errors`, `scan_error`, `empty_untrusted`, and the blocked-source stop.

*Cheapest confirmation before touching code:* against `/dbvol/crawler.db`, run
`SELECT listing_complete, COUNT(*), MAX(completed_at) FROM hdencode_shadow_cycles GROUP BY listing_complete;`
Prediction: everything since the migration is `0`.

---

**H3. A paused metadata scan can never be resumed — the Resume button is dead**
`backend/database.py:4852`

*What you'd see:* You start a full 4K metadata scan, pause it after a while, then click Resume. You get an error ("metadata scan has no retryable items"), the run stays stuck at "paused" forever, and restarting the container does not help. The only way forward is to throw the manifest away and re-run the entire multi-hour scan from scratch — and the durable scan does **not** reuse cached results, so all the dovi_tool/HDR10+ work is redone.

*Why:* `prepare_metadata_scan_resume` only resets rows in status `interrupted` or `cancelled`. A user pause leaves every unprocessed row in status `pending`, so it resets 0 rows and returns 0 without re-queuing the run. Reproduced end-to-end with a positive control. The UI renders Resume for three states — `paused`, `cancelled`, `interrupted` — and it only actually works for `interrupted`, the crash-recovery case.

*Fix:* Include `pending` in the resumable set (a pending row is by definition unprocessed) and drop the `reset_count == 0` early return so the run status still flips back to `queued`. Gate resumption on the *run's* status, not on a nonzero reset count.

---

**H4. RSS-primary mode deadlocks on its own health check (armed but not currently reachable)**
`backend/hdencode_rss_service.py:104`

*What you'd see:* Nothing today. But if RSS is ever promoted to primary, one transient outage stops all HDEncode discovery permanently. In `rss_primary` mode, `poll_cycle` refuses to poll when readiness isn't "ready" — but two of the readiness inputs (feed freshness and consecutive-failure count) can *only* be repaired by a successful poll. And the listing-crawl fallback that exists for exactly this situation is gated on the same `readiness["ready"]` flag, so the backup path shuts off too. A single HTTP 403 is enough (`consecutive_failures` increments on the first failure, not the third). The only exit is a human POSTing the mode back.

*Fix:* Separate the promotion gate from the runtime gate. Use full readiness only to decide whether RSS *may become* primary; gate the ongoing poll on durable qualification evidence only, never on live feed health — feed health is the thing the poll exists to repair. And drop `readiness["ready"]` from `fallback_qualified` so the listing crawl still runs.

---

### MEDIUM — 6 findings

**M1. DV auto-sync reconciles the entire Plex library every hour, forever, for nothing**
`backend/app_service.py:732`

The change-detector stores the *pre-sync* high-water mark, but `sync_labels` itself writes `dv_scan` rows for every matched title, pushing that mark forward. So the stored watermark is stale the instant it is written and the sync re-fires on the next pass — permanently. Reproduced with the real database and the real sync code: pass N stores `11:34:56` while its own writes push the true max to `11:34:58`; passes N+1 and N+2 both re-fire with `added=0, removed=0`.

*What you'd see:* Nothing wrong — just an hourly full enumeration of every configured Plex movie library plus ~444 SQLite writes with zero labels changed. The guard's own comment calls firing hourly "pure waste"; that is the shipped behaviour. The verifier also found it covers **every** movie with DV coverage, not just DV titles, and that it nulls the `sig_mtime`/`sig_size` columns on the way through.

*Fix:* Re-read the watermark *after* a successful sync (`self._last_dv_scan_at = self.db.get_latest_dv_scan_at(source="scan") or latest`), or make the rating-key back-write a targeted UPDATE that doesn't touch `last_seen_at`. The existing test cannot see this — it mocks the database and patches `sync_labels` out entirely.

---

**M2. DV detected inside the container can never produce a Plex label**
`backend/rename/dv_paths.py:25`

The path-normalizer knows exactly one drive mapping (`Y:` ↔ the Geronimo UNC share), and neither `sync_labels` caller passes a mapping table. But container-side detectors write rows with container paths (`/library/movies-4k/...`), while Plex — a native Windows install — reports `G:\Downloads\...`. Those two strings can never compare equal.

*What you'd see:* A 4K DV scan reports success, the inventory shows the detection, and the Kometa DV FEL/MEL badge never appears. Files on shares mounted both ways also accumulate two `dv_scan` rows, so the DV-scans panel double-counts them. Each unmatched write also feeds M1's hourly sync.

*Fix:* Pass the existing `plex_library_path_mappings` (reversed) into `normalize_path` at both call sites, or translate container paths back to Plex form before writing. Better still: `dv_scan` already stores `rating_key` — join on that instead of on path.

---

**M3. An unscanned 4K file vanishes from the inventory once a sibling version is scanned**
`backend/database.py:4961`

For a movie with two versions in one library, the inventory query suppresses a candidate if *any* inventory row shares its Plex item ID — but inventory rows are per-file while the item ID is per-title. Reproduced: before scanning, both versions show as `unscanned`; after scanning one, the other doesn't show as unscanned — it disappears from the list, the facet counts, the CSV export and the discrepancy report entirely.

*What you'd see:* "All 4K scanned" when it isn't. Scan *targeting* is unaffected (it reads Plex cache directly), so this is a visibility/coverage-reporting defect, not a scan-correctness one.

*Fix:* Make the de-duplication path-identity-based. The item-ID clause is load-bearing (it bridges the Plex-path vs container-path gap), so narrow it — compare translated paths, or carry `media_id`/part identity into the inventory table — don't just delete it.

---

**M4. Undo reports success when it did nothing at all**
`backend/rename/fileops.py:1760`

If a `move`-method job's destination file was moved or renamed outside ScanHound (in Windows or Plex, or during a share blip), Undo silently does nothing, marks the job `reverted`, and returns 200 with a "Reverted" toast. Because the job is no longer `applied`, Undo can never be retried. Requires the non-default `move_method`, so no data is destroyed — but the database now asserts a state ("the file is back at its original path") that isn't true.

*Fix:* Have `undo_place` return a definite outcome or raise `FileNotFoundError` when the destination is gone, and surface `{"ok": false, "error": "..."}` instead of writing `status=reverted`.

---

**M5. The only automatic release of the verification hold is tested by no test that executes it**
`tests/test_verification_hold.py:508`

`source_reveal_succeeded` is the single field that releases a Turnstile verification hold. Every test that exercises the release hands the queue a hand-built dictionary through a mocked download service. Proven by mutation: deleting the production line that sets the flag leaves **311 tests green**. A standalone end-to-end script shows clean tree → hold released, mutated tree → hold stays armed.

The production code is *correct today* — this is unprotected regression risk on a load-bearing field, in exactly the class of bug (`transport_attempted`) that already shipped twice and prompted the sibling contract suite.

*Fix:* Two added assertions inside the existing `test_a_REAL_jdownloader_success_sets_it_end_to_end`, plus one test pushing a real mapped outcome through `_complete` against a batch with an armed hold.

---

**M6. The fileops writer-guard "contract" cannot catch the regression it exists to catch**
`tests/test_fileops_writer_guard_contract.py:15`

The test checks that eleven hand-listed functions still take the writer lock first. It derives nothing from the module and uses a subset assertion, which is mathematically incapable of failing when a function is *added*. Proven: appending an unguarded `purge_orphan_parts()` that unlinks files passes the contract green. There is no second enforcement layer anywhere — no decorator, no import hook.

*Fix:* Invert it. Walk the module's AST for functions calling mutating primitives (`os.unlink`, `os.remove`, `os.replace`, `shutil.move`, …) and require each to guard first or appear in an explicit, comment-justified exemption list (a derived scan yields exactly 9 internal helpers today). Adding a mutator should then fail by default rather than pass by omission.

---

### LOW — 9 findings

Grouped tersely; none of these will surprise you in normal use.

| # | Where | What |
|---|---|---|
| L1 | `backend/download_queue.py:1648` | "Retry now" on a claimed/completed row returns success while retrying nothing — and still clears the batch's shared source cooldown, hiding that batch from two diagnostics. `cancel_item` correctly 409s on the same state. Fix: check the UPDATE's rowcount, raise, and skip the batch write. |
| L2 | `backend/api/main.py:519` | Adding the `/rss` API router made a browser navigation to the `/rss` **page** return `401 {"detail":"Unauthorized"}` instead of the app. Bookmark, F5 or middle-click only; in-app clicks are fine. `/settings`, `/analytics`, `/pipeline`, `/watchlist` already do this. Fix: exempt navigation-shaped GETs (Accept: text/html + Sec-Fetch-Mode: navigate). |
| L3 | `backend/api/ws.py:127` | The 30-day session token rides in the WebSocket URL and is written verbatim to uvicorn's stdout (the existing masking filter is attached to the root logger and never sees it), plus NPM's access log and Cloudflare's request records. Every reader of those already has equal-or-greater access on this box, so it's weakened defence-in-depth, not escalation. Pre-dates the window. |
| L4 | `backend/database.py:4995` | A file whose own DV probe failed inherits a sibling file's DV layer via a per-item fallback, and is displayed as if it were that file's own measurement. Display-only — the discrepancy endpoint filters it out. The verifier found the *seed* side borrows symmetrically and **does** reach the discrepancy export. |
| L5 | `backend/database.py:2761` | A failed read of the miss-rows table returns `[]`, which reads as "no misses" and can produce a false "ready." Needs an infrastructure fault; one variant is already caught by a count-reconciliation guard the reviewer missed. Fix: sentinel default + an `evidence_problems` entry. |
| L6 | `backend/rename/fileops.py:1387` | The two sweep paths refuse a trash bucket that is a symlink; `delete`/`restore`/`list` don't, so `/trash/delete` can unlink a file outside the trash root. Needs symlink creation inside a Windows bind mount — thin in this deployment. One-line fix at three call sites. |
| L7 | `backend/rename/fileops.py:591` | A corrupt trash-root index reads as "no roots" rather than "I couldn't tell," hiding trashed files from list/restore/sweep after a restart. The index is written atomically, so corruption is unlikely. Fails safe (leaks space, doesn't delete). Fix: log it. |
| L8 | `backend/rename/fileops.py:1746` | If trashing the source fails *after* a cross-device copy published the destination, the apply is reported failed and the error message says the library slot "is now EMPTY" when it holds the new file. Acting on that message is how you lose one of the two copies. Needs the non-default `move` method plus a rare I/O failure. |
| L9 | `tests/test_dv_labeler.py:568` | The tests pinning "manual DV sync is non-destructive by default" do the plumbing themselves; the route line that actually carries the safe value is executed by no test, and both layers below default to the destructive value. No live defect — one route-level assertion closes it. |

---

## 3. WHAT IS SOLID

Things that were probed hard and came back clean:

- **The download queue and the Turnstile verification hold.** This dimension refuted **3 of its 4** candidate findings — including a claim that a cancelled batch could strand siblings and a claim that unknown outcomes could be re-promoted — by driving a real 22-item queue through the real claim/execute/fail path against a real SQLite database. The hold logic itself (`_pause_for_source`, `_release_verification_hold`, `decide()`'s precedence ordering) held up under attack. Only a cosmetic no-op survived.
- **The auth boundary.** The DV ingest key is correctly scoped to one method + one exact path with no decode divergence to exploit; every branch of `_request_requires_auth` fails closed, including during startup/teardown when the database is nil; session tokens are stored SHA-256 only; comparisons use constant-time; the blanket OPTIONS exemption is safe because no router registers OPTIONS. The `_within` path confinement now realpaths both sides and fails closed on error.
- **The v8→v9 migration** applied cleanly on a hand-built v8 database with a paused-source batch and an interactive-challenge item, and stamped `user_version` correctly. The `... is not None` upsert bug (fixed in `355e9d2`) has no surviving instances repo-wide.
- **DV correctness rules** that were checked and found right: the schema-version handshake, the POST-rows count reconciliation, and the bounded-FEL asymmetry rule.
- The DV **detection** logic and the **fail-closed** posture generally: every issue in this report either wastes work, hides information, or reports the wrong status. None of them produce a wrong DV verdict or a wrong acquisition decision.

**What that does not prove.** A clean dimension means *"seven reviewers looking for a specific class of bug, at a specific depth, did not find one."* It does not mean the subsystem is correct. In particular, the auth dimension returned 2 confirmed / **0 refuted** — no adversarial pressure was applied to its own conclusions, which is a different reliability profile from the download-queue dimension's 1/3. And every "solid" verdict above was reached against a scratchpad checkout of `9227578`, not against the running container.

---

## 4. COMPLETENESS CRITIQUE — the important part

This review covered maybe half of what shipped. Here is what it did not.

### 4a. Modules in the diff that nobody read

The seven dimensions were: download queue, DV pipeline, auth, database/migrations, RSS shadow, fileops safety, test quality. Across 388 changed files, that leaves entire subsystems with **no owner**:

- **The frontend.** No dimension owned SvelteKit. Every `.svelte` reference in this report is incidental — read to check whether a backend bug was reachable from a button, never to review the frontend itself. Nobody looked at state management, the API client's error handling, the mobile views, or whether the new RSS Operations page actually works. Given 24 PRs, a meaningful share of those 388 files are frontend.
- **`scanner_service.py` / the scraper core.** Read only in the regions that touch RSS shadow termination flags. The Chromium/ChromeDriver interaction, the reveal flow, page diagnostics, and the Turnstile detection heuristics were not reviewed as a subsystem.
- **`download_service.py` in full.** Only ~1,000 of its lines were read (three windows), all chosen because the queue reviewer needed them. The JDownloader integration proper was not reviewed.
- **`pipeline_service.py`, `conflict_analyzer.py`, `conflicts.py`** — touched only where DV scoring intersects them.
- **`docker-compose.yml`, `Dockerfile`, `docker/entrypoint.sh`, the scheduled-task scripts.** Read only for individual facts (mount paths, log level). Nobody reviewed deployment configuration as a thing that can be wrong — and per your own notes the live compose carries **required uncommitted modifications** (ingest key env + the `127.0.0.1:9721:9721` port binding). **Nobody verified that the deployed container actually corresponds to `9227578`.** That is a premise the whole review rests on and it was never checked.
- **`scripts/host-detector/` beyond the DV scan path**, and `scripts/reverify_716.py`.

### 4b. Nothing was run against the live system

This is the single biggest gap. Multiple findings produced **specific, cheap, falsifiable predictions about production data — and none of them were executed**:

- `SELECT listing_complete, COUNT(*), MAX(completed_at) FROM hdencode_shadow_cycles GROUP BY listing_complete;` — would confirm or kill H2 in one query.
- Hourly bucket counts on `dv_scan.last_seen_at WHERE source='scan'` — would confirm M1's upsert storm.
- `SELECT source, COUNT(*) FROM dv_scan WHERE path LIKE '/library/%' GROUP BY source;` — would size M2 exactly.
- `SELECT rating_key, COUNT(*) FROM plex_cache ... GROUP BY rating_key HAVING COUNT(*)>1;` — would size M3's exposure.
- `docker logs scanhound | grep 'WebSocket /ws'` — would confirm L3's leak on the real box.

Every one of these is a five-second query. Running them should be the *first* action of the next round, before any code is touched: they convert four findings from "proven in a synthetic harness" to "proven in your database," and they may reveal the blast radius is bigger or smaller than modelled.

Relatedly: **no test suite run was performed** in this review, and **nothing was deployed or exercised through the UI**. Behaviour only provable by running the app — does the Resume button actually error the way H3 predicts? does the DV badge actually fail to appear for a container-detected title (M2)? — remains unproven at the level that matters to you.

### 4c. Findings that rest on reading rather than executing

Roughly split: the fileops, database, download-queue and test-quality dimensions ran real code (repro scripts, mutation experiments, a real `DatabaseManager`, a real `DownloadService`). The **RSS shadow dimension explicitly notes "Bash was unavailable for part of the session; findings are grounded in file reads, not command output."** That dimension produced **H2 and H4** — two of the four high-severity findings, including the one claimed to be silently broken in production right now. Their verifiers reasoned rigorously but also did not execute or query live data. H2 and H4 are the least empirically grounded high findings in the set and should be treated as high-confidence hypotheses until the queries above run.

The auth dimension is entirely read-based except for one reproduction (L2's 401) and one logging repro (L3).

### 4d. Surface that looks thin relative to churn

- **`backend/database.py` grew by 3,217 insertions.** One dimension read all ~5,800 lines and ran four repro scripts — good depth — but the migration test used a *hand-built* v8 database, not a copy of the real `/dbvol/crawler.db` with two weeks of production rows and whatever historical schema drift it carries. Migrations are exactly where a synthetic fixture lies to you.
- **No performance review at all.** The new media-inventory CTE is a multi-way join with correlated subqueries, and it backs the search, facets, CSV export and discrepancy endpoints. Nobody ran `EXPLAIN QUERY PLAN` or timed it against a realistic row count. Given SQLite write-lock contention is already a known pain point on this box, this is a real omission.
- **No concurrency or restart-race testing.** Several verifications lean on "single worker thread," "`LIMIT 1`," "startup rewrites claimed rows." Those were asserted from code, not tested with two processes, a mid-transaction kill, or a container restart during an active durable scan.
- **The 8 refuted findings were not mined.** Each refutation encodes a mental model that turned out to be wrong about how a subsystem works. Nobody asked whether a *pattern* in those mistakes points at code that is simply hard to reason about — which is where the next real bug tends to live.

### 4e. What round two needs to do

1. Run the five live queries above and reconcile every prediction. Confirm the deployed image is actually `9227578` plus the known compose deltas.
2. Give the frontend and `scanner_service`/`download_service` their own dimensions.
3. Run the full test suite (whole-tree copy, in-container, per the established convention) against both `9227578` and `origin/main` in the same session, so any failure is attributable.
4. Restore the durable-scan and RSS findings by *doing them in the UI* — pause a real scan and click Resume; check the RSS readiness panel's actual numbers.
5. Time the media-inventory queries against a copy of the real database.

---

## 5. FOR THE PEER REVIEWER (ChatGPT)

Five questions, chosen where a second opinion is most likely to change the answer rather than confirm it.

**Q1 — `backend/background_scanner.py:521` + `backend/scanner_service.py:1006-1079`.** Our claim is that `listing_complete` has been written `0` on essentially every cycle since it shipped, because the only production caller uses `early_stop=True` and the cached-frontier stop is classified as a non-complete termination — permanently freezing the RSS readiness gate. **Two things to scrutinize:** (a) is there any production path we missed that can terminate `"complete"` in steady state? (b) More importantly — is our proposed fix *safe*? Treating a cached-frontier early stop as "listing complete" asserts that reaching known content proves the frontier was fully observed. Is that actually true given `early_stopped` is crawl-global (declared once before the source loop at `scanner_service.py:808`) and one source's early stop poisons the whole crawl's verdict? A wrong answer here would certify partial listings as authoritative evidence — the exact failure the column was created to prevent.

**Q2 — `backend/rename/fileops.py:1752-1758` and `backend/rename/service.py:1784-1796`.** `undo_place` hard-deletes the destination for `hardlink`/`symlink`/`copy` on an unverified assumption that the source still exists, using `os.unlink` rather than the trash. Two questions: (a) is the *default* same-volume hardlink case really as fatal as our verifier found (unlinking the last directory entry for the inode)? (b) Which fix is correct — refuse when the source is missing, or route the deletion through `_trash()`? We think both, but the module's mandate (`fileops.py:7-16`) arguably means *no* undo path should ever call `os.unlink` on a library file. Does a rule that strict break any legitimate undo?

**Q3 — `backend/rename/dv_paths.py:25`, `backend/rename/dv_labeler.py:115/231`, `backend/database.py:4995`, `backend/app_service.py:732`.** Three of our findings (M1, M2, L4) are arguably one root cause: `dv_scan` rows are joined **by path**, but paths exist in at least three namespaces (host drive letters, UNC, container mounts) and there is no canonical form. `dv_scan` already stores `rating_key`. **Should the labeler and the inventory evidence join on `rating_key` (with `media_id`/part for multi-version titles) and stop normalizing paths entirely?** If yes, three separate patches collapse into one design change — and we'd rather hear that now than ship three narrow fixes.

**Q4 — `backend/database.py:4852` and `backend/plex_metadata_scan.py:217-227`.** A paused durable scan can never resume because pause leaves rows in `pending` and the resume query only resets `interrupted`/`cancelled`. There are two candidate fixes: **reader-side** (accept `pending` in the reset set) or **writer-side** (have pause/cancel transition remaining rows to `interrupted`). Which is safer against a worker that is still mid-file when the pause lands, and against a container restart racing the same rows? We lean reader-side because a `pending` row is unprocessed by definition, but the writer-side fix preserves a cleaner state machine.

**Q5 — `backend/database.py:4956-4963`.** The item-ID clause in the inventory CTE is load-bearing: it is the only thing that stops an already-scanned file re-appearing as "unscanned" when a Plex path mapping is configured, because scanned rows store the *translated* container path while the cache stores Plex's path. Removing it fixes the disappearing-sibling bug (M3) and reintroduces a false-unscanned bug. **What is the right identity key here?** Options we see: compare translated paths on both sides; carry `media_id`/part into the inventory table; or split the projection so unscanned candidates and scanned rows are de-duplicated by different rules. A second opinion on which of those is least likely to create a third bug is worth more than our patch.