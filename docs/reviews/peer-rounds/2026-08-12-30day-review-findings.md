# Confirmed findings appendix — a4090c3..9227578

19 confirmed of 27 candidates (8 refuted and dropped by adversarial verification).

## [HIGH] A paused (or cancelled) durable metadata scan run can never be resumed — its remaining pending items are orphaned
**Location:** `backend/database.py:4852`  **Category:** correctness

**Claim:** prepare_metadata_scan_resume() only resets manifest rows whose status is 'interrupted' or 'cancelled', but a user-initiated pause/cancel leaves every unprocessed row in status 'pending', so it resets 0 rows, returns 0 without re-queueing the run, and PlexMetadataScanJob.resume() raises 'metadata scan has no retryable items' forever.

**Failure scenario:** Operator starts a full 4K metadata scan (POST /plex/metadata-scans, scope='full'). After file 1 of 3 completes they POST /plex/metadata-scans/{uuid}/pause. _run_durable breaks its loop and writes run status='paused'; files 2 and 3 are still status='pending'. POST .../resume -> prepare_metadata_scan_resume returns 0 -> resume() raises RuntimeError -> HTTP 409 'metadata scan has no retryable items'. POST .../retry-failures (retry_failed=True) also returns 0. Restarting the container does not help: interrupt_abandoned_metadata_scans() only touches status='running'. The run sits at 'paused' permanently and the only recourse is to discard the durable manifest and re-run the whole multi-hour scan from scratch. Reproduced: `run status: paused / item statuses: ['current','pending','pending'] / prepare_metadata_scan_resume -> 0 / prepare(retry_failed=True) -> 0 / run status after: paused`.

**Evidence:** database.py:4852-4873 `statuses = ["interrupted", "cancelled"]` … `WHERE run_uuid = ? AND status IN ({placeholders})` … `reset_count = max(cursor.rowcount, 0); if reset_count == 0: return 0` (the `UPDATE metadata_scan_runs SET status='queued'` at 4874 is skipped). plex_metadata_scan.py:137-138 `if reset <= 0: raise RuntimeError("metadata scan has no retryable items")`. plex_metadata_scan.py:222-227 writes `status=stop_mode` ('paused'/'cancelled') while leaving unprocessed items 'pending'. plex_metadata_scan.py:329-331 even pushes the in-flight item back to 'pending' on ProcessCancelled. database.py:4826 `WHERE status = 'running'` — restart recovery cannot convert those pending rows either. Endpoints backend/api/routes/plex.py:312-334 and frontend/src/lib/api/client.ts:309-315 expose pause and resume as a normal pair. The only test of pause, tests/test_metadata_scan_runs.py:126-139, asserts nothing but the in-memory `job._stop_mode` and never round-trips through resume, so it would pass unchanged if resume were deleted.

**Suggested fix:** Include 'pending' in the resumable set (a pending row is by definition unprocessed and safe to re-run — _run_durable already selects exactly status='pending'), and drop the `reset_count == 0` early return so the run is still flipped back to 'queued'/'running' when the manifest simply has nothing to reset. Guard resumption on the run's own status ('paused'/'cancelled'/'interrupted'/'completed'), not on a nonzero reset count. Add a test that pauses a run with pending rows and asserts resume() re-queues them.

**Adversarial verifier:** Reproduced end-to-end against the real DatabaseManager and the real PlexMetadataScanJob, with a positive control that passes in the same run.

CODE VERIFIED: backend/database.py:4852 `statuses = ["interrupted", "cancelled"]` (+ "failed" when retry_failed=True); the UPDATE at 4865-4870 filters `status IN (...)`, so `pending` rows are never reset, and `if reset_count == 0: return 0` (4872-4873) short-circuits before the `UPDATE metadata_scan_runs SET status='queued'` at 4874. backend/plex_metadata_scan.py:137-138 turns that 0 into `RuntimeError("metadata scan has no retryable items")`, which backend/api/routes/plex.py:305-309 (`_run_control`) converts to HTTP 409.

PAUSE REALLY LEAVES ROWS PENDING: `_run_durable` (plex_metadata_scan.py:217-227) snapshots `list_metadata_scan_items(run_uuid, status="pending")` and `break`s at the top of an iteration, so unprocessed rows are never transitioned; the in-flight row is explicitly pushed BACK to 'pending' on cancellation (lines 262-266, 285-289, 327-331).

NO RECOVERY ELSEWHERE: `interrupt_abandoned_metadata_scans` (database.py:4806-4841) is scoped `WHERE status = 'running'` and its own docstring states "A user-paused run remains paused across restart."

REPRODUCTION (scratchpad/repro_e2e.py, only mediainfo.probe_detailed stubbed):
  run status after pause: paused
  item statuses: ['current', 'current', 'pending']
  resume RAISED: metadata scan has no retryable items
  retry_failures RAISED: metadata scan has no retryable items
  final run status: paused
POSITIVE CONTROL in the same session: an item set to 'interrupted' resets 1 row and moves the run to 'queued' — so the query mechanism works and 'pending' is genuinely excluded, not a harness artifact.

REACHABILITY IS WORSE THAN CLAIMED: I looked for the "not wired to any UI" re


---

## [HIGH] listing_complete is always written False in production: the only shadow-cycle producer runs with early_stop=True, which is a non-"complete" termination, so no new cycle can ever count toward readiness or resolve any miss
**Location:** `backend/background_scanner.py:521`  **Category:** correctness

**Claim:** The listing-arm authority flag is keyed on `_last_crawl_termination == "complete"`, but the only production caller that produces shadow cycles crawls with `early_stop=True`, which sets the termination to "early_stopped" on essentially every steady-state cycle — so every cycle recorded since the column shipped is `listing_complete=0`, which permanently freezes `successful_cycles`, makes every miss grade `not_yet_assessable`, and makes every unattributed candidate uncleanable.

**Failure scenario:** Steady state, hourly background cycle, background cache populated. Page 1 of the 4K listing has 2 new posts (page_new=2), page 2 has 25 posts all already cached (page_unique=25, page_new=0) -> early_stopped=True -> `_last_crawl_termination="early_stopped"` -> compare_shadow is called with listing_complete=False -> `record_hdencode_shadow_comparison` stores listing_complete=0. Consequences, all silent: (a) `get_hdencode_shadow_summary().successful_cycles` never increments again, so readiness reports `insufficient_comparison_cycles` forever even though hundreds of healthy cycles are on disk; (b) `classify_miss_resolution` finds `valid_later == []` for every recorded miss, so every miss grades `not_yet_assessable` and readiness reports `miss_resolution_pending` forever; (c) `unattributed_candidates` can never be cleared because clearing requires `listing_ok is not False`. The gate is permanently closed while reporting reasons that read as "keep waiting". Note the intent is documented backwards in tests/test_listing_membership_authority.py:198-201, which treats "early stop -> listing_complete False" as the desired chain without checking that early stop IS the normal production termination.

**Evidence:** background_scanner.py:514-523 `listing_complete=(not bool(err) and getattr(scanner, "_last_crawl_termination", "not_run") == "complete")`. The producing crawl is background_scanner.py:635-644 `self._reg.scanner.run_scan(..., skip_urls=skip_urls, early_stop=True)` with `skip_urls = db.get_background_cache_urls()` (background_scanner.py:277). scanner_service.py:1006-1009 `if early_stop and page_unique > 0 and page_new == 0: ... early_stopped = True`; scanner_service.py:1068-1070 `elif early_stopped: self._last_crawl_termination = "early_stopped"`; only scanner_service.py:1077-1079 yields "complete". Consumers: database.py:2296 `AND (listing_complete IS NULL OR listing_complete=1)` in the `eligible` query behind `successful_cycles`; hdencode_shadow.py:523-526 `if not listing_ok: return False` in `cycle_is_valid_evidence_for`; database.py:2742 `if listing_ok is not False:` guarding candidate clearing.

**Suggested fix:** Treat "early_stopped" as listing-complete for membership purposes when the early stop was the cached-frontier stop (page_unique>0 and page_new==0 — the crawl reached known content, which means the frontier was fully observed), and keep listing_complete=False only for the terminations that truncate observation: "cancelled", "page_errors", "scan_error", "empty_untrusted", and the blocked-source early stop (scanner_service.py:1025). Record the two early-stop causes as distinct termination values rather than folding them into one.

