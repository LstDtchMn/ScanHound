# State of play — end of 2026-08-15

Everything below is pushed. Nothing here is deployed except where marked LIVE.

## Merge-ready (CI green, peer-reviewed, review findings fixed)

| PR | Branch | What it fixes |
|---|---|---|
| #72 | `fix/dv-backwrite-freshness` | hourly full-library re-sync; annotation-vs-observation authority |
| #75 | `feat/dv-tag-set` | multiple managed tags per title |
| #76 | `feat/dv-hdr10` | HDR10 label, DV8/DV5 rename, deterministic HDR aggregation |
| #77 | `fix/jd-stall-visibility` | silent JDownloader stall |

**Merge with `--merge`, not `--squash`** — #76 is stacked on #75, and squashing
breaks a stack (the child re-applies commits that are no longer ancestors).

    gh pr merge 72 --merge -R LstDtchMn/ScanHound
    gh pr merge 75 --merge -R LstDtchMn/ScanHound
    gh pr merge 76 --merge -R LstDtchMn/ScanHound
    gh pr merge 77 --merge -R LstDtchMn/ScanHound

After #76 deploys: copy `docs/kometa/dv_badges.yml` into the Kometa config and
run Kometa. It carries a TRANSITIONAL both-names window (`DV P8 (retiring)` /
`DV P5 (retiring)`) so no poster loses a badge during the rename; delete those
two blocks after a clean pass confirms convergence.

## Peer-review findings, all fixed

* **#72** — the annotation could restore a stale layer over a newer detector
  import. `observed=False` preserved the timestamp and signature, which
  SHARPENED the contradiction rather than removing it. Now
  `annotate_dv_scan_rating_key()`: UPDATE-only, no layer, never inserts.
  Watermark stays PRE-sync, as the reviewer confirmed it should.
* **#76** — title HDR was order-dependent. Verified live: 1,032 rating_keys have
  multiple plex_cache rows and **225 disagree on hdr**, so the HDR10 decision
  was a coin flip for those titles, and a wrong answer REMOVES a correct label.
  Fixed with `MAX(hdr) GROUP BY rating_key`, tested through the real
  DB -> labeler boundary (the old tests started from a hand-built index and
  never touched it).
* **#77** — one global marker let any active problem suppress a NEW one
  indefinitely. Dedup is now keyed by subsystem. Production wiring is pinned
  (deleting the liveness calls from `poll_results` used to leave every test
  green). Failure PHASE is now recorded and logged.
* **#75** — the reviewer's precondition is SATISFIED. A read-only inventory of
  15,250 Plex titles found labels: `Overlay` (15,165, Kometa's own),
  `DV FEL` 267, `DV MEL` 229, `DV P8` 150, `DV P5` 61. **`DV`, `DV7`, `DV8`,
  `DV5` are absent** — unowned and safe for ScanHound to claim.

## LIVE right now (no merge needed)

* `C:\Tools` repaired AND immunised — every file carries explicit ACEs, so a
  repeat of the `/inheritance:r ... /T` lockdown cannot strip them (proven by
  replaying the command against a copy).
* DV scan roots consolidated to `C:/4K Drives` + the Y:/UNC roots (9 total),
  covering all seven drives including the two mis-named "BU" ones.
* 3,769 false permission failures reset; scanning is progressing (~890 of 5,254
  classified, ~4-6 days to clear at observed throughput).
* `ScanHound DV Health Check` scheduled task, **every 30 minutes**, running
  `X:\Docker Apps\scanhound-health\dv_health_check.py` (installed OUTSIDE the
  git tree deliberately — a scheduled task must not depend on the checked-out
  branch). Alerts to Gotify; delivery proven end-to-end.
* `ScanHound DV Detector` task DISABLED (duplicate of `ScanHound-DVScan`).

## Open, not started

* **Why the JD reconnect kept failing for 15 hours** is still unproven. The
  cache invalidation and 90s TTL look correct, so a stale handle alone does not
  explain it. The new failure-phase telemetry is what should answer it on the
  next occurrence — check `jd_poll.failure_phase` in `/health`.
* **RSS coverage-canary hybrid** (#61) — spec'd, not built. Today's numbers
  reinforce it: 500 shadow cycles, 70.4% request saving available, but **192
  relevant misses**, so promotion stays a no-go.
* The reviewer's ACL point: per-file explicit ACEs is recovery, not policy. The
  durable invariant is a hardened parent that lower-trust principals cannot
  write. Also unproven: that the SCHEDULED task resolved `C:\Tools\dovi_tool.exe`
  rather than the repo-local copy — the pinned wrapper prepends `C:\Tools` to
  PATH, which supports it, but the resolved path was not preserved.

## Standing hazards worth re-reading before touching these areas

* `docker-compose.yml` carries REQUIRED uncommitted production mods (ingest key
  env + `127.0.0.1:9721:9721`). Check `grep -c` for both before and after any
  git operation on this tree.
* The health checker exists in TWO places: the repo copy and the installed copy
  at `X:\Docker Apps\scanhound-health\`. Editing the repo copy does NOT change
  what runs. Copy it across after merging #77.
