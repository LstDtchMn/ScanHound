# Audit-fixes relay block (Jesse: paste the fenced block to ChatGPT)

**Supersedes the version committed at `f593cec`**, which described only the first
commit (`440682d`). Three more fixes landed after it; all four are covered below.

```
Peer review -- STANDALONE AUDIT FIXES, independent of the hybrid-sweep
and category-switch branches. Read the artifacts, not any summary; if
you find yourself reviewing a summary, STOP. (An overnight report also
sits on this branch at docs/reviews/2026-08-06-overnight-report.md --
that is CONTEXT for what was done and why, written for the repo owner
in non-technical language. It is explicitly NOT the review target.
Review the code and tests.)

Repository: LstDtchMn/ScanHound
Branch: agent/audit-fixes-2026-08
Head: 03f0569 (plus this relay commit)
Base: main @ 7adb17b
Commits under review, oldest first:
  440682d  dv_detect no-DV-is-positive-only; watchlist add_item -> add
  93f7060  dv_labeler 'unknown' no longer strips labels; upsert_dv_scan
           preserves a good layer
  fa56dfd  keep_both unchecked write no longer arms a destructive undo
  03f0569  the rename pause now covers the manual apply path

Context: a decomposed full-program audit (nine subsystem readers over
database, scanner core, RSS pipeline, API routes, rename/fileops,
DV/HDR/Kometa, frontend, infra, security) produced 15 critical/high
candidates, then an adversarial pass whose job was to REFUTE each one.
Two were refuted and are NOT here: a Windows drive-letter escape in
trash delete (impossible -- the deployed runtime is the linux
container, verified in-container os.name=posix), and a feed guid
collision aborting ingestion (guid and canonical_url cannot vary
independently against the real feed). This branch carries only
confirmed findings.

1. dv_detect (440682d) -- DV detection FAILURES were reported as an
   authoritative "no Dolby Vision". tempfile.mkstemp pre-creates the
   RPU output at zero bytes and dovi_tool writes it only on success,
   so every genuine failure (mount read error, truncated file, demux
   error) leaves rpu_size == 0; the empty-file test ran BEFORE the
   error discrimination, so those returned {layer:'none', error:None}
   -- the exact inverse of what the surrounding comment says the code
   does. Reachability is the point: the media sits on 9p/SMB bind
   mounts where dovi_tool read failures are an expected event.
   CONFIRMED LIVE VICTIM: Alien Romulus was recorded dv_layer='none'
   from a dovi_tool TIMEOUT on 2026-07-22; it kept its label only
   because a sibling copy scanned clean. "No DV" is now positive-only:
   a clean exit, or dovi_tool itself saying "no rpu"/"not found".
   Test-quality note worth your attention: the pre-existing hard-error
   test passed only because it used rpu_size=5 -- a state dovi_tool
   never leaves behind on failure -- so the suite had no power on the
   axis the bug was on.

2. dv_labeler + upsert_dv_scan (93f7060) -- THE PAIRING, and the most
   important thing to check. Fix 1 converts silent 'none' into honest
   'unknown', and 'unknown' was exactly the value that triggered label
   stripping: reconcile_movie treated `layer is not None` as "matched"
   while desired_label('unknown') is None, so the removal loop
   subtracted nothing and stripped EVERY managed DV label -- in the
   unattended hourly additive-only sync. Shipping fix 1 WITHOUT this
   would have made the system worse, not better. is_authoritative()
   now enforces the "could not run" vs "confirmed no DV" distinction
   dv_detect already documents; 'none' stays authoritative and still
   removes stale labels. `matched` now means matched-to-a-real-finding,
   which also stops sync_labels re-persisting the 'unknown' row on
   every pass. Separately, upsert_dv_scan no longer lets an incoming
   'unknown' destroy a known layer, mirroring the COALESCE
   preserve-on-null the same statement already applies to
   title/rating_key/imdb_id; the signature columns still take the
   incoming NULLs so the intended retry still happens.

3. keep_both (fa56dfd) -- a DATA-LOSS path, not mitigated by the
   auto-rename pause (keep_both is a manual conflict resolution and is
   the default selection in the Compare modal). update_rename_job
   returns False on a failed write rather than raising, and the
   keep_both branch ignored it -- the one write in apply() that did.
   The row then named the ORIGINAL destination while the file was
   placed at the deduped sibling, and undo() REMOVES (not trashes)
   whatever the row names: the pre-existing library file the user
   explicitly chose to KEEP was the thing deleted. Now checked,
   matching restore_key_ok/applied_ok in the same function; nothing
   has moved at that point so the bail-out is a clean on-disk no-op.
   The test asserts the on-disk file list before and after, not the
   return value.

4. rename pause (03f0569) -- "paused" meant only that the JDownloader
   post-extract hook was off. Process -> Apply stayed fully live and
   performed a real, source-consuming move, and fileops' move->hardlink
   downgrade fires only for UNATTENDED applies, so with "require
   confirmation" on it never fires at all. All three apply routes
   funnel through queue_apply, so the gate sits there. NOT gated:
   undo() (recovery must stay reachable while paused) and
   resolve_keep_plex() (moves only the DOWNLOAD to recoverable trash).
   DECLARED TRADE-OFF: the setting is labelled "Enable auto-rename" and
   also arms the JDownloader hook, so applying one file manually now
   means switching that on. Decoupling would need its own setting; that
   is the repo owner's call and is flagged, not decided.

Evidence, all red-first (test fails on the unfixed tree, passes after),
with negative controls stated in each test file:
  440682d  3 failed / 44 passed red; 363 passed / exit 0 green
  93f7060  4 failed / 34 passed red; 228 passed / exit 0 green
  fa56dfd  1 failed / 1 passed red;  346 passed / exit 0 green
  03f0569  2 failed / 1 passed red;  349 passed / exit 0 green
Run in a throwaway container from image scanhound:latest with the
dependency set .github/workflows/tests.yml installs. CI itself is
billing-blocked, so this is author-attested local evidence (declared).

Verdicts requested:
Q1 dv_detect: is "no DV" positive-only the right semantics, and does
   the branch ordering miss any failure shape?
Q2 The dv_labeler/upsert_dv_scan pairing: is 'none' correctly left
   authoritative while 'unknown' is not? Note the consequence I did
   NOT fix in code -- ~13 'none' and 2 'unknown' rows already in the
   live DB may be false negatives from the old dv_detect behaviour.
   That is data, not code. Should the fix ship with a one-off
   re-scan of those rows as a required step?
Q3 The rename pause trade-off in item 4: gate on the existing
   auto-rename toggle (as built), or add a dedicated
   "allow manual applies" setting?
Q4 Any objection to merging this branch ahead of the other two? File
   overlap: none with agent/hybrid-sweep-rebased or
   agent/category-switch-cache-fix.
```