**Adversarial verifier:** Survives every refutation attempt. (1) Single producer: grep across backend/ finds exactly one call to compare_shadow( (background_scanner.py:461) and one to record_hdencode_shadow_comparison (background_scanner.py:532) — no route, manual scan, or backfill can write listing_complete=1. (2) That producer's listing arm is _scan_source(), which hardcodes early_stop=True and passes skip_urls=db.get_background_cache_urls(); the same loop upserts the background cache, so skip_urls is non-empty in steady state. (3) early_stopped is crawl-global, not per-source: scanner_service.py:808 declares it once BEFORE `for source in sources:` and the early-stop break only exits the page loop, so a single source reaching its cached frontier poisons the whole crawl's verdict — and _category_flags() returns ALL categories, so multiple sources are crawled per cycle. (4) No softening downstream: hdencode_shadow.py:445-447 preserves an explicit False, and database.py:2233 stores it as 0 (not NULL), so the legacy-NULL fallback at database.py:2296 and hdencode_shadow.py:520 does not apply. (5) Termination is not re-stamped: scanner_service.py:406 resets to "not_run" at scan start; "complete" is only reachable at 1077-1079 as the final else after `elif early_stopped`. (6) All three consumers behave exactly as claimed: eligible-cycles query (database.py:2296), cycle_is_valid_evidence_for (hdencode_shadow.py:523-526 `if not listing_ok: return False`), and candidate clearing (database.py:2742 `if listing_ok is not False:`). The reviewer actually UNDER-stated the blast radius: observed_days, request_reduction_pct and recovery_cycles all derive from the same `eligible` aggregate (database.py:2545, 2857), so MAX(completed_at) freezes too and `insufficient_observation_days` locks alongside `insufficient


---

## [HIGH] rss_primary deadlocks on its own readiness gate: a stale or failing normal feed makes poll_cycle skip polling, and only polling can clear staleness — the listing fallback is disabled by the same condition
**Location:** `backend/hdencode_rss_service.py:104`  **Category:** correctness

**Claim:** In rss_primary, `poll_cycle` refuses to poll when readiness is not ready, but two readiness inputs (`last_checked_at` freshness and `consecutive_failures`) can only be repaired by a successful poll, and the listing fallback that exists for exactly this situation also requires `readiness["ready"]` — so one transient outage permanently stops all HDEncode discovery with no self-recovery.

**Failure scenario:** Concrete trigger from an adjacent subsystem in the same window: a reveal stall escalates the coordinator cooldown to 4 hours (`_REVEAL_ESCALATION = (1, 2, 4)`, hdencode_coordinator.py:510, applied at 512-556). Every `poll_feed` during that window raises `HDEncodeTrafficDenied` at hdencode_rss_service.py:210-225 and returns outcome "denied" WITHOUT touching `last_checked_at`. After 180 minutes both movies_all and tv_all are stale, so `feeds_healthy` is False and `ready` is False. The next cycle hits line 104 and returns `primary_not_ready` before reaching the feed loop; background_scanner.py:401-411 then skips the HDEncode listing crawl because `fallback_qualified` is False. From then on nothing polls the feeds, so `last_checked_at` never advances, so readiness is never ready — zero RSS discovery and zero listing discovery, indefinitely, until a human POSTs /rss/mode back to listing or rss_shadow. A simpler trigger: three consecutive HTTP 403s set `consecutive_failures` (database.py:1686) and only a successful poll resets it, so the same deadlock occurs one cycle after the first sustained block. The fallback protects exactly the FIRST failing cycle (readiness is read at line 96 before the failure is persisted) and then disables itself.

**Evidence:** hdencode_rss_service.py:104-120 `if mode == "rss_primary" and not readiness["ready"]: ... return cycle` (with `"requests": 0`, `"fallback_qualified": False`). readiness health at database.py:2874: `feeds_healthy=all(... last_status in (200,304) and int(consecutive_failures or 0)==0 and fresh(...))`, where `fresh()` uses `max_stale_minutes` default 180 (database.py:2855, 2872). The only writers of `last_checked_at`/`consecutive_failures=0` are `ingest_hdencode_feed` (database.py:1606,1614) and `record_hdencode_feed_not_modified` (database.py:1653,1655), both reachable only from `poll_feed` (hdencode_rss_service.py:261,322), which is reachable only from the `poll_cycle` that just returned early. Fallback: hdencode_rss_service.py:167-175 `fallback_qualified = bool(mode == "rss_primary" and readiness["ready"] and coverage_uncertain and ...)`, consumed at background_scanner.py:400-411, which appends `skipped: "rss_primary"` and `continue`s when not qualified — so the listing crawl is skipped too. tests/test_hdencode_rss_primary.py:208-231 asserts exactly this skip-with-requests==0 behaviour as correct.

**Suggested fix:** Split the gate: use the full readiness result only as the PROMOTION gate (api/routes/rss.py:282 and the shadow-evidence checks), and gate the runtime rss_primary poll on the durable qualification evidence only (`successful_cycles`, `observed_days`, miss resolution, integrity), never on live feed health/freshness. Feed health should trigger the fallback, not suppress the poll — i.e. drop `readiness["ready"]` from `fallback_qualified` and replace the line-104 early return with a poll that still runs (feed health is what the poll is supposed to repair).

**Adversarial verifier:** Verified against the code, and the mechanism is exactly as described (in one respect worse). backend/hdencode_rss_service.py:104-120 returns before the feed loop whenever mode=="rss_primary" and readiness is not ready. readiness["ready"] depends on feeds_healthy (database.py:2874, 2931), which requires last_status in (200,304), consecutive_failures==0, and last_checked_at fresher than max_stale_minutes (default 180, database.py:2855/2872 — poll_cycle does not override it). I grepped every writer in backend/: consecutive_failures is zeroed only by ingest_hdencode_feed (database.py:1614) and record_hdencode_feed_not_modified (database.py:1655); last_checked_at is written only by those two plus record_hdencode_feed_failure (1684). All three are reachable only from poll_feed, which is called only from poll_cycle:152 — the loop that was just skipped. No reset endpoint, no auto-demote, no maintenance writer exists. background_scanner.py:308 is the sole poll_cycle call site outside tests, so there is no second poller. The listing fallback at 167-175 does require readiness["ready"], and readiness is read at line 96 before any failure is persisted, so it covers only the first failing cycle; background_scanner.py:400-411 then appends skipped:"rss_primary" and continues, skipping the listing crawl. Both triggers check out: coordinator.request() raises HDEncodeTrafficDenied (hdencode_coordinator.py:383,404) and _REVEAL_ESCALATION=(1,2,4) at line 510 makes a 4h block reachable; the denied return (hdencode_rss_service.py:219-225) persists nothing, so last_checked_at ages past 180 minutes and stays stale after the block clears. The HTTP-failure trigger is actually stronger than the reviewer claimed: record_hdencode_feed_failure (database.py:1680-1688) sets consecutive_failures = +1 on


---

## [HIGH] undo of a copy-method apply hard-deletes the library file without checking the source still exists (permanent media loss, not trashed)
**Location:** `backend/rename/fileops.py:1757`  **Category:** data-loss

**Claim:** undo_place() unconditionally unlinks the placed destination for methods hardlink/symlink/copy on the assumption "the original src still exists", but nothing anywhere verifies that assumption, and the removal is os.unlink (via _unlink_durable), not a trash move — so undoing a cross-device copy after the download has been cleaned up destroys the only surviving copy of the media.

**Failure scenario:** Downloads on P:, Plex library on X: (different volumes). Apply a job: hardlink raises EXDEV, place_file falls back to a verified copy and records move_method="copy"; the DB row says applied. A week later JDownloader cleanup (or the user) removes the P: download. The user clicks Undo on the Renames page expecting the library file to go back to the download folder. undo_place takes the copy branch, `os.path.lexists(dst)` is true, `_unlink_durable(dst)` permanently removes the library file. Nothing is trashed, no trash entry is created, undo() then returns {"ok": True} and marks the job "reverted". Both copies of the movie are now gone with no recovery path — directly violating the module's own stated mandate at fileops.py:7-16 ("no accidental file deletion ... deletions must go through a user's input first" / source is trashed, never os.remove'd).

**Evidence:** fileops.py:1755-1758:
    if method in ("hardlink", "symlink", "copy"):
        # The original src still exists — just drop the link/copy.
        if os.path.lexists(dst):
            _unlink_durable(dst)
fileops.py:399-405 `_unlink_durable` is a plain `os.unlink(path)` + dir fsync — it never calls `_trash`.
service.py:1791-1796 (the only caller):
        src = job.get("original_path")
        dst = os.path.join(job.get("destination_path") or "", job.get("new_filename") or "")
        try:
            _fileops.undo_place(src, dst, job.get("move_method") or "move")
No `os.path.isfile(src)` check exists in undo(), in undo_place(), or in the route (routes/rename.py:405-410). `move_method` is whatever place_file returned; place_file returns "copy" whenever a configured "hardlink" hits EXDEV (fileops.py:1716-1719), which is the documented normal deployment (tests/test_rename_core.py: "Covers the real JD-output-vs-Plex-library-on-different-volumes case"). Default `auto_rename_move_method` is "hardlink" (backend/config.py:527). service.py:1734-1782 `detect_moved_source_files` only scans status in (needs_review, matched), so an applied job whose source vanished is never flagged.

**Suggested fix:** In undo_place, for the hardlink/symlink/copy branch, require the source to still be present before removing dst: `if not os.path.exists(src): raise FileNotFoundError(...)` (mirroring the `os.path.isfile(dst)` precondition the move branch has), and/or route the removal through `_trash(dst)` instead of `_unlink_durable(dst)` so an undo is recoverable like every other destructive path in this module. Add a test that applies with method="copy", deletes src, calls undo_place, and asserts dst survives.

**Adversarial verifier:** SURVIVES. I tried hard to refute it and could not find any guard.

Code verified at HEAD (backend/rename/fileops.py:1752-1770): undo_place() takes the `("hardlink","symlink","copy")` branch, and on `os.path.lexists(dst)` calls `_unlink_durable(dst)`. `_unlink_durable` (fileops.py:399-405) is `os.unlink` + `_fsync_directory` — it never calls `_trash`. The comment "The original src still exists" is an unverified assumption; there is no `os.path.isfile(src)` check in undo_place(), in RenameService.undo() (service.py:1784-1796, which I read in full — it goes straight from the status=="applied" check to `_fileops.undo_place(...)`), or in the route (backend/api/routes/rename.py:405-410, a bare pass-through). The `move` branch, by contrast, is careful (no-replace publication, then verified copy). git log -L on those lines shows commit 8e1f951 actually *replaced* `os.remove(dst)` with `_unlink_durable(dst)` — still a hard delete — while adding safety to the move branch only, so the asymmetry is recent and deliberate-looking rather than stale.

EMPIRICAL PROOF (script at C:\Users\NLSur\AppData\Local\Temp\claude\X--Docker-Apps\4d014f7c-ed33-45d7-aec3-c30608af4bed\scratchpad\repro_undo.py, run against the repo copy):
  place_file returned: copy            (os.link monkeypatched to EXDEV, i.e. the real P:->X: case)
  after external cleanup -> src exists: False
  after undo_place -> dst exists: False  src exists: False
  trash entries: none matching the destroyed path
  hardlink case method: hardlink -> dst2 exists: False
So BOTH the copy branch and the plain hardlink branch destroy the last surviving link when the source is already gone, with nothing sent to trash. The finding actually understates it: the default same-volume `hardlink` path is equally fatal, since unlinking dst rem


---

## [MEDIUM] DV auto-sync never quiesces: sync_labels' own back-write advances the watermark that gates it
**Location:** `backend/app_service.py:732`  **Category:** correctness

**Claim:** The DV auto-sync change-detector records the PRE-sync value of MAX(dv_scan.last_seen_at), but sync_labels itself writes dv_scan rows (bumping last_seen_at) for every matched title, so the watermark is stale the instant it is written and a full-library Plex label reconcile fires on every maintenance pass forever.

**Failure scenario:** Host detector imports rows at 10:00. 11:00 maintenance pass: latest = get_latest_dv_scan_at() = '... 10:00:00' (app_service.py:695) > watermark, so sync_labels runs (line 729). For each of the ~444 matched titles it calls db.upsert_dv_scan(..., source='scan') (dv_labeler.py:274-278), which sets last_seen_at = CURRENT_TIMESTAMP (database.py:5152) -> MAX becomes '... 11:00:xx'. Line 732 then assigns self._last_dv_scan_at = latest = '... 10:00:00'. 12:00 pass: latest = '11:00:xx' > '10:00:00' -> full sync again, which stamps 12:00:xx. The loop is stable and self-feeding: every hour (interval_seconds=3600, app_service.py:740) the app enumerates every configured Plex movie library via lib.all() and issues ~444 SQLite upserts, with zero label changes to show for it. The guard's own comment (app_service.py:685-690) says firing it hourly regardless 'would be pure waste' -- that is exactly the shipped behaviour.

**Evidence:** app_service.py:695  latest = self.db.get_latest_dv_scan_at(source="scan")
app_service.py:729  result = dv_labeler.sync_labels(self.db, pm, self.config, additive_only=True)
app_service.py:732  self._last_dv_scan_at = latest
dv_labeler.py:274-278  db.upsert_dv_scan(norm_to_path.get(p, p), index[p], rating_key=str(mv.ratingKey), source="scan")
database.py:5152  last_seen_at = CURRENT_TIMESTAMP
database.py:5168  'SELECT MAX(last_seen_at) AS latest FROM dv_scan WHERE source = ?'
The guarding test cannot see this: tests/test_dv_autosync_watermark.py:32-33 uses a MagicMock db whose get_latest_dv_scan_at returns a FIXED string, and :95-96 patches out backend.rename.dv_labeler.sync_labels entirely, so the back-write never happens. test_watermark_advances_on_success' own docstring names the risk ('every pass would redundantly reconcile the whole library') but the stub makes it unobservable -- the test passes identically whether the watermark is correct or not.

**Suggested fix:** Re-read the watermark AFTER a successful sync (self._last_dv_scan_at = self.db.get_latest_dv_scan_at(source='scan')), or stop the back-write from touching last_seen_at (a dedicated rating_key-only UPDATE). Then extend the test to a real DatabaseManager with the real sync_labels against a fake pm so a second pass is asserted to NOT run.

**Adversarial verifier:** CONFIRMED by execution, not by reading. I tried hard to break it and could not.

I reproduced the loop end-to-end with the REAL `DatabaseManager` (temp SQLite) and the REAL `dv_labeler.sync_labels` (only Plex faked), driving the exact gate arithmetic from app_service.py:695-732. Script: C:\Users\NLSur\AppData\Local\Temp\claude\X--Docker-Apps\4d014f7c-ed33-45d7-aec3-c30608af4bed\scratchpad\repro_dv.py

  watermark after host detector run  : 2026-08-12 11:34:56
  pass N: latest read by the gate    : 2026-08-12 11:34:56
  pass N: sync result                : total=5 matched=5 added=0 removed=0
  pass N: plex label writes          : added=0 removed=0
  pass N: MAX(last_seen_at) AFTER    : 2026-08-12 11:34:58   <- moved by sync's own back-write
  pass N: stored watermark           : 2026-08-12 11:34:56   <- stale the instant it is written
  pass N+1: WOULD SYNC FIRE AGAIN?   : True
  pass N+2: WOULD SYNC FIRE AGAIN?   : True  (self-sustaining)

Every refutation I tried failed:
- Guard elsewhere? None. The only suppressor is matched==0. My control run (library with no matching paths) gives matched=0, MAX unchanged, loop does not start — so the guard fires exactly when the sync would have been pointless anyway, and never in the steady state it was written to protect.
- Unreachable in production? No. `dv_auto_sync_enabled` defaults True (config.py:553), maintenance loop starts unconditionally at app_service.py:581 with interval 3600s.
- Idempotent SQL no-op? No. `ON CONFLICT DO UPDATE SET ... last_seen_at = CURRENT_TIMESTAMP` (database.py:5152) is unconditional; SQLite has no unchanged-row skip.
- Correctly attributed to the window? Yes. The back-write in dv_labeler is pre-existing (b2a4ed2), but the watermark gate that turns it into an hourly self-feeding loop is new here (4db


---

## [MEDIUM] Container-detected DV rows can never match a Plex path, so those detections never label and double-count in the inventory
**Location:** `backend/rename/dv_paths.py:25`  **Category:** correctness

**Claim:** normalize_path only knows the single host-drive<->UNC pair Y: <-> \\TURTLELANDSRV2\4K HDR Geronimo and neither sync_labels caller passes a mappings table, so dv_scan rows written with CONTAINER paths (/library/movies-4k/...) by the container's own detectors can never normalize onto a Plex part.file (Plex is a native Windows install serving F:/G:/Y:/UNC paths).

**Failure scenario:** The durable Plex metadata scan probes /library/movies-4k/Foo (2024).mkv, runs dovi_tool, and persists upsert_dv_scan(path='/library/movies-4k/Foo (2024).mkv', 'fel', source='scan') (plex_metadata_scan.py:443-445; same for /rename/dv-scan-folder via service.py:841-844 and scan_conflict_dv via service.py:909-912). sync_labels indexes that row as '/library/movies-4k/foo (2024).mkv' (dv_labeler.py:232 -> build_index_and_paths -> normalize_path with mappings=None -> DEFAULT_DV_MAPPINGS). Plex reports the part as 'G:\Downloads\Foo (2024).mkv', which normalizes to 'g:/downloads/foo (2024).mkv' (dv_labeler.py:115). They never compare equal, so pick_layer sees no row, the title gets no DV FEL/MEL label, and Kometa's overlay never appears -- while the UI reports a successful DV scan. The same physical file also ends up with TWO dv_scan rows (host-detector 'Y:\...' form plus this container form), so count_dv_scans_by_layer / the /rename/dv-scans panel double-counts it. Compounding finding 1, each such unmatchable write still advances MAX(last_seen_at) and triggers a full-library auto-sync that necessarily matches nothing new.

**Evidence:** backend/rename/dv_paths.py:25-27  DEFAULT_DV_MAPPINGS = [(r"Y:", r"\\TURTLELANDSRV2\4K HDR Geronimo")]   # no container<->host pair
backend/rename/dv_paths.py:53  table = mappings if mappings is not None else DEFAULT_DV_MAPPINGS
backend/app_service.py:729  dv_labeler.sync_labels(self.db, pm, self.config, additive_only=True)        # mappings omitted
backend/api/routes/rename.py:842-845  dv_labeler.sync_labels(reg.db, plex_manager, reg.config, dry_run=..., progress_cb=..., additive_only=...)   # mappings omitted
backend/plex_metadata_scan.py:443-445  self._db.upsert_dv_scan(path=path, dv_layer=result.get("layer"), sig_mtime=st.st_mtime, sig_size=st.st_size, source="scan")
docker-compose.yml:29-30  "F:/Downloads:/library/movies" / "G:/Downloads:/library/movies-4k"   # container paths bear no resemblance to Plex's Windows paths
plex_metadata_scan.py was rewritten in-window (312 lines changed across a725e16/0bdf49e/3d3b32a), which is what made this write path a routine producer of source='scan' rows.

**Suggested fix:** Feed the existing plex_library_path_mappings (backwards: container root -> Plex root) into normalize_path via the mappings argument at both sync_labels call sites, or translate container paths back to their Plex form before upsert_dv_scan in plex_metadata_scan/service. Add a test asserting a container-path row and the corresponding Plex part.file resolve to the same key.

**Adversarial verifier:** The mechanism is real and I could not refute it. DEFAULT_DV_MAPPINGS (dv_paths.py:25-27) holds only the Y: <-> \\TURTLELANDSRV2\4K HDR Geronimo pair, dv_paths.py:53 falls back to it, and neither sync_labels caller passes a mappings table (app_service.py:709-731, api/routes/rename.py:842-845). Meanwhile plex.py:194-202 translates every scan target through plex_library_path_mappings, and config.py:560-583 SHIPS A SEEDED 23-LINE DEFAULT for that key -- so container-form paths (/library/plex-source/..., /library/movies-4k/...) are the default production behavior, killing the obvious refutation that mappings might be unset (and if unset, os.stat would fail and no row would be written at all). plex_metadata_scan.py:300-306 and :443-445 persist that container path with source="scan"; dv_labeler.py:231 reads exactly source="scan" and dv_labeler.py:115 normalizes Plex's host-form part.file. Nothing reverse-translates on read, so those rows are permanently unmatchable.

Downgraded from high to medium for three reasons the finding overstates. (1) No label corruption or stripping: an unmatched movie gives pick_layer -> None -> authoritative False, and both live callers run additive_only, so may_remove is False (dv_labeler.py:161-164). The only removal path is an explicit non-additive manual sync, whose "no match -> remove" policy is pre-existing and not caused by these rows. (2) The durable scan's designed DV consumer is media_inventory (plex_metadata_scan.py:314-321), which is searchable and CSV-exportable including dv_layer (plex.py:381-412); the dv_scan write is a cache reused by dv_scan_is_current. The detection is not lost, only the Plex-label consumer misses it. (3) The gap PREDATES the diff window -- service.py:841 (scan_folder_dv), service.py:909 (scan_conflict_dv), and the


---

## [MEDIUM] An unscanned 4K file silently disappears from the media inventory once a sibling version of the same Plex item is scanned
**Location:** `backend/database.py:4961`  **Category:** correctness

**Claim:** _MEDIA_INVENTORY_EVIDENCE_CTE suppresses a plex_cache 4K file from the `cached_unscanned_4k` projection when ANY media_inventory row shares its rating_key, but media_inventory is keyed per FILE while rating_key identifies a Plex ITEM — so for a multi-version movie, scanning one version makes every other, never-scanned version vanish from the inventory entirely rather than showing as 'unscanned'.

**Failure scenario:** A movie has two 4K versions in one Plex library (same rating_key R1, different media_id/part): /movies/Dune/Dune.FEL.mkv and /movies/Dune/Dune.HDR10.mkv. Before any scan, search_media_inventory(scan_state='unscanned') correctly returns both. After the FEL version alone is scanned (upsert_media_inventory writes path=Dune.FEL.mkv, rating_key=R1), the NOT EXISTS clause matches on rating_key for the HDR10 file, so it is dropped from inventory_candidates. search_media_inventory() now returns 1 row total and search_media_inventory(scan_state='unscanned') returns 0 — the never-scanned 4K file is invisible in the inventory list, the facet counts, the CSV export (/plex/media-inventory/export) and the discrepancy report, and the operator reads 'all 4K scanned'. Reproduced: `unscanned BEFORE any scan: [Dune.FEL.mkv, Dune.HDR10.mkv] total 2` -> after scanning one version -> `unscanned AFTER: [] total 0`.

**Evidence:** database.py:4956-4963 `AND NOT EXISTS ( SELECT 1 FROM media_inventory AS existing WHERE existing.path = pc.file_path OR ( pc.rating_key IS NOT NULL AND existing.rating_key = pc.rating_key ) )`. plex_cache legitimately holds one row per PART for movies — _plex_cache_key() at database.py:3330-3331 returns `f"{rating_key}_{media_id}"`, i.e. multiple rows share one rating_key. Consumers that lose the row: backend/api/routes/plex.py:359 (search), :371 (facets), :403-407 (CSV export), :431 (discrepancies).

**Suggested fix:** Make the de-duplication path-identity-only (`existing.path = pc.file_path`), and if the rating_key clause exists to absorb Plex-view vs container-view path translation, narrow it so it can only suppress a candidate whose path is a translation of the SAME file (e.g. compare translated paths, or additionally require media_id/part identity) rather than any file sharing the Plex item.

**Adversarial verifier:** I tried hard to break this finding and could not. Every premise checks out, and I reproduced the exact symptom against the real CTE text extracted from the file.

WHAT I VERIFIED

1. The code is quoted accurately and is inside the diff window. `backend/database.py:4931-5021` defines `_MEDIA_INVENTORY_EVIDENCE_CTE`; lines 4956-4963 are the `NOT EXISTS (... existing.path = pc.file_path OR (pc.rating_key IS NOT NULL AND existing.rating_key = pc.rating_key))` clause. `git log -S` shows it was introduced by b826c29 ("Fix metadata pilot bootstrap inventory"), and `git diff a4090c3..9227578` includes the whole inventory subsystem as new code.

2. plex_cache really is keyed per FILE, not per item. `backend/plex_service.py:479-532` loops `for media in movie.media` then `for part_idx, part in enumerate(parts)` and emits one dict per part with `'rating_key': movie.ratingKey` and `'key': f"{movie.ratingKey}_{media.id}_{part_idx}"`. Its own comment says media_id "is unique per version, but NOT per part". `_plex_cache_key` (database.py:3316-3331) honors that pre-set key. So N rows share one rating_key by design.

3. media_inventory really is keyed per FILE. Schema at database.py:713-733 is `path TEXT PRIMARY KEY`; `upsert_media_inventory` conflicts on `path`.

4. The rating_key column on media_inventory is actually populated, which is what makes the NOT EXISTS fire. I checked this specifically as a refutation route (if it were always NULL, the clause would be inert): `_movie_targets_for_scope` (backend/api/routes/plex.py:200-207) puts `"rating_key": m.get("rating_key")` on every target; `create_metadata_scan_items` (database.py:4721-4753) persists it; `_process_one_durable` spreads `**item` into `upsert_media_inventory` at plex_metadata_scan.py:314-319 and 270-274, and `_record_faile


---

## [MEDIUM] undo reports success and marks the job 'reverted' when undo_place did nothing at all
**Location:** `backend/rename/fileops.py:1760`  **Category:** correctness

**Claim:** undo_place()'s move branch is guarded only by `if os.path.isfile(dst)`; when dst is absent it silently returns None, and undo() cannot distinguish "restored" from "did nothing", so it flips the job to status=reverted and returns ok:True even though no file was moved back.

**Failure scenario:** A job was applied with move_method="move" (source consumed). The user later renames or moves the library file directly in Windows/Plex, then clicks Undo in ScanHound to get the original back. `os.path.isfile(dst)` is False, undo_place returns immediately having touched nothing, undo() marks the row status="reverted", reverted_at=now and the API returns 200 {"ok": true}. The UI tells the user the rename was undone and the file is back at original_path; in reality nothing was restored, and the DB now asserts a state ("reverted" ⇒ the file is at original_path) that every downstream reader trusts. This is exactly the "failed operation reported as success" class — and because the job is no longer 'applied', undo can never be retried (service.py:1789-1790).

**Evidence:** fileops.py:1759-1760:
    elif method == "move":
        if os.path.isfile(dst):
(no else, no return value, no raise). `git show 8e1f951` removed the only other precondition this branch had:
-        # src was consumed; move dst back to it.
-        if os.path.exists(src):
-            raise FileExistsError(f"Original path already occupied: {src}")
service.py:1793-1796 calls it inside a try that only converts exceptions to errors, then service.py:1835-1847:
        reverted_ok = db.update_rename_job(job_id, status="reverted", reverted_at=_now())
        ...
        return {"ok": True, "restore_warning": restore_warning}
The same silent-no-op also applies when `job.get("new_filename")` is empty — service.py:1792 builds `dst = os.path.join(destination_path or "", new_filename or "")`, which for an empty filename yields the destination DIRECTORY; `os.path.isfile(dir)` is False, so undo is a no-op that still reports success.

**Suggested fix:** Make undo_place return (or raise) a definite outcome: raise FileNotFoundError when method=="move" and dst is absent, and have undo() surface {"ok": False, "error": "The renamed file is no longer at <dst>; nothing was restored"} instead of writing status=reverted. Also reject an empty new_filename in undo() before building dst.

**Adversarial verifier:** NOT REFUTED — the code says exactly what the finding claims, and I reproduced the no-op. But the severity is overstated; I'd call it medium.

What I verified:

1. fileops.py:1752-1770 (`undo_place`) — the move branch is `elif method == "move": if os.path.isfile(dst): ...`. There is no `else`, no return value, no raise. I ran it: place_file(src,dst,"move") consumed src, then I renamed dst externally and called `undo_place(src, dst, "move")` → returned `None`, no exception, `src` still absent, the file still at the user's new path. Silent no-op is real.

2. service.py:1784-1847 (`undo`) — the only failure channel is the try/except around undo_place, which converts exceptions to `{"ok": False}`. A clean return falls straight through the best-effort trash-restore block into `db.update_rename_job(job_id, status="reverted", reverted_at=_now())` and `return {"ok": True, "restore_warning": None}`. Route rename.py:405-410 turns that into HTTP 200; frontend `run(job.id, undoJob, 'Reverted')` (+page.svelte:394,453) toasts "Reverted" on any non-throwing call. So the false-success report reaches the user verbatim.

3. Retry is genuinely blocked afterward: undo() requires `status == "applied"` (service.py:1791-1792), and status is now "reverted". Terminal.

4. No test covers it. tests/test_rename_core.py:82-149 covers move+undo with dst PRESENT (incl. the EXDEV path); tests/test_rename_service.py:1142-1178, 2393-2400, 3124-3133 all undo with the file in place. Nothing asserts behavior when dst is gone.

Refutation attempts that FAILED:
- "The removed FileExistsError guard was a regression" (the finding's own evidence) — actually not: 8e1f951 replaced `shutil.move` with `_move_no_replace_durable`, which enforces no-replace internally, so that removal was a legitimate refactor. But it 


---

## [MEDIUM] The only automatic release of the verification hold rests on a producer field no test ever executes
**Location:** `tests/test_verification_hold.py:508`  **Category:** missing-contract-test

**Claim:** `source_reveal_succeeded` is the sole key that releases a verification hold, but every test that exercises the release path injects the flag from a hand-built dict through a MagicMock'd download service — the two production lines that actually produce and carry the flag are never run by any test.

**Failure scenario:** Delete backend/download_service.py:3169 (or drop the `source_reveal_succeeded` key from `public_download_result` at backend/download_outcome.py:495). The entire test suite stays green — test_the_load_bearing_negative_control, test_a_delivering_probe_releases_the_siblings_with_spacing, test_a_held_source_holds_a_second_batch_until_a_probe_succeeds and test_a_reveal_success_with_a_failed_delivery_still_releases_the_hold all still pass, because each one hands the queue a literal dict containing the flag rather than letting DownloadService produce it. In production the operator promotes a probe with 'Retry now', HDEncode serves the links, JDownloader accepts them, the item completes — and `_release_verification_hold` sees a falsy value, so `verification_hold_source` stays set. `decide()` (backend/queue_recovery_policy.py:273) then returns VERIFICATION_HOLD for every sibling forever: 21 items wedged, `_maybe_auto_resume` promotes nothing, `retry_ready` skips them (download_queue.py:1719), `resume_batch` raises (download_queue.py:1883). The only exit is POST /downloads/verification-hold/clear, which has no frontend caller (grep of frontend/ for 'verification-hold' returns nothing).

**Evidence:** Consumer: backend/download_queue.py:956 `if outcome.get("source_reveal_succeeded"): UPDATE download_queue_batches SET verification_hold_source = NULL ...` (docstring at :942 — "THE ONLY RELEASE OF A VERIFICATION HOLD"). Producer: backend/download_service.py:3134 `reveal_served = bool(links)` and :3169 `result["source_reveal_succeeded"] = reveal_served`. Carrier: backend/download_outcome.py:495 `"source_reveal_succeeded": bool(source.get("source_reveal_succeeded"))`, which download_queue.py:792 applies to every outcome before `_complete`/`_fail`. Test side: `_rig()` at tests/test_verification_hold.py:530 does `download = MagicMock()`, and the only sources of a True value are the literals at tests/test_verification_hold.py:508 (`_success_outcome`) and :525 (`_reveal_ok_delivery_failed_outcome`). A repo-wide grep for `source_reveal_succeeded` returns only those two test lines plus the three production lines. Contrast the sibling field `source_progress`, which has a dedicated end-to-end contract suite (tests/test_source_progress_contract.py:63 `test_a_REAL_jdownloader_success_sets_it_end_to_end`, written after exactly this defect class shipped: "Seventeen tests passed, because both suites fabricated transport_attempted=True in hand-built outcome dicts", tests/test_source_progress_contract.py:12).

**Suggested fix:** Add the `source_reveal_succeeded` twin of tests/test_source_progress_contract.py: run the real `DownloadService.download_item` with `scrape_links` returning links and `send_to_jdownloader` stubbed, push its OWN returned dict through the real `public_download_result`, and assert the mapped outcome releases the hold via `DownloadQueueService._release_verification_hold` against a real DatabaseManager. Include the negatives already argued in the docstrings but never executed against the producer: a pre-scrape duplicate (returns at download_service.py:3081, before :3169) and a pasted direct file-host link (links=[url] at :3142, after `reveal_served` is captured) must both leave the flag False.

**Adversarial verifier:** SURVIVES, but two of its supporting claims are wrong and the impact is overstated, so I downgrade high -> medium.

WHAT I VERIFIED EMPIRICALLY (mutation experiment on a whole-tree copy at ...\scratchpad\mut):
1. Baseline: `python -m pytest tests/test_verification_hold.py tests/test_source_progress_contract.py tests/test_download_service.py tests/test_repeatable_batch_resume.py tests/test_auto_resume_diagnostics.py -q` -> 311 passed.
2. Mutated backend/download_service.py:3169 (`result["source_reveal_succeeded"] = reveal_served` -> `pass`) -> the same 311 passed, unchanged. Then additionally deleted backend/download_outcome.py:495 -> still green on that set. (The full-suite run was still going at cutoff, but a repo-wide grep shows the symbol exists in only 5 places: download_outcome.py:495, download_queue.py:956, download_service.py:3051/3169, and the two test literals at tests/test_verification_hold.py:508 and :525 — nothing else can observe it.)
3. Kill-signal proof: I wrote the missing contract as a standalone script (C:\Users\NLSur\AppData\Local\Temp\claude\X--Docker-Apps\4d014f7c-ed33-45d7-aec3-c30608af4bed\scratchpad\prove_release.py) — real DownloadService.download_item (only scrape_links / send_to_jdownloader / save_to_history stubbed) -> real public_download_result -> real DownloadQueueService._complete against a real SQLite batch with verification_hold_source='hdencode'.
   - Clean tree: raw True, mapped True, hold 'hdencode' -> None. RELEASED.
   - Mutated tree: raw False, mapped None, hold stays 'hdencode'. STILL HELD.
So a mutation that demonstrably breaks the only automatic release in production is invisible to the suite. The coverage gap is real.

WHERE THE FINDING IS WRONG:
(a) "the two production lines that actually produce and carry the flag are never r


---

## [MEDIUM] The fileops writer-guard contract is a hand-maintained allowlist, so a new unguarded mutator is invisible to it
**Location:** `tests/test_fileops_writer_guard_contract.py:15`  **Category:** guard-test-cannot-catch-the-regression-it-names

**Claim:** `test_all_file_mutation_entry_points_guard_first` only verifies that the eleven functions someone already listed still guard first; it derives nothing from the module, so any newly added file-mutating entry point that omits `require_writer_lock()` passes the contract silently.

**Failure scenario:** A future change adds, say, `def purge_orphan_parts(root)` to backend/rename/fileops.py that walks a library and unlinks stray `.part` files, and the author forgets `require_writer_lock()` as its first statement. Both tests in this file pass unchanged — the subset assertion at line 49 does not notice a new name, and the parametrize at line 71 does not include it. The new function then deletes files from a second process (or during shutdown) with no writer-lock ownership, which is precisely the process-lifetime invariant this file's docstring says it enforces, and the destructive-operation risk the whole runtime_lock mechanism exists for.

**Evidence:** tests/test_fileops_writer_guard_contract.py:15-27 defines `_GUARDED_MUTATIONS` as a literal set of eleven names, and line 49 asserts only `_GUARDED_MUTATIONS <= functions.keys()` — a subset check that can never fail on an ADDED function. The per-function loop at lines 51-56 iterates `sorted(_GUARDED_MUTATIONS)`, never `functions`. The complementary executable test (lines 71-88) is likewise a fixed three-entry parametrize (`place_file`, `sweep_trash`, `restore_trash_entry`). tests/test_runtime_lock.py contains no fileops enumeration either (its only references are to `require_writer_lock` itself at lines 18, 41, 50). backend/rename/fileops.py already has module-level mutators outside the list that call `os.unlink`/`os.rmdir`/`os.remove` directly (`_remove_empty_bucket` at :814, `_cleanup_prepared_trash` at :845, `_copy_then_unlink_to_trash` at :856, `_restore_no_replace` at :1137, `_unlink_durable` at :399), so the boundary between 'entry point' and 'internal helper' is a judgement encoded only in the literal set.

**Suggested fix:** Invert the check: walk `tree.body` for module-level functions whose body contains a call to a known mutating primitive (`os.unlink`, `os.remove`, `os.rmdir`, `os.replace`, `os.link`, `os.symlink`, `os.makedirs`, `shutil.move`, `shutil.copy*`, `_move_no_replace*`, `_copy_verify_atomic`, `_trash`) and assert each such function either starts with `require_writer_lock()` or appears in an explicit, comment-justified `_INTERNAL_HELPERS` exemption set. That makes adding a mutator fail the test by default rather than pass by omission.

**Adversarial verifier:** I tried hard to refute this and could not. Every element of the evidence checks out, and I reproduced the blind spot empirically.

1. The test is exactly as described. `C:\...\scratchpad\ScanHound\tests\test_fileops_writer_guard_contract.py:15-27` defines `_GUARDED_MUTATIONS` as a literal 11-name set; line 49 is `assert _GUARDED_MUTATIONS <= functions.keys()` — a subset check that is monotone in the module's function set and therefore mathematically incapable of failing when a function is ADDED. The per-function loop at 51-56 iterates `sorted(_GUARDED_MUTATIONS)`, never `functions`. The executable test at 71-88 is a fixed three-lambda parametrize.

2. No second enforcement layer exists. `grep -rn "require_writer_lock"` across the repo returns only the definition (`backend/runtime_lock.py:202`), the 11 call sites inside `backend/rename/fileops.py` (lines 675, 882, 985, 1178, 1288, 1378, 1448, 1493, 1588, 1696, 1754), the contract test's string assertion, and three references in `tests/test_runtime_lock.py` (18, 41, 50) that test the primitive itself, not its call sites. `require_writer_lock()` is a plain function with no import hook, no `os` monkeypatch, no decorator registry — nothing enforces the invariant except a hand-written first line in each function. Only two modules import fileops at all (`backend/app_service.py:592`, `backend/rename/service.py:27`), neither of which adds a guard.

3. I derived the guarded set from the module by AST and got exactly the 11 names in the literal set, so the test passes today and the invariant is currently intact — there is no live production bug here, which is why this is a test-quality finding and not a correctness one.

4. Proof of the blind spot: I copied `backend/rename/fileops.py` to `C:\Users\NLSur\AppData\Local\Temp\claude\X


---

## [LOW] retry_item on a claimed/completed/cancelled row reports success while retrying nothing — and still destroys the batch's shared source cooldown
**Location:** `backend/download_queue.py:1648`  **Category:** correctness

**Claim:** retry_item never checks the rowcount of its item UPDATE, so when the row's state is outside the allowed set the item is untouched and HTTP 200 is returned with the unchanged row, while the unconditional second statement wipes the batch's cooldown_until and forces state='scheduled'.

**Failure scenario:** Verified on a real DB: a row is claimed by the worker (transport may be live) and its batch carries a shared source brake (state='paused_source', cooldown_until=+1h). cancel_item on that row correctly raises a 409 download_queue_item_claimed. retry_item on the SAME row returns a 200 payload with state still 'claimed' — the operator is told the retry was scheduled and nothing was retried — and afterwards the batch reads state='scheduled', cooldown_until=None. That cleared brake is the WAITING_BRAKE veto decide() reads (queue_recovery_policy.py:286-287), and 'paused_source' is the key both _warn_exhausted_batches (:1257) and the `stuck` diagnostic (:1577) filter on, so one no-op click removes the source-protection cooldown for the whole batch and hides that batch from both diagnostics.

**Evidence:** download_queue.py:1648-1668 the item UPDATE is guarded by `AND state IN ('verification_required','waiting_source','failed','scheduled','ready')` but its `.rowcount` is discarded; :1669-1676 the batch UPDATE `SET state = 'scheduled', cooldown_until = NULL` runs unconditionally; :1679-1681 returns `self.get_item(item_uuid) or item` and the route (api/routes/downloads.py:274-286) returns it as 200. Contrast cancel_item (:2171-2172), which raises DownloadQueueItemClaimed for the same 'claimed' state, and _complete/_fail/_pause_for_source, which all check `rowcount != 1` and log "ignored stale ...". `list_retries` (:2291-2294) surfaces 'claimed' rows to the UI, so the button is reachable on exactly the state that no-ops.

**Suggested fix:** Capture the item UPDATE's rowcount; if it is not 1, raise DownloadQueueItemClaimed for 'claimed' (mirroring cancel_item) or a DownloadQueueError for completed/cancelled, and skip the batch UPDATE entirely so the shared cooldown and paused_source state survive when nothing was promoted.

**Adversarial verifier:** CORE MECHANIC SURVIVES, STATED IMPACT DOES NOT.

What is verifiably true (backend/download_queue.py:1638-1681):
- The item UPDATE at :1648 is guarded by `AND state IN ('verification_required','waiting_source','failed','scheduled','ready')` and its `.rowcount` is never inspected. Contrast _pause_for_source (:1043-1049), _complete (:893), _fail (:979) and cancel_item (:2181-2183), which all check rowcount and either log "ignored stale ..." or return False.
- The batch UPDATE at :1669-1676 (`SET state='scheduled', cooldown_until=NULL`) is unconditional and does NOT depend on the first statement having matched.
- _refresh_batch_locked (:1126-1168) does not undo it: it writes `state = COALESCE(?, state)` and only ever supplies 'completed' (when active==0), so a batch with any active child keeps the just-written 'scheduled'. (One partial guard the reviewer missed: for a fully completed/cancelled batch the refresh does rewrite state to 'completed', so that sub-case self-corrects.)
- The route (backend/api/routes/downloads.py:274-286) has no state precondition and returns the unchanged row as 200; frontend/src/lib/components/VerificationRetries.svelte:206-212 disables "Remove" for `item.state === 'claimed'` but leaves "Retry now" enabled (it gates only on `retry_available`, which list_retries computes purely from the hdencode coordinator, :2315-2317). retry() then toasts "Retry scheduled" (:91-92). So the no-op IS reachable from the UI on a 'claimed' row, and the operator is told it worked.

Where the finding is WRONG (this is why I downgrade):
1. "Removes the source-protection cooldown for the whole batch" is not what happens. _pause_for_source writes cooldown_until to the triggering item, to every same-source sibling (:1050-1073) AND to the batch (:1074-1093). decide() (queue


---

## [LOW] New /rss API prefix shadows the new /rss SPA page: direct navigation returns 401 JSON instead of the app
**Location:** `backend/api/main.py:519`  **Category:** interaction-defect

**Claim:** Adding rss.router in this window put "rss" into app.state.protected_segments, and because _request_requires_auth classifies a request purely by its first path segment, a browser HTML navigation to /rss (the new SvelteKit page added in the same window) is treated as an unauthenticated API call and answered with 401 {"detail":"Unauthorized"} before the SPA catch-all can serve index.html.

**Failure scenario:** Admin is logged in (token in localStorage). They bookmark https://scanhound.turtleland.us/rss, or press F5 on that page, or middle-click the RSS nav item to open it in a new tab. The browser issues a top-level GET /rss with no Authorization header (browsers never attach bearer tokens to navigations). _request_requires_auth returns True (segment "rss" is protected, a password is set), _token_authorized("") is False, _dv_ingest_authorized is False -> the middleware returns the JSON 401 at main.py:690. The user sees a raw JSON error page instead of the RSS Operations screen, and the only recovery is to navigate to / and click through. The same collision already exists for /settings, /analytics, /pipeline and /watchlist (those routers pre-date the window; /settings additionally returns the settings JSON body once a token is present, never the page) — /rss is the instance this diff newly created.

**Evidence:** backend/api/main.py:704 `rename.router, pipeline.router, rss.router,` -> :712 `app.state.protected_segments = _compute_protected_segments(api_routers)`; rss.py:22 `router = APIRouter(prefix="/rss", tags=["rss"])` so _compute_protected_segments (main.py:493-495) adds "rss". main.py:518-521: `protected = getattr(request.app.state, "protected_segments", frozenset())` / `segment = request.url.path.lstrip("/").split("/", 1)[0]` / `if segment not in protected: return False`. The SPA page exists at frontend/src/routes/rss/+page.svelte and is a first-class nav destination (frontend/src/lib/icons.ts:23 `{ href: '/rss', label: 'RSS Operations', ... }`, frontend/src/routes/+layout.svelte:55). frontend/src/routes/+layout.ts sets `export const prerender = false;` and frontend/svelte.config.js uses `adapter-static({ fallback: 'index.html' })`, so no /rss/index.html is emitted — /rss can only be served by the `@app.get("/{full_path:path}")` fallback at main.py:734, which the middleware never reaches.

**Suggested fix:** Do not decide API-vs-SPA on the leading segment alone. Either (a) resolve the request against app.router first and only apply the bearer gate when an APIRoute actually matched, or (b) in the middleware, let a GET whose Accept header prefers text/html and whose path matches no API route fall through to _serve_spa. Option (b) is a two-line change and keeps the fail-closed posture for every real API path, since API clients send Accept: application/json.

**Adversarial verifier:** Reproduced empirically, not just by reading. I instantiated the real app from backend/api/main.py with a stub SCANHOUND_FRONTEND_DIR containing index.html, forced _auth_enabled() True (models "a password is set"), and issued token-less HTML navigations. Result: GET /rss -> 401 application/json {"detail":"Unauthorized"}; GET / -> 200 text/html. app.state.protected_segments does contain "rss". The mechanism the reviewer describes is exactly right: _compute_protected_segments (main.py:483-502) lifts "rss" off rss.py:22's APIRouter(prefix="/rss"), _request_requires_auth (main.py:518-521) classifies purely by first path segment, and the middleware (main.py:681-694) 401s before routing ever reaches the SPA catch-all at main.py:734.

Every refutation avenue I tried failed. (1) Frontend-not-served: Dockerfile:64 copies frontend/build into the image and sets SCANHOUND_FRONTEND_DIR=/app/frontend/build, so the catch-all IS mounted in production. (2) Escape hatch: SCANHOUND_ALLOW_OPEN appears nowhere in docker-compose.yml; and with no credential row at all, _request_requires_auth falls through to the fail-CLOSED `return True` at main.py:528, so /rss 401s in BOTH credential states. (3) Prerendered /rss/index.html: irrelevant — the middleware precedes routing, so the 401 fires regardless of on-disk files (and svelte.config.js fallback + +layout.ts prerender=false mean it does not exist anyway). (4) Existing coverage: nothing in tests/ asserts SPA deep-link behavior; test_security_review_20260731.py:51 only mentions the catch-all in passing.

One refinement to the reviewer's evidence: rss.py defines no route matching "/rss" exactly (only /rss/status, /rss/candidates, ...), so WITH a token /rss falls through to the SPA and renders fine. It is strictly the token-less browser navigation 


---

## [LOW] Session token is passed in the WebSocket URL query string and is written verbatim to uvicorn and reverse-proxy access logs
**Location:** `backend/api/ws.py:127`  **Category:** credential-exposure

**Claim:** The /ws handshake takes the 30-day session token (or the desktop nonce) as a URL query parameter, so uvicorn's WebSocket access-log line — which renders path *and* query string — records a live, replayable whole-API credential in container stdout on every connect, and the same value lands in NPM's access log and in the Cloudflare tunnel's request records.

**Failure scenario:** Admin logs in; the browser opens ws://scanhound:9721/ws?token=<32-byte session token>. uvicorn writes `172.18.0.5:41022 - "WebSocket /ws?token=8Kf..." [accepted]` to stdout. Anyone who can run `docker logs scanhound` (or who receives shipped logs, or reads NPM's /data/logs/proxy-host-*.log) copies that token and replays it as `Authorization: Bearer <token>` against POST /rename/jobs/{id}/apply, POST /rename/trash/empty, or PUT /settings for the next 30 days — with no password and no further interaction. Rotating the password (POST /auth/set-password -> delete_all_sessions) is the only revocation, and nothing prompts it because the leak leaves no trace on the auth path itself. NOTE: the query-parameter transport pre-dates a4090c3; it is reported because session-token handling and credentials-in-URLs were explicitly in scope, and because ws.py:126-138 was rewritten in this window (the fail-closed SH-H01 fix), which is the natural place to correct it.

**Evidence:** backend/api/ws.py:127 `async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):` and :135 `if not token_authorized(token):`. Producer side: frontend/src/lib/stores/connection.ts:97 `const wsUrl = nonce ? `${base}?token=${encodeURIComponent(nonce)}` : base;`. token_authorized (dependencies.py:258-277) accepts the desktop nonce OR an unexpired session row, and auth_service.SESSION_TTL_DAYS = 30, so the value in the URL is exactly the credential that admits every protected HTTP route via the Authorization header. uvicorn's websockets implementation logs `'%s - "WebSocket %s" [accepted]'` with get_path_with_query_string(scope), i.e. including `?token=...`.

**Suggested fix:** Move the credential out of the URL: send it in the WebSocket subprotocol header (`new WebSocket(url, ['bearer', token])` client side, read ws.headers['sec-websocket-protocol'] and echo the accepted subprotocol in ws.accept()), or issue a single-use short-TTL ticket from an authenticated POST /auth/ws-ticket and put only that opaque ticket in the query string. If neither is acceptable short-term, at minimum disable uvicorn's access log for the WS path and confirm NPM is not logging the query string — but the header/ticket fix is the one that actually removes the credential from every intermediary.

**Adversarial verifier:** Every link in the chain verified, and I proved the logging step empirically rather than by inference.

CONFIRMED: (1) backend/api/ws.py:127 is verbatim `async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):` with `token_authorized(token)` at :135. (2) dependencies.py token_authorized accepts the desktop nonce OR an unexpired session row via `db.get_session_expiry(auth_service.hash_token(token))`; auth_service.py:31 SESSION_TTL_DAYS = 30 — so the query value is the same whole-API credential the HTTP bearer middleware honors. (3) The producer side is real for the WEB deployment, not just desktop: frontend/src/lib/stores/auth.ts:41-43 `setStoredToken(token); setAuthNonce(token)` stores the SESSION TOKEN from authLogin, and connection.ts:96-97 reads it back into `?token=`. The name "nonce" is misleading — post-login it holds the session token. (4) The container runs uvicorn at INFO: docker/entrypoint.sh -> `python -m backend.api` -> __main__.py:26 `uvicorn.run(app, ..., log_level="info")`, with uvicorn[standard] + websockets in requirements-docker.txt:48-49.

BEST REFUTATION ATTEMPT, AND WHY IT FAILS: ScanHound has a CredentialMaskingFilter (app_service.py:200-220) whose regex DOES match `token=...` — I confirmed it rewrites the exact line to `"WebSocket /ws?token=8Kf***"`. But it is attached only to ROOT handlers (app_service.py:304-317). uvicorn's WS line is emitted on `uvicorn.error`, which has no handlers and propagates to `uvicorn`, which owns an unfiltered StreamHandler with propagate:false — so it never reaches root. Live repro (fastapi 0.137.1 / uvicorn 0.49.0 / websockets 16.0) with the real filter installed on a root handler tagged [ROOTHANDLER] printed: `INFO:     127.0.0.1:34826 - "WebSocket /ws?token=8KfQzR1SECRETSESSIONTOKEN" [accepted]`


---

## [LOW] A file whose own DV probe failed is reported carrying a sibling file's DV layer via the rating_key evidence fallback
**Location:** `backend/database.py:4995`  **Category:** correctness

**Claim:** evidence_base resolves scan_layer as `COALESCE(live_path.dv_layer, live_rating.scan_layer)`, where live_rating aggregates dv_scan by rating_key — so a media_inventory row with no DV evidence of its own inherits the DV layer detected on a DIFFERENT file of the same Plex item, and the derived `discrepancy` column then reports that borrowed evidence as if it were the file's own.

**Failure scenario:** Same multi-version movie (rating_key R1). Version A scans successfully as FEL and gets a dv_scan row. Version B's probe FAILS, so _record_failed_inventory writes a media_inventory row with scan_state='failed' and no dv_layer, and no dv_scan row is written for it. search_media_inventory() then reports version B with scan_layer='fel' and discrepancy='live_only', and media_inventory_facets()['discrepancy'] shows `[{'value':'live_only','count':2}]` — two files verified as having live DV evidence when only one was ever measured. This is the 'process failed' vs 'found nothing' conflation the project already fixed once in dv_scan (upsert_dv_scan's 'unknown' preserve-on-worse rule at database.py:5140): the per-file rule is enforced in dv_scan but bypassed by this adjacent per-rating_key aggregate. Reproduced: `Dune.HDR10.mkv | scan_state: failed | own dv_layer: None | scan_layer(evidence): fel | discrepancy: live_only`.

**Evidence:** database.py:4984-4990 `live_by_rating AS ( SELECT rating_key, CASE WHEN COUNT(DISTINCT lower(dv_layer)) = 1 THEN MIN(lower(dv_layer)) ELSE 'conflict' END AS scan_layer FROM dv_scan WHERE source = 'scan' … GROUP BY rating_key )` and database.py:4995 `COALESCE(live_path.dv_layer, live_rating.scan_layer) AS scan_layer`; the identical shape exists for seeds at 4976-4982/4994. discrepancy is derived from these at database.py:5005-5019 and is exported by list_metadata_discrepancies (database.py:5083) and the /plex/metadata-scans/{uuid}/discrepancies route (backend/api/routes/plex.py:431).

**Suggested fix:** Only apply the rating_key fallback when the Plex item has exactly one media part (or when the candidate row itself has no competing sibling in media_inventory/dv_scan), and never let it supply scan_layer/seed_layer for a row whose scan_state is 'failed' or 'unscanned'. Alternatively expose the borrowed value under a distinct column (e.g. item_scan_layer) so discrepancy is computed only from per-file evidence.

**Adversarial verifier:** MECHANISM CONFIRMED, but the finding's stated harm is overstated and the design context materially changes the severity.

What I verified:
1. The SQL does what the reviewer says. I rebuilt the four tables (media_inventory, dv_scan, dv_seed_baseline, plex_cache) in an in-memory SQLite DB, extracted the literal `_MEDIA_INVENTORY_EVIDENCE_CTE` text from backend/database.py, inserted two media_inventory rows sharing rating_key 'R1' (one scan_state='current' with a dv_scan fel row, one scan_state='failed' with none), and got exactly:
   ('/m/Dune.DV.mkv','current',scan_layer='fel',discrepancy='live_only')
   ('/m/Dune.HDR10.mkv','failed',scan_layer='fel',discrepancy='live_only')
   So the borrowing is real, not a misread.
2. The precondition is reachable and routine, not exotic. plex_cache is keyed `rating_key_media_id` (backend/database.py:3331) and backend/plex_service.py:523 documents that media_id is one-per-version — so multi-version movies are explicitly modeled. `_movie_targets_for_scope` (backend/api/routes/plex.py:170-208) applies NO resolution filter on a full scan, so every version of every movie lands in media_inventory sharing a rating_key. The much more common trigger is not a failed probe at all: any non-DV sibling version (e.g. a 1080p copy of a 4K DV movie) that scans successfully also inherits scan_layer='fel'/'live_only'.
3. No guard exists. _record_failed_inventory (backend/plex_metadata_scan.py:357) does pass rating_key through **item, upsert_media_inventory stores it, and the dovi-failure branch (plex_metadata_scan.py:290-296) writes NO dv_scan row — so live_path is NULL and live_rating wins, exactly as claimed.

WHY I DOWNGRADE TO LOW:
a) The reviewer's cited consumer is wrong. list_metadata_discrepancies (backend/database.py:5083) filters `discrepancy


---

## [LOW] A failed miss-rows query is indistinguishable from "no misses": every miss-derived readiness blocker silently evaluates to zero
**Location:** `backend/database.py:2761`  **Category:** fail-open

**Claim:** `get_hdencode_miss_resolution` loads the miss rows with `_query_dicts(..., default=[])`, and `_query` swallows every exception and returns the default — so a read failure against `hdencode_shadow_misses` produces an empty miss list, which makes `never_acquired`, `undetermined` and `not_yet_assessable` all zero and removes every miss-derived reason from the readiness result, while `evidence_problems` stays empty.

**Failure scenario:** `hdencode_shadow_misses` becomes unreadable — a corrupted page, a column rename during a partial migration, or an sqlite error on that table specifically — while `hdencode_shadow_cycles` reads fine. `eligible` (database.py:2286-2299) still returns the legacy pre-`listing_complete` rows: cycles >= 20, span >= 7 days, request_reduction > 0, recovery_cycles >= 1. Every miss query returns `[]`, so `misses_never_acquired`, `misses_undetermined`, `misses_not_yet_assessable`, `unattributed_candidates`, `miss_evidence_integrity` and `evidence_problems` are all empty. `reasons` is empty, `ready` is True, and `POST /rss/mode {"mode":"rss_primary"}` (api/routes/rss.py:282) succeeds — promotion granted on evidence that was never actually read. This is the "process failed vs found nothing" conflation the rest of this module explicitly guards against everywhere else (see the `_normal_feed_outcomes` docstring at database.py:2816-2822, which makes exactly this distinction one layer down).

**Evidence:** database.py:2761-2768 `for row in self._query_dicts("SELECT m.canonical_url AS url, m.media_type ... FROM hdencode_shadow_misses m JOIN hdencode_shadow_cycles c ...", default=[]):`. database.py:190-202 `_query` wraps the whole execution in `try: ... except Exception as e: logger.error("DB query error: %s", e); return default`. The same pattern hides a failure in `get_hdencode_shadow_summary`'s integrity loader (database.py:2362-2370, `default=[]`) and its orphan check (database.py:2465-2469, `default=None` -> `orphan_count=0`). All of the miss-side readiness reasons are conditioned on non-zero counts from these loaders: database.py:2891-2907 and 2919-2928. Nothing records that the read failed, so `resolution.get("evidence_problems")` is empty and `miss_evidence_integrity` is empty.

**Suggested fix:** Give the readiness-critical loaders a sentinel default that cannot be confused with an empty result — e.g. call `_query_dicts` with `default=None` and treat `None` as an evidence problem (`problems.append("miss_rows_unreadable")` / `integrity.append("miss_rows_unreadable")`) rather than as zero rows, so the existing `miss_resolution_evidence_unreadable` and `miss_evidence_integrity_failed` reasons fire. Same for the integrity loader and the orphan count.

**Adversarial verifier:** Reproduced empirically, but the reviewer's own stated scenario is the variant that is already guarded, and the real reachable paths are narrower than claimed.

Setup: a real DatabaseManager fixture that otherwise passes readiness (20 eligible cycles, 8-day span, reduction>0, 1 recovery cycle, both feeds 304/fresh) plus one real miss row. Baseline readiness is correctly False with reason miss_resolution_undetermined.

GUARD THE REVIEWER MISSED (partially refutes the stated scenario): the count reconciliation at database.py:2477-2491 reads relevant_miss_count from hdencode_shadow_cycles alone and compares it against rows counted from the misses join. When the misses table is wholly unreadable, per_cycle is empty, total=0, and every provenance-aware cycle with a stored count emits count_without_rows:<cycle>:<n> -> integrity finding -> miss_evidence_integrity_failed at 2927. Measured: provenance-aware cycles + whole-table read failure => ready=False, reasons=['miss_evidence_integrity_failed']. So "a corrupted page / a column rename / an sqlite error on that table specifically", the exact scenario in the finding, fails CLOSED for data in the shape the current writer produces (the writer stores {} or a _derived_from marker rather than NULL, per the comment at 2321).

WHAT SURVIVES (two narrower paths):
1) A failure isolated to the single statement at database.py:2761 while get_hdencode_shadow_summary's own miss query (2362) succeeds. Measured: provenance-aware cycles, only the 2761 query blinded => ready=True, reasons=[], misses_undetermined 1 -> 0. The reconciliation sees consistent counts, evidence_problems is empty, and every miss-derived reason vanishes. Reachable via any transient per-statement sqlite error (lock timeout, disk I/O error), which _query swallows at 190-202


---

## [LOW] Symlinked trash bucket is refused by sweep/repair but accepted by delete/restore/list — /trash/delete can unlink a file outside the trash root
**Location:** `backend/rename/fileops.py:1387`  **Category:** security

**Claim:** The two sweep paths explicitly refuse a bucket directory that is a symlink, but the three entry points reachable from the HTTP API (delete_trash_entry, restore_trash_entry, list_trash_entries) test only os.path.isdir(), which follows symlinks — so the confinement rule is enforced in one layer and bypassed by the adjacent one.

**Failure scenario:** Anything with write access to a directory that hosts a `.scanhound-trash` fallback root (another container on the same share, or an extracted archive dropped into the JD output folder) creates `.scanhound-trash/20260810-120000` as a symlink to /library/Movies. GET /rename/trash lists the linked directory's contents as trash entries (no islink check at :1011). POST /rename/trash/delete {"bucket":"20260810-120000","name":"Some Movie (2019).mkv"} passes _is_safe_component, passes `os.path.isdir(bucket_path)` (follows the link), and os.unlink permanently deletes the real library file — no trash, no recovery. The retention sweep would have skipped that same bucket. POST /trash/empty is safe only because sweep_trash has the guard; the primitive it calls does not.

**Evidence:** Guarded (fileops.py:1198 and 1609, identical text):
            if os.path.islink(bucket_path) or not os.path.isdir(bucket_path):
                continue
Unguarded:
  fileops.py:1011  (list_trash_entries)   if not os.path.isdir(bucket_path):
  fileops.py:1295  (restore_trash_entry)  if not os.path.isdir(bucket_path):
  fileops.py:1387  (delete_trash_entry)   if not os.path.isdir(bucket_path):
`_is_safe_component` (fileops.py:955-967) only rejects separators and ".." in the single components — it cannot see that `bucket` resolves through a symlink. The routes at routes/rename.py:602 and :620 re-apply exactly the same component check and add nothing. delete_trash_entry then does `os.unlink(fpath)` at fileops.py:1410 with no re-confinement of the resolved path.
Reachability of a writable trash directory outside the volume root: `_same_volume_trash_roots` (fileops.py:751-794) deliberately falls back to `<source_dir>/.scanhound-trash` — i.e. inside a JDownloader extract folder — when the volume root is not writable, and buckets are enumerated to the client by GET /rename/trash.

**Suggested fix:** Add `os.path.islink(bucket_path) or` to the checks at fileops.py:1011, 1295 and 1387 so all five enumerators share one rule, and additionally re-confine the resolved path before the destructive call: assert `os.path.realpath(fpath)` starts with `os.path.realpath(root) + os.sep` in delete_trash_entry and restore_trash_entry.

**Adversarial verifier:** I tried to break this and could not break the factual core; all five line references are exact in `backend/rename/fileops.py` (HEAD 4b40735):
- 1198 `if os.path.islink(bucket_path) or not os.path.isdir(bucket_path): continue` (repair sweep)
- 1609 same line in `sweep_trash`
- 1011 `list_trash_entries`, 1295 `restore_trash_entry`, 1387 `delete_trash_entry` — all bare `os.path.isdir(bucket_path)`, which follows symlinks.

Refutation attempts that FAILED:
1. "A guard exists upstream." No. `_is_safe_component` (955-967) only rejects empty/"."/".."/separators — a bucket name like `20260810-120000` passes. The routes (`backend/api/routes/rename.py:602, 620`) re-apply the identical component check and add nothing. There is no `realpath`/containment re-check on the resolved path before `os.unlink(fpath)` at 1410.
2. "`_begin_trash_operation` requires a manifest record, so a planted bucket can't be deleted." It does NOT for delete: 1074-1084 synthesizes a record when `rec is None` (`if operation != "delete": raise` — delete is explicitly exempt). So the delete path proceeds on a manifest-less symlinked bucket, and additionally writes `manifest.json` into the linked-to directory.
3. "A test already covers it." `tests/test_rename_core.py:1106 test_does_not_follow_symlinks` covers a symlinked FILE inside a real bucket (harmless — `os.unlink` removes the link), not a symlinked BUCKET. No test exercises the bucket case.
4. "The guard is incidental, not a real invariant." The opposite: `empty_trash` (1482-1494) documents that it delegates to `sweep_trash` precisely "so it inherits the sweep's symlink safety" — the codebase treats bucket-symlink refusal as a named safety property, which the three API-reachable primitives do not honor. `/trash/empty` is safe, `/trash/delete` is not.
5. 


---

## [LOW] A corrupt trash-root index silently reports 'no registered roots', hiding trashed files from list/restore/sweep
**Location:** `backend/rename/fileops.py:591`  **Category:** correctness

**Claim:** all_trash_roots() reads the persisted discovery index through the LENIENT reader, which swallows OSError/ValueError and returns [], while the strict reader that raises on the same corruption already exists in the file and is used only on the write path — so an unreadable index turns "I could not determine the roots" into "there are no roots".

**Failure scenario:** A file is trashed onto a deep fallback root (`<library subdir>/.scanhound-trash`, chosen at fileops.py:751-794 because the mount root was not writable); `_record_trash_root` persists it to /data/trash_roots.json. That JSON is later truncated by a crash or a full disk. On the next start, `_read_persisted_trash_roots_unlocked` swallows the ValueError and returns []. all_trash_roots() no longer includes that root, so: GET /rename/trash shows the file as absent; POST /trash/restore returns 404 "Trash entry not found"; the hourly retention sweep never reclaims the space; and `_restore_overwritten_original` (service.py:1378-1379) can no longer restore an overwrite's displaced original, silently escalating a recoverable apply failure into a stranded file. No log line and no error is emitted anywhere — the app reports an empty trash rather than a broken index.

**Evidence:** Lenient reader used by every consumer (fileops.py:585-604):
    try:
        with open(_TRASH_ROOTS_INDEX, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
called from `_load_registered_trash_roots` (fileops.py:607-615), called from `all_trash_roots` (fileops.py:1547-1548: `roots.update(_load_registered_trash_roots())`).
Strict reader that raises, in the same module and unused by any reader (fileops.py:618-639):
        raise OSError(f"Trash-root discovery index is unreadable: {_TRASH_ROOTS_INDEX}")
referenced only from `_record_trash_root` (fileops.py:687).
Consumers of all_trash_roots(): routes/rename.py:592 (GET /trash), :604 (restore), :622 (delete), :634 (empty); app_service.py:594/604 (repair + retention sweep); service.py:1379 and :1889 (_restore_overwritten_original, resolve_keep_plex rollback).

**Suggested fix:** Have `_load_registered_trash_roots` use `_read_trash_root_index_strict_unlocked` (or at minimum log.exception and propagate a `roots_index_error` flag into all_trash_roots' callers) so an unreadable index fails loudly instead of reading as an empty trash. The sweep in particular must not silently skip roots it could not enumerate.

**Adversarial verifier:** The code reads exactly as claimed and I reproduced the behavior, but two load-bearing parts of the reviewer's impact scenario are wrong, so medium is too high.

CONFIRMED mechanic. `backend/rename/fileops.py:585-604` (`_read_persisted_trash_roots_unlocked`) swallows OSError/ValueError/TypeError and returns `[]`; `_load_registered_trash_roots` (607-615) wraps it; `all_trash_roots` (1547-1548) unions it in. The strict twin `_read_trash_root_index_strict_unlocked` (618-639) raises on the identical corruption and is referenced only from `_record_trash_root` (687), i.e. the write path. I ran it directly: with a healthy index containing a deep root, `all_trash_roots()` includes it; after truncating the file to `{"version": 1, "roo` and clearing `_TRASH_ROOTS_RUNTIME` (restart simulation), `all_trash_roots()` drops it silently while the strict reader raises "Trash-root discovery index is unreadable". No log line on the lenient path — that part of the claim is accurate. No caller-side guard exists: routes/rename.py:592/604/622/634, app_service.py:594 (which feeds BOTH `repair_trash_transactions` and `sweep_trash`), and service.py:1379/1889 all pass `all_trash_roots()` straight through.

REFUTED sub-claims that reduce severity:
1. "truncated by a crash or a full disk" is not reachable through this code. The index is only ever written by `_atomic_write_json` (470-490): mkstemp in the same dir, write, flush, fsync, `os.replace`, then parent dir fsync. A crash or ENOSPC kills the temp file before the rename, leaving the previous index byte-intact; the `finally` unlinks the temp. Producing a corrupt `trash_roots.json` requires filesystem-level corruption, external tampering, or a transient mount/permission error on /data (a 9p bind mount, so transient EIO is the only semi-plausible 


---

## [LOW] Cross-device move whose source-trashing fails leaves the media placed at dst while the apply is reported failed, and the SH-H09 message falsely says the slot is empty
**Location:** `backend/rename/fileops.py:1746`  **Category:** correctness

**Claim:** In place_file's EXDEV 'move' path the verified copy is published to dst BEFORE `_trash(src)` runs; if _trash raises, the exception propagates out of place_file with dst fully populated, and apply()'s failure handler then tries to restore the previously-trashed occupant into an occupied dst and emits a message asserting the library slot 'is now EMPTY' when it is not.

**Failure scenario:** Library on a share whose volume root and every writable ancestor refuse the hidden `.scanhound-trash` directory and _DATA_DIR is full. User applies with conflict_strategy="overwrite" and auto_rename_move_method="move": the existing occupant is trashed, the cross-device verified copy publishes the incoming file at dst, then `_trash(src)` fails. place_file raises, apply() marks the job status="failed" (so queue_apply will never retry it — service.py:2221 only admits matched/needs_review — and undo() refuses it at service.py:1789), and the operator is told the library slot is EMPTY and the original is stranded in trash. In fact dst holds the new file, src still holds the download, and the DB has no record that anything was placed. Acting on the message (manually moving the trashed original back over dst) is how the operator loses one of the two files.

**Evidence:** fileops.py:1736-1749:
    try:
        _move_no_replace_durable(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_verify_atomic(src, dst, progress_cb)
        if deletions_require_confirmation:
            _trash(src)          # <- raises; dst is already published, no cleanup
        else:
            os.remove(src)
    return "move"
_trash raises on a real failure by design (fileops.py:927-937: "No trash destination could durably prepare a restore record; source kept").
service.py:1666-1684 treats any place_file exception as a placement failure:
            error_message = self._restore_overwritten_original(trashed_to, restore_slot, job_id, str(e))
            db.update_rename_job(job_id, status="failed", ...)
and _restore_overwritten_original (service.py:1377-1394) calls restore_trash_entry, which returns {"ok": False, "error": "Restore destination already exists"} (fileops.py:1314-1315) because dst is occupied, producing the literal text "the library slot at {dst} is now EMPTY".

**Suggested fix:** Make the EXDEV move branch all-or-nothing: wrap `_trash(src)` so that a failure removes the just-published dst (mirroring `_copy_then_unlink_to_trash` at fileops.py:856-872, which already rolls dst back when the source-consuming step fails) before re-raising — or return a distinct 'placed-but-source-not-disposed' outcome that apply() records as applied-with-warning instead of failed. Additionally, have _restore_overwritten_original check `os.path.lexists(dst)` before asserting the slot is empty.

**Adversarial verifier:** The mechanism is real and unguarded. fileops.py:1736-1748 publishes the verified copy at dst and only then calls _trash(src); _trash raises by design on a genuine failure (fileops.py:930-937) and there is no try/except or dst cleanup around it, so place_file can raise with dst fully populated. service.py:1666-1684 unconditionally treats any place_file exception as "nothing was placed" and calls _restore_overwritten_original without vacating dst; restore_trash_entry then returns "Restore destination already exists" (fileops.py:1314-1315) and the operator gets the literal text "the library slot at {dst} is now EMPTY" while dst in fact holds the incoming file.

The decisive corroboration is the adjacent SH-H08 branch at service.py:1705-1722, which handles the mirror case (place_file succeeded, DB write failed) by calling _fileops.undo_place(src, dst, used) FIRST to free dst, with a comment stating that is why the subsequent restore works. That vacate-before-restore step is exactly what the SH-H09 branch lacks, because it assumes place_file is all-or-nothing — an assumption the EXDEV move path violates. So this is not a reviewer misreading; the codebase itself documents the required step elsewhere.

I could not find a guard that blocks it: no caller-side lexists(dst) check on the failure path, and the three existing SH-H09 tests (tests/test_rename_service.py:873-960) all monkeypatch place_file to a _boom that raises before doing anything, so none exercise a post-publication raise.

Severity downgraded from medium to low on reachability and impact:
1. auto_rename_move_method defaults to "hardlink" (backend/config.py:527). On the default, EXDEV degrades to method="copy" and returns without touching src, so no post-publication step exists at all. The bug requires an explicitly


---

## [LOW] The additive-only default of the manual DV label sync is asserted on a hand-plumbed flag, not on the route — the route call is executed by no test
**Location:** `tests/test_dv_labeler.py:568`  **Category:** test-does-not-discriminate

**Claim:** The tests that claim to pin the manual sync's non-destructive default read `DvSyncRequest().additive_only` and pass it to `reconcile_movie` themselves; nothing executes `dv_sync_labels`, so the single line that carries the safe value into `sync_labels` is untested — and both layers underneath it default to the destructive value.

**Failure scenario:** Drop `additive_only=additive_only` from the `sync_labels(...)` call at backend/api/routes/rename.py:845 (a plausible casualty of any refactor of that kwarg list, since the module default supplies a value silently). Every test in tests/test_dv_labeler.py still passes, including test_manual_sync_request_defaults_to_additive_only and test_manual_sync_default_would_not_strip_that_same_label, because neither touches the route. In production, pressing 'sync labels' once again runs a destructive full reconciliation over every movie in the configured libraries: any title whose path fails to match a dv_scan row that run (a temporarily unmounted Y:, a changed plex_library_path_mapping) yields desired=None and has its managed DV FEL/MEL/P5/P8 label stripped — the exact hazard tests/test_dv_labeler.py:552 calls out, and the same 444-title blast radius the section comment cites at line 546.

**Evidence:** tests/test_dv_labeler.py:568-576 `test_manual_sync_default_would_not_strip_that_same_label` calls `reconcile_movie(mv, {}, VOCAB, pm, dry_run=False, additive_only=DvSyncRequest().additive_only)` — the test itself performs the plumbing the route is supposed to perform. Its section comment at tests/test_dv_labeler.py:549-551 claims otherwise: "These pin the behaviour rather than the flag: they assert on what reaches Plex, so flipping the default back fails them." The load-bearing production line is backend/api/routes/rename.py:829 `additive_only = bool(req.additive_only)` and :845 `additive_only=additive_only`. Both callees default to the dangerous value: `backend/rename/dv_labeler.py:218 def sync_labels(..., additive_only=False)` and `dv_labeler.py:129 def reconcile_movie(..., additive_only=False)`. A grep of tests/ for `dv_sync` / `dv-sync-labels` finds only comments (tests/test_dv_acceptance.py:4, :132) — no test ever calls the route function or POSTs the path.

**Suggested fix:** Add a route-level test that calls `backend.api.routes.rename.dv_sync_labels(DvSyncRequest(), reg=<registry with db + plex_manager>)` (or POSTs /rename/dv-sync-labels via TestClient) with `dv_labeler.sync_labels` patched, and assert `sync.call_args.kwargs["additive_only"] is True` — plus the counterpart with `DvSyncRequest(additive_only=False)` asserting False, so the assertion discriminates rather than matching any constant. Since the route runs the sync in a background thread (`_run` at rename.py:836), either invoke `_run` directly or make the thread joinable for the test.

**Adversarial verifier:** SURVIVES on facts, but the severity is inflated.

Verified by mutation in a full-tree copy (C:\Users\NLSur\AppData\Local\Temp\claude\X--Docker-Apps\4d014f7c-ed33-45d7-aec3-c30608af4bed\scratchpad\mutcheck), baseline green first (37/37 on tests/test_dv_labeler.py).

1. The gap is real. Deleting `additive_only=additive_only` from backend/api/routes/rename.py:845 leaves tests/test_dv_labeler.py at 37/37 passed, and tests/test_api_rename.py + test_dv_acceptance.py + test_dv_settings.py + test_dv_autosync_watermark.py + test_metadata_seed_reconciliation.py + test_dv_import.py at 112/112 passed. Grep confirms no test imports or calls `dv_sync_labels` and no test POSTs `/rename/dv-sync-labels`; the only hits in tests/ are prose comments (tests/test_dv_acceptance.py:4, :132). Both callees really do default to the destructive value (backend/rename/dv_labeler.py:218 `sync_labels(..., additive_only=False)`, :129 `reconcile_movie(..., additive_only=False)`), and the route function is the sole place the safe value is chosen. Neither is the auto-sync kwarg asserted — tests/test_dv_autosync_watermark.py only checks called/not-called and the watermark, never `sync.call_args`. So the whole additive_only boundary is pinned by hand-plumbed unit calls, never by a caller.

2. One part of the evidence overreaches. The reviewer says the section comment at tests/test_dv_labeler.py:549-551 "claims otherwise". The comment's operative claim is "flipping the default back fails them", and that is empirically TRUE: setting `DvSyncRequest.additive_only = False` at rename.py:177 fails exactly test_manual_sync_request_defaults_to_additive_only and test_manual_sync_default_would_not_strip_that_same_label (2 failed, 35 passed). The comment never asserts the route is executed. So the tests do catch the si


---
